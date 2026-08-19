#!/usr/bin/env python3
"""镜像层最小自测：状态映射单测 + mock RPC 调用路径。

不依赖真实 gateway/openclaw：subprocess.run 被 mock 为本地函数，
只记录 workboard-mirror.py 发起的 RPC 调用并返回成功响应。

用法：
    python3 tests/mirror_test.py

覆盖：
    1. STATUS_MAP 八态映射正确性（stopped/error/waiting_retry/waiting_decision → blocked 等）；
    2. 红线断言：dispatch/ready/sessionKey 触发 RuntimeError；
    3. sync_task 主链路：boards.upsert → cards.create → cards.move →
       cards.update，done 迁移附带 comment；
    4. 失败降级：RPC 异常时事件流不抛错、任务进入 pending_ops，
       恢复后 flush_pending 补齐。
"""

import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# --- 虚构配置（不含任何真实值） -------------------------------------------
TMP = tempfile.mkdtemp(prefix="feishu-crew-mirror-")

TEAM = {
    "default_account": "bot01",
    "accounts": {
        "bot01": {"engineer": "Alice", "open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
    },
    "state_dir": TMP,  # 镜像状态落在临时目录，不污染仓库
}

CONFIG_DIR = os.path.join(TMP, "config")
os.makedirs(CONFIG_DIR, exist_ok=True)
with open(os.path.join(CONFIG_DIR, "team.json"), "w") as f:
    json.dump(TEAM, f, ensure_ascii=False)

# PIPELINE_CONFIG_DIR 必须在 import workboard-mirror 前设置（模块级读取）
os.environ["PIPELINE_CONFIG_DIR"] = CONFIG_DIR
os.environ["BOARD_ACCOUNT"] = "bot01"

import importlib  # noqa: E402
wm = importlib.import_module("workboard-mirror")

# --- mock subprocess.run：记录 RPC 调用，不启动任何真实进程 ----------------
CALLS = []


class FakeCompleted:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def make_fake_run(fail=False):
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        # cmd: [openclaw, gateway, call, <method>, --params, <json>, ...]
        method = cmd[3]
        params = json.loads(cmd[5])
        CALLS.append((method, params))
        if fail:
            raise RuntimeError(f"模拟 gateway 不可达 method=[{method}]")
        resp = {}
        if method == "workboard.cards.create":
            resp = {"card": {"id": "card-mock-001"}}
        return FakeCompleted(json.dumps(resp))
    return fake_run


wm.subprocess.run = make_fake_run()

# --- 虚构任务台账 -----------------------------------------------------------
TASK_ID = "task-1700000000-abc123"
STATE = {
    "account": "bot01",
    "tasks": {
        TASK_ID: {
            "id": TASK_ID,
            "title": "示例任务",
            "status": "running",
            "agent": "coder",
            "stage": "编码",
            "attempts": 1,
            "max_attempts": 3,
            "summary": "",
            "result": None,
            "last_error": "",
            "run_id": "",
            "child_session_key": "",
        }
    },
}
with open(os.path.join(TMP, "task-state-bot01.json"), "w") as f:
    json.dump(STATE, f, ensure_ascii=False)

FAILURES = []


def check(cond, name):
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")


def set_task_status(status, **extra):
    STATE["tasks"][TASK_ID].update(extra)
    STATE["tasks"][TASK_ID]["status"] = status
    with open(os.path.join(TMP, "task-state-bot01.json"), "w") as f:
        json.dump(STATE, f, ensure_ascii=False)


def methods_called():
    return [m for m, _ in CALLS]


print("== 1. 状态映射单测 ==")
check(wm.STATUS_MAP["pending"] == "todo", "pending -> todo")
check(wm.STATUS_MAP["running"] == "running", "running -> running")
check(wm.STATUS_MAP["review"] == "review", "review -> review")
check(wm.STATUS_MAP["done"] == "done", "done -> done")
check(wm.STATUS_MAP["stopped"] == "blocked", "stopped -> blocked")
check(wm.STATUS_MAP["error"] == "blocked", "error -> blocked")
check(wm.STATUS_MAP["waiting_retry"] == "blocked", "waiting_retry -> blocked")
check(wm.STATUS_MAP["waiting_decision"] == "blocked", "waiting_decision -> blocked")
check(wm.ALLOWED_WB_STATUS == {"todo", "running", "review", "blocked", "done"},
      "镜像卡只允许五态（R1/R4）")

print("== 2. 红线断言 ==")
try:
    wm.assert_red_lines("workboard.cards.dispatch", {"id": "x"})
    check(False, "R3：dispatch 应被拒绝")
except RuntimeError:
    check(True, "R3：dispatch 被拒绝")

try:
    wm.assert_red_lines("workboard.cards.update", {"id": "x", "patch": {"sessionKey": "s"}})
    check(False, "R2：sessionKey 应被拒绝")
except RuntimeError:
    check(True, "R2：sessionKey 被拒绝")

try:
    wm.assert_red_lines("workboard.cards.move", {"id": "x", "status": "ready"})
    check(False, "R1：ready 应被拒绝")
except RuntimeError:
    check(True, "R1：ready 被拒绝")

try:
    wm.assert_red_lines("workboard.cards.move", {"id": "x", "status": "review"})
    check(True, "合法五态 move 放行")
except RuntimeError:
    check(False, "合法五态 move 放行")

print("== 3. sync_task 主链路（mock RPC） ==")
CALLS.clear()
action, card_id = wm.sync_task("bot01", TASK_ID)
ms = methods_called()
check(card_id == "card-mock-001", "cards.create 返回 card_id 被采纳")
check("workboard.boards.upsert" in ms, "先 upsert 建板")
check("workboard.cards.create" in ms, "幂等建卡")
check(("workboard.cards.move" in ms) and ("workboard.cards.update" in ms),
      "move + update notes/labels")
create_params = next(p for m, p in CALLS if m == "workboard.cards.create")
check(create_params.get("idempotencyKey") == TASK_ID, "idempotencyKey=task_id")
move_params = next(p for m, p in CALLS if m == "workboard.cards.move")
check(move_params.get("status") == "running", "running 任务 move 到 running")

# done 迁移附带 comment
CALLS.clear()
set_task_status("done", summary="完成摘要")
wm.sync_task("bot01", TASK_ID)
ms = methods_called()
check("workboard.cards.comment" in ms, "done 迁移附带 comment")
move_params = next(p for m, p in CALLS if m == "workboard.cards.move")
check(move_params.get("status") == "done", "done 任务 move 到 done")

# O6：waiting_decision → blocked + label + 决策 comment
CALLS.clear()
set_task_status("waiting_decision",
                decision={"question": "示例决策问题", "timeout_at": "2026-08-19 18:00:00",
                          "default_action": "approve"})
wm.sync_task("bot01", TASK_ID)
ms = methods_called()
move_params = next(p for m, p in CALLS if m == "workboard.cards.move")
check(move_params.get("status") == "blocked", "waiting_decision 任务 move 到 blocked")
update_params = next(p for m, p in CALLS if m == "workboard.cards.update")
check("pipe:waiting_decision" in update_params["patch"]["labels"], "waiting_decision 带 label")
check("workboard.cards.comment" in ms, "waiting_decision 迁移附带决策 comment")

# O8：blocked（卡住等输入）→ blocked + pipe:blocked label + 原因 comment（与 waiting_decision 区分）
# 先经 running 重置 last_synced，否则同为 blocked 目标不重复附 comment（幂等设计）
CALLS.clear()
set_task_status("running")
wm.sync_task("bot01", TASK_ID)
CALLS.clear()
set_task_status("blocked", blocked_reason="等上游产物：接口文档",
                blockedTaskId="task-1700000000-up0001", blocked_at="2026-08-19 15:00:00")
wm.sync_task("bot01", TASK_ID)
ms = methods_called()
move_params = next(p for m, p in CALLS if m == "workboard.cards.move")
check(move_params.get("status") == "blocked", "blocked 任务 move 到 blocked")
update_params = next(p for m, p in CALLS if m == "workboard.cards.update")
check("pipe:blocked" in update_params["patch"]["labels"], "blocked 带 pipe:blocked label")
check("卡住等输入" in update_params["patch"]["notes"
      ] and "task-1700000000-up0001" in update_params["patch"]["notes"],
      "blocked notes 含原因与 blockedTaskId")
check("workboard.cards.comment" in ms, "blocked 迁移附带卡住原因 comment")
comment_params = next(p for m, p in CALLS if m == "workboard.cards.comment")
check("卡住等输入" in comment_params.get("body", "")
      and "task-1700000000-up0001" in comment_params.get("body", ""),
      "blocked comment 与 waiting_decision 区分（含原因与上游任务）")

# O9：取消意图在 notes 中可见（镜像层只读展示）
CALLS.clear()
set_task_status("stopped", cancel_requested=True,
                cancel_requested_at="2026-08-19 15:00:00")
wm.sync_task("bot01", TASK_ID)
update_params = next(p for m, p in CALLS if m == "workboard.cards.update")
check("cancel_requested 在镜像 notes 可见",
      "cancel_requested" in update_params["patch"]["notes"])

print("== 4. 失败降级与 pending_ops 补同步 ==")
wm.subprocess.run = make_fake_run(fail=True)  # 模拟 gateway 不可达
CALLS.clear()
rc = 0
# 与 CLI event 路径等价：失败必须捕获并降级入 pending_ops
try:
    wm.sync_task("bot01", TASK_ID, quiet=True)
except Exception as e:
    wm.queue_pending("bot01", TASK_ID)
ms = wm.load_mirror_state("bot01")
check(len(ms["pending_ops"]) >= 1, "失败后进 pending_ops 队列")
check(rc == 0, "降级路径不向上抛错")

wm.subprocess.run = make_fake_run()  # gateway 恢复
CALLS.clear()
done = wm.flush_pending("bot01")
check(done >= 1, "恢复后 flush_pending 补齐队列")
ms = wm.load_mirror_state("bot01")
check(len(ms["pending_ops"]) == 0, "补齐后 pending_ops 清空")

print()
if FAILURES:
    print(f"镜像层自测失败：{len(FAILURES)} 项 -> {FAILURES}")
    sys.exit(1)
print("镜像层自测全部通过 ✔")
