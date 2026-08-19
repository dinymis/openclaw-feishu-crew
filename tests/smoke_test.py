#!/usr/bin/env python3
"""离线冒烟验收：用虚构 team.json 跑通看板状态机闭环。

不发任何真实飞书消息：get_token / send_card 被 mock 为本地函数，
卡片发送仅打印摘要并返回成功响应，message_id 用本地自增模拟。

用法：
    python3 tests/smoke_test.py

覆盖命令链路（与生产调用等价）：
    board / assign / set-result / complete / stop / clear / fail / retry-due
状态迁移断言：
    assign 后任务 running；set-result 后 review；complete 后 done；
    看板卡片每次状态变化都被刷新；
    clear 仅终态可清、非终态拒绝、清除后 board 不显示且 history 保留。
O1 revision 乐观锁：
    state.json 含 revision 且每次写入 +1；并发写冲突被拒并重读重试成功；
    无 revision 字段的存量文件平滑升级。
O2 waiting_retry 退避重试：
    瞬时错误 fail → waiting_retry + next_retry_at、attempts 不增；
    非瞬时错误 fail → error；retry-due 到点真启动且 attempts 才 +1。
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# --- 虚构配置（不含任何真实值） -------------------------------------------
TMP = tempfile.mkdtemp(prefix="feishu-crew-smoke-")
FAKE_OPEN_ID = "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

TEAM = {
    "default_account": "bot01",
    "accounts": {
        "bot01": {"engineer": "Alice", "open_id": FAKE_OPEN_ID},
        "bot02": {"engineer": "Bob", "open_id": "ou_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"},
    },
    "max_attempts": 3,
    "agents": {"coder": {"model_hint": "your-provider/your-model"}},
    "state_dir": TMP,  # 看板状态落在临时目录，不污染仓库
}

CONFIG_DIR = os.path.join(TMP, "config")
os.makedirs(os.path.join(CONFIG_DIR, "accounts"), exist_ok=True)
with open(os.path.join(CONFIG_DIR, "team.json"), "w") as f:
    json.dump(TEAM, f, ensure_ascii=False)

os.environ["PIPELINE_CONFIG_DIR"] = CONFIG_DIR
os.environ["BOARD_ACCOUNT"] = "bot01"
os.environ["BOARD_OPEN_ID"] = FAKE_OPEN_ID
os.environ["OPENCLAW_CONFIG_PATH"] = os.path.join(TMP, "openclaw.json")

# --- mock 飞书 API：不发真实消息 ------------------------------------------
_seq = [0]
def fake_get_token():
    return "mock-tenant-access-token"

def fake_send_card(card, token, message_id=None):
    _seq[0] += 1
    mid = message_id or f"mock-msg-{_seq[0]:03d}"
    title = card.get("header", {}).get("title", "") if isinstance(card, dict) else ""
    print(f"  [mock-card] {'PATCH' if message_id else 'SEND'} mid={mid} header={title!r}")
    return {"code": 0, "data": {"message_id": mid}}

import pipeline  # noqa: E402（必须在环境变量设置后导入）
pipeline.get_token = fake_get_token
pipeline.send_card = fake_send_card

PASS = 0
def check(name, cond, extra=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name} {extra}")

print("== 1) board（初始空看板） ==")
r = pipeline.show_board()
check("board 发送成功", r.get("code") == 0, f"mid={r['data']['message_id']}")
state = pipeline.load_state()
check("board_message_id 已记录", state.get("board_message_id") == r["data"]["message_id"])

print("== 2) assign（派活给编码猴） ==")
r = pipeline.add_adhoc_task("coder", "冒烟测试任务：虚构需求 A")
check("assign ok", r.get("ok"), r.get("msg"))
task_id = r["task_id"]
state = pipeline.load_state()
t = state["tasks"][task_id]
check("任务 running", t["status"] == "running", f"attempts={t['attempts']}")
check("编码猴占用", state["agents"]["coder"]["status"] == "running")

print("== 3) record-run（记录子会话） ==")
r = pipeline.record_task_run(task_id, run_id="run-mock-001", child_session_key="agent:coder:subagent:mock")
check("record-run ok", r.get("ok"))

print("== 4) set-result（保存结果并转审核） ==")
r = pipeline.set_task_result(task_id, "虚构结果：冒烟测试通过", "冒烟通过")
check("set-result ok", r.get("ok"))
t = pipeline.load_state()["tasks"][task_id]
check("任务 review", t["status"] == "review")

print("== 5) complete（完成任务） ==")
r = pipeline.complete_task(task_id)
check("complete ok", r.get("ok"))
state = pipeline.load_state()
t = state["tasks"][task_id]
check("任务 done", t["status"] == "done" and t["progress"] == 100)
check("编码猴释放", state["agents"]["coder"]["status"] == "idle")

print("== 6) board 刷新（同卡片原地 PATCH） ==")
r = pipeline.show_board()
check("board 二次刷新", r.get("code") == 0 and r["data"]["message_id"] == state["board_message_id"])

print("== 7) clear（终态清除/非终态拒绝） ==")
# 非终态拒绝：新建一个 running 任务尝试 clear
r = pipeline.add_adhoc_task("coder", "冒烟测试任务：虚构需求 B（clear 用例）")
check("assign B ok", r.get("ok"), r.get("msg"))
rid = r["task_id"]
r = pipeline.clear_task(rid)
check("非终态 clear 拒绝", (not r.get("ok")) and "stop/complete" in r.get("msg", ""), r.get("msg", ""))
check("被拒后任务仍在", rid in pipeline.load_state()["tasks"])

# 终态可清：stop 后清除
r = pipeline.stop_task(rid)
check("stop B ok", r.get("ok"))
r = pipeline.clear_task(rid)
check("终态 clear ok", r.get("ok"), r.get("msg"))
state = pipeline.load_state()
check("清除后 tasks 无该任务", rid not in state["tasks"])
check("history 保留记录", any(h.get("id") == rid for h in state.get("history", [])))

# 清除后 board 不显示：build_board 渲染文本不含已清除任务 id
card = pipeline.build_board(state)
board_text = card["elements"][0]["text"]["content"]
check("board 不显示已清除任务", rid not in board_text)

# history 命令仍可查到
r = pipeline.show_history()
check("history 含已清除任务", any(h.get("id") == rid for h in r["history"]))

# 不存在的任务
r = pipeline.clear_task("task-not-exist")
check("不存在任务 clear 拒绝", not r.get("ok"), r.get("msg"))

print("== 8) O1 revision 乐观锁（写入 +1 / 冲突拒绝 / 重读重试） ==")
state = pipeline.load_state()
rev0 = state.get("revision")
check("state.json 含 revision 字段", isinstance(rev0, int), f"revision={rev0}")

r = pipeline.add_adhoc_task("coder", "冒烟测试任务：O1 写入计数")
check("O1 前置 assign ok", r.get("ok"), r.get("msg"))
rev1 = pipeline.load_state()["revision"]
check("每次写入 revision +1", rev1 == rev0 + 1, f"{rev0} -> {rev1}")

# 并发写冲突：两个命令各自持有旧快照，后写入的必须被拒
snap_a = pipeline.load_state()
snap_b = pipeline.load_state()
snap_a["tasks"][r["task_id"]]["summary"] = "并发写 A"
pipeline.save_state(snap_a)
conflict_seen = False
try:
    pipeline.save_state(snap_b)  # snap_b 的 revision 已落后
except pipeline.RevisionConflictError:
    conflict_seen = True
check("旧 revision 写入被拒（RevisionConflictError）", conflict_seen)

# revision_retry 装饰器：第一次保存冲突，重读重试后成功（模拟并发写交错）
o1_tid = r["task_id"]
calls = {"n": 0}
orig_save = pipeline.save_state
def flaky_save(state):
    calls["n"] += 1
    if calls["n"] == 1:
        raise pipeline.RevisionConflictError("模拟并发写抢先")
    return orig_save(state)
pipeline.save_state = flaky_save
try:
    rr = pipeline.record_task_run(o1_tid, run_id="run-conflict-001")
finally:
    pipeline.save_state = orig_save
check("冲突后重读重试成功（revision_retry）", rr.get("ok") and calls["n"] == 2,
      f"save 调用次数={calls['n']}")
t = pipeline.load_state()["tasks"][o1_tid]
check("重试后数据未丢且生效", t.get("run_id") == "run-conflict-001" and t.get("summary") == "并发写 A")

# 存量文件平滑升级：删除 revision 字段（模拟旧版 state）再读写
path = pipeline.state_file()
with open(path) as f:
    raw = json.load(f)
raw.pop("revision", None)
with open(path, "w") as f:
    json.dump(raw, f, ensure_ascii=False)
state = pipeline.load_state()
check("无 revision 存量文件可读（升级兼容）", state.get("revision") == 0)
pipeline.record_task_run(o1_tid, run_id="run-legacy-002")
state = pipeline.load_state()
check("存量文件首次写入后 revision=1", state["revision"] == 1)
check("升级后任务数不丢", state["tasks"][o1_tid]["run_id"] == "run-legacy-002")
pipeline.stop_task(o1_tid)

print("== 9) O2 waiting_retry 退避重试（瞬时/非瞬时分类） ==")
# 瞬时错误：配额耗尽 → waiting_retry + next_retry_at，attempts 不增
r = pipeline.add_adhoc_task("coder", "冒烟测试任务：O2 瞬时失败")
check("O2 前置 assign ok", r.get("ok"), r.get("msg"))
tid = r["task_id"]
attempts_before = pipeline.load_state()["tasks"][tid]["attempts"]
r = pipeline.fail_task(tid, "provider quota exhausted 配额耗尽")
t = pipeline.load_state()["tasks"][tid]
check("瞬时 fail → waiting_retry", t["status"] == "waiting_retry", r.get("msg"))
check("waiting_retry 带 next_retry_at", bool(t.get("next_retry_at")), t.get("next_retry_at"))
check("瞬时 fail attempts 不增", t["attempts"] == attempts_before,
      f"{attempts_before} -> {t['attempts']}")
check("fail 返回 waiting=true/retry=false", r.get("waiting") is True and r.get("retry") is False)

# 看板对 waiting 任务显示预计重试时间，不显示误导性失败计数
card = pipeline.build_board(pipeline.load_state())
board_text = card["elements"][0]["text"]["content"]
check("board 显示等待重试区与预计时间", "等待重试" in board_text and t["next_retry_at"] in board_text)

# retry-due 未到点：不启动
r = pipeline.retry_due_tasks()
check("未到点 retry-due 不启动", r.get("ok") and r.get("started") == [],
      pipeline.load_state()["tasks"][tid]["status"])

# 到点：把 next_retry_at 改到过去，sweep 真启动且 attempts 才 +1
state = pipeline.load_state()
state["tasks"][tid]["next_retry_at"] = "1970-01-01 00:00:00"
pipeline.save_state(state)
r = pipeline.retry_due_tasks()
started_ids = [s["task_id"] for s in r.get("started", [])]
t = pipeline.load_state()["tasks"][tid]
check("到点 retry-due 真启动", tid in started_ids and t["status"] == "running", r.get("msg"))
check("真启动时 attempts 才 +1", t["attempts"] == attempts_before + 1,
      f"{attempts_before} -> {t['attempts']}")
check("retry-due 幂等：重复扫不重复启动",
      pipeline.retry_due_tasks().get("started") == [])

# 非瞬时错误：权限类 → 直接 error，不进 waiting
r = pipeline.add_adhoc_task("coder", "冒烟测试任务：O2 权限失败")
tid2 = r["task_id"]
r = pipeline.fail_task(tid2, "permission denied: 应用缺少卡片权限")
t = pipeline.load_state()["tasks"][tid2]
check("非瞬时 fail → error 终态", t["status"] == "error", r.get("msg"))
check("fail 返回 waiting=false", r.get("waiting") is False)
pipeline.stop_task(tid)  # tid 被 retry-due 拉起为 running，先停止再清理
pipeline.clear_task(tid)
pipeline.clear_task(tid2)

print(f"\nSMOKE PASS: {PASS} checks, state file = {pipeline.state_file()}")
