#!/usr/bin/env python3
"""deploy.sh 一键部署端到端冒烟验收。

验证「clone 后一条命令走完部署并可自验证」的完整闭环，全部用临时配置目录 +
假凭据（example 占位风格），不真连飞书、不重启 gateway：

  1. deploy.sh 本体：bash -n 语法检查、--help、真实子进程跑一遍
     （前置检查 → setup.py 非交互 → doctor --offline 全绿 → openclaw 缺失只提示），
     再重跑验证幂等；
  2. pipeline.py 状态机全闭环：assign → record-run → set-result → complete；
  3. 新机制断言（commit c62cee1）：瞬时 fail 进 waiting_retry 且 attempts 不增、
     retry-due 到点幂等启动、revision 乐观锁写入递增；
  4. feishu_card.py 卡片渲染冒烟：mock 发送层（_post_json/get_token），
     断言卡片 JSON 结构（schema 2.0 / header / 按钮）与发送/更新 code 路径，不真发；
  5. doctor.py --offline 全绿。

用法：
    python3 tests/deploy_smoke_test.py

退出码：全部断言通过 0；任一失败 1。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

TMP = tempfile.mkdtemp(prefix="feishu-crew-deploy-smoke-")
CONFIG_DIR = os.path.join(TMP, "config")
STATE_DIR = os.path.join(TMP, "state")
OPENCLAW_JSON = os.path.join(TMP, "openclaw.json")
os.makedirs(os.path.join(CONFIG_DIR, "accounts"), exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

# --- 虚构配置（不含任何真实值） -------------------------------------------
# 假凭据：非占位符特征（doctor 的 is_placeholder / is_single_char_suffix 可过），
# 但显然是 example 虚构值，doctor --offline 不会联网探测。
FAKE_APP_ID = "cli_fakebot01000000"
FAKE_APP_SECRET = "fake-s…-01"
FAKE_OPEN_ID = "ou_fakefakefakefakefakefakefake01"

TEAM = {
    "_comment": "deploy_smoke_test 虚构配置，非真实团队。",
    "default_account": "bot01",
    "accounts": {"bot01": {"engineer": "Alice", "open_id": FAKE_OPEN_ID}},
    "max_attempts": 3,
    "agents": {"coder": {"model_hint": "your-provider/your-model"}},
    "card": {"dashboard_title": "🤖 {engineer} 的管理控制台"},
    "state_dir": STATE_DIR,  # 看板状态落临时目录，不污染仓库
}
with open(os.path.join(CONFIG_DIR, "team.json"), "w", encoding="utf-8") as f:
    json.dump(TEAM, f, ensure_ascii=False)

with open(os.path.join(OPENCLAW_JSON), "w", encoding="utf-8") as f:
    json.dump({"channels": {"feishu": {"accounts": {
        "bot01": {"appId": FAKE_APP_ID, "appSecret": FAKE_APP_SECRET}}}}}, f)

# 环境变量必须在 import pipeline / feishu_card 之前设好（两模块均在 import 期读配置）
os.environ["PIPELINE_CONFIG_DIR"] = CONFIG_DIR
os.environ["BOARD_ACCOUNT"] = "bot01"
os.environ["BOARD_OPEN_ID"] = FAKE_OPEN_ID
os.environ["OPENCLAW_CONFIG_PATH"] = OPENCLAW_JSON

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name} {extra}")


def run_deploy(args):
    """以子进程跑 deploy.sh（等价三方用户的真实调用路径）。"""
    env = dict(os.environ)
    env["PIPELINE_CONFIG_DIR"] = CONFIG_DIR
    env["OPENCLAW_CONFIG_PATH"] = OPENCLAW_JSON
    return subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "deploy.sh")] + args,
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)


print("== 1) deploy.sh 本体：语法 / --help / 端到端真实执行 ==")
r = subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", "deploy.sh")],
                   capture_output=True, text=True)
check("bash -n 语法检查通过", r.returncode == 0, r.stderr.strip())

r = run_deploy(["--help"])
check("--help 退出码 0", r.returncode == 0)
check("--help 输出含用法说明", "--smoke" in r.stdout and "setup.py" in r.stdout)

# 真实执行一遍：前置检查 → setup.py（非交互）→ doctor --offline 全绿 → restart 提示
r = run_deploy(["--offline",
                "--account-id", "bot01", "--name", "Alice",
                "--app-id", FAKE_APP_ID, "--app-secret", FAKE_APP_SECRET,
                "--open-id", FAKE_OPEN_ID])
check("deploy.sh 首次执行退出码 0", r.returncode == 0, r.stdout + r.stderr)
check("前置检查 python3 通过", "python3" in r.stdout and "仓库文件完整" in r.stdout)
check("setup.py 被串联执行", "setup 完成" in r.stdout)
check("accounts 凭证由 setup 生成", os.path.isfile(
    os.path.join(CONFIG_DIR, "accounts", "bot01.json")))
with open(os.path.join(CONFIG_DIR, "accounts", "bot01.json"), encoding="utf-8") as f:
    acct = json.load(f)
check("凭证写入且为假凭据", acct["app_id"] == FAKE_APP_ID
      and acct["app_secret"] == FAKE_APP_SECRET)
check("doctor 收尾全绿", "doctor 自检全绿" in r.stdout)
check("openclaw restart 环节有明确提示（无命令时只提示不报错）",
      "gateway restart" in r.stdout)

# 幂等：重跑不覆盖已有配置、仍全绿
r2 = run_deploy(["--offline",
                 "--account-id", "bot01", "--name", "Alice",
                 "--app-id", FAKE_APP_ID, "--app-secret", FAKE_APP_SECRET,
                 "--open-id", FAKE_OPEN_ID])
check("deploy.sh 重跑退出码 0（幂等）", r2.returncode == 0, r2.stderr)
check("重跑保留已有配置", "保留不覆盖" in r2.stdout)

print("== 2) pipeline.py 状态机全闭环：assign → record-run → set-result → complete ==")
# mock 飞书发送层：不发真实消息
import pipeline  # noqa: E402（必须在环境变量设置后导入）

_seq = [0]
def fake_get_token():
    return "mock-tenant-access-token"

def fake_send_card(card, token, message_id=None):
    _seq[0] += 1
    mid = message_id or f"mock-msg-{_seq[0]:03d}"
    return {"code": 0, "data": {"message_id": mid}}

pipeline.get_token = fake_get_token
pipeline.send_card = fake_send_card

rev_prev = pipeline.load_state().get("revision", 0)

r = pipeline.add_adhoc_task("coder", "部署冒烟任务：虚构需求 A")
check("assign ok", r.get("ok"), r.get("msg"))
task_id = r["task_id"]
t = pipeline.load_state()["tasks"][task_id]
check("assign 后任务 running", t["status"] == "running")
rev_assign = pipeline.load_state()["revision"]
check("assign 写入后 revision 递增", rev_assign > rev_prev, f"{rev_prev} -> {rev_assign}")

r = pipeline.record_task_run(task_id, run_id="run-deploy-mock-001",
                             child_session_key="agent:coder:subagent:mock")
check("record-run ok", r.get("ok"))
rev_record = pipeline.load_state()["revision"]
check("record-run 写入后 revision 递增", rev_record > rev_assign)

r = pipeline.set_task_result(task_id, "虚构结果：部署冒烟通过", "冒烟通过")
check("set-result ok", r.get("ok"))
t = pipeline.load_state()["tasks"][task_id]
check("set-result 后任务 review", t["status"] == "review")
rev_result = pipeline.load_state()["revision"]
check("set-result 写入后 revision 递增", rev_result > rev_record)

r = pipeline.complete_task(task_id)
check("complete ok", r.get("ok"))
state = pipeline.load_state()
t = state["tasks"][task_id]
check("complete 后任务 done", t["status"] == "done" and t["progress"] == 100)
check("编码猴释放", state["agents"]["coder"]["status"] == "idle")
check("complete 写入后 revision 递增", state["revision"] > rev_result)

print("== 3) 新机制断言：waiting_retry 退避 / retry-due 幂等 / revision 乐观锁 ==")
# 瞬时错误（timeout 命中 TRANSIENT_ERROR_KEYWORDS）→ waiting_retry，attempts 不增
r = pipeline.add_adhoc_task("coder", "部署冒烟任务：瞬时失败用例")
tid = r["task_id"]
attempts_before = pipeline.load_state()["tasks"][tid]["attempts"]
r = pipeline.fail_task(tid, "request timeout 请求超时")
t = pipeline.load_state()["tasks"][tid]
check("瞬时 fail → waiting_retry", t["status"] == "waiting_retry", r.get("msg"))
check("waiting_retry 带 next_retry_at 退避时间", bool(t.get("next_retry_at")),
      t.get("next_retry_at"))
check("瞬时 fail attempts 不增", t["attempts"] == attempts_before,
      f"{attempts_before} -> {t['attempts']}")

# retry-due 未到点：不启动（幂等扫空）
r = pipeline.retry_due_tasks()
check("未到点 retry-due 不启动", r.get("ok") and r.get("started") == [])
check("未到点任务仍 waiting_retry",
      pipeline.load_state()["tasks"][tid]["status"] == "waiting_retry")

# 到点：next_retry_at 改到过去 → sweep 真启动且 attempts 才 +1；重复扫幂等
state = pipeline.load_state()
state["tasks"][tid]["next_retry_at"] = "1970-01-01 00:00:00"
pipeline.save_state(state)
r = pipeline.retry_due_tasks()
started_ids = [s["task_id"] for s in r.get("started", [])]
t = pipeline.load_state()["tasks"][tid]
check("到点 retry-due 真启动", tid in started_ids and t["status"] == "running")
check("真启动时 attempts 才 +1", t["attempts"] == attempts_before + 1,
      f"{attempts_before} -> {t['attempts']}")
check("retry-due 幂等：重复扫不重复启动",
      pipeline.retry_due_tasks().get("started") == [])

# revision 乐观锁：并发写冲突被拒（RevisionConflictError）
snap_a = pipeline.load_state()
snap_b = pipeline.load_state()
snap_a["tasks"][tid]["summary"] = "并发写 A"
pipeline.save_state(snap_a)
conflict_seen = False
try:
    pipeline.save_state(snap_b)  # revision 已落后于磁盘
except pipeline.RevisionConflictError:
    conflict_seen = True
check("旧 revision 写入被拒（乐观锁生效）", conflict_seen)
pipeline.stop_task(tid)
pipeline.clear_task(tid)
pipeline.clear_task(task_id)

print("== 4) feishu_card.py 卡片渲染冒烟（mock 发送层，不真发） ==")
import feishu_card  # noqa: E402（env 已在 import 前设好，TEAM/CURRENT_ACCOUNT 生效）

check("feishu_card 读到冒烟账号", feishu_card.CURRENT_ACCOUNT == "bot01")

sent = []
def fake_post_json(url, payload, token, method=None):
    sent.append({"url": url, "payload": payload, "token": token, "method": method})
    return {"code": 0, "data": {"message_id": "mock-card-msg-001"}}

feishu_card.get_token = lambda: "mock-tenant-access-token"
feishu_card._post_json = fake_post_json

pipeline_state = {"stages": [
    {"name": "需求分析", "status": "done", "agent": "requirement-analyst"},
    {"name": "技术评审", "status": "running", "agent": "architect"},
    {"name": "编码开发", "status": "idle", "agent": "coder"},
    {"name": "代码评审", "status": "idle", "agent": "code-reviewer"},
    {"name": "测试", "status": "error", "agent": "tester"},
]}
agents = []  # 离线环境无 openclaw agents list，get_agent_statuses 返回空列表
card = feishu_card.build_dashboard_card(agents, pipeline_state)

check("卡片为 schema 2.0 结构", card.get("schema") == "2.0")
check("卡片标题含工程师昵称（dashboard_title 模板生效）",
      "Alice" in card["header"]["title"]["content"])
elements = card["body"]["elements"]
md_blocks = [e for e in elements if e.get("tag") == "markdown"]
check("卡片含 Agent 列表与流水线两个 markdown 区块", len(md_blocks) >= 2)
stage_md = md_blocks[1]["content"]
check("阶段状态渲染正确（done/running/error 图标）",
      "✅" in stage_md and "🔄" in stage_md and "❌" in stage_md)
card_json = json.dumps(card)
check("操作按钮命令路径完整（!board/!pipeline start/!pipeline stop）",
      "!board" in card_json and "!pipeline start" in card_json
      and "!pipeline stop" in card_json)
check("阶段详情按钮按阶段数渲染（!detail 1..5）",
      "!detail 1" in card_json and "!detail 5" in card_json)

# 发送新卡片：code 路径断言（mock _post_json，不真连飞书）
token = feishu_card.get_token()
open_id = feishu_card.resolve_open_id(None)
check("open_id 解析走 BOARD_OPEN_ID/team.json 分层", open_id == FAKE_OPEN_ID)
result = feishu_card.send_card(open_id, card, token)
check("发送卡片返回 code=0", result.get("code") == 0)
check("发送请求命中 im/v1/messages 接口",
      sent[-1]["url"].startswith(feishu_card.FEISHU_API + "/im/v1/messages"))
check("发送请求体 receive_id/msg_type/content 正确",
      sent[-1]["payload"]["receive_id"] == FAKE_OPEN_ID
      and sent[-1]["payload"]["msg_type"] == "interactive"
      and json.loads(sent[-1]["payload"]["content"]).get("schema") == "2.0")

# 更新已有卡片：PUT 路径断言
result = feishu_card.update_card("mock-card-msg-001", card, token)
check("更新卡片返回 code=0", result.get("code") == 0)
check("更新请求为 PUT 且命中 message_id 路径",
      sent[-1]["method"] == "PUT"
      and sent[-1]["url"].endswith("/im/v1/messages/mock-card-msg-001"))

print("== 5) doctor.py --offline 全绿 ==")
import doctor  # noqa: E402
import io
buf = io.StringIO()
code = doctor.run(offline=True, out=buf)
doctor_out = buf.getvalue()
check("doctor --offline 退出码 0", code == 0, doctor_out)
check("doctor 无 ❌ 项", "❌" not in doctor_out)
check("doctor 确认凭证/对齐全绿", "凭证已填写" in doctor_out and "account_id 对齐" in doctor_out)

print(f"\nDEPLOY SMOKE PASS: {PASS} checks, tmp dir = {TMP}")
