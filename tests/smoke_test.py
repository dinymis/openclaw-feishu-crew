#!/usr/bin/env python3
"""离线冒烟验收：用虚构 team.json 跑通看板状态机闭环。

不发任何真实飞书消息：get_token / send_card 被 mock 为本地函数，
卡片发送仅打印摘要并返回成功响应，message_id 用本地自增模拟。

用法：
    python3 tests/smoke_test.py

覆盖命令链路（与生产调用等价）：
    board / assign / set-result / complete
状态迁移断言：
    assign 后任务 running；set-result 后 review；complete 后 done；
    看板卡片每次状态变化都被刷新。
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

print(f"\nSMOKE PASS: {PASS} checks, state file = {pipeline.state_file()}")
