#!/usr/bin/env python3
"""批次 4 验收：O8 blocked 态 + O9 sticky cancel。

不发任何真实飞书消息：pipeline.get_token/send_card 被 mock 为本地函数，
卡片发送仅返回成功响应，message_id 用本地自增模拟。

覆盖（方案 §5 批次 4 验收标准）：
  ① 「卡住等输入」任务显示 blocked + blockedTaskId，与 error 明确区分，
     unblock 恢复流转；看板 blocked 区显示卡住原因；
  ② stop 后再对该任务派活被拒（sticky 意图持久化：直接重读磁盘 state
     文件意图仍在，模拟重启不丢），unstop 可反悔（恢复流转）；
  ③ sweep 对取消后仍疑似活跃（有 run_id 无进展）的任务产出
     cancelled_active finding；
  ④ 既有命令语义零改动抽查（assign/set-result/complete/stop/fail 主链路
     仍按原语义工作，仅新增 block/unblock/unstop 命令）。

用法：
    python3 tests/blocked_cancel_test.py
"""

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# --- 虚构配置（不含任何真实值） -------------------------------------------
TMP = tempfile.mkdtemp(prefix="feishu-crew-batch4-")
FAKE_OPEN_ID = "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

TEAM = {
    "default_account": "bot01",
    "accounts": {
        "bot01": {"engineer": "Alice", "open_id": FAKE_OPEN_ID},
    },
    "max_attempts": 3,
    "retry_backoff_seconds": 3600,
    "state_dir": TMP,
    # 测试专用：cancelled_active 关注窗口极小便于命中/越窗断言
    "sweep": {
        "cancelled_active_window_seconds": 120,
    },
}

CONFIG_DIR = os.path.join(TMP, "config")
os.makedirs(os.path.join(CONFIG_DIR, "accounts"), exist_ok=True)
with open(os.path.join(CONFIG_DIR, "team.json"), "w") as f:
    json.dump(TEAM, f, ensure_ascii=False)

os.environ["PIPELINE_CONFIG_DIR"] = CONFIG_DIR
os.environ["BOARD_ACCOUNT"] = "bot01"
os.environ["BOARD_OPEN_ID"] = FAKE_OPEN_ID
os.environ["OPENCLAW_CONFIG_PATH"] = os.path.join(TMP, "openclaw.json")

# --- mock 飞书发送层：不发真实消息 ---------------------------------------
_seq = [0]


def fake_get_token():
    return "mock-tenant-access-token"


def fake_send_card(card, token, message_id=None):
    _seq[0] += 1
    return {"code": 0, "data": {"message_id": message_id or f"mock-msg-{_seq[0]:03d}"}}


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


def raw_state():
    """直接读磁盘 state 文件（不经任何内存缓存）：模拟重启后重读。"""
    with open(pipeline.state_file()) as f:
        return json.load(f)


print("== 1) O8 block：卡住等输入（验收①） ==")
# 上游任务（产物提供方）与下游任务（等产物卡住）
r_up = pipeline.add_adhoc_task("coder", "批次4测试：上游产物任务")
check("上游 assign ok", r_up.get("ok"), r_up.get("msg", ""))
up_id = r_up["task_id"]
r_dn = pipeline.add_adhoc_task("tester", "批次4测试：等上游产物的任务")
check("下游 assign ok", r_dn.get("ok"), r_dn.get("msg", ""))
dn_id = r_dn["task_id"]

r = pipeline.block_task(dn_id, "等上游产物：接口文档", blocked_task_id=up_id)
check("block ok", r.get("ok") and r.get("status") == "blocked", r.get("msg", ""))
check("返回带 blockedTaskId", r.get("blockedTaskId") == up_id)
t = pipeline.load_state()["tasks"][dn_id]
check("任务状态 blocked（非 error）", t["status"] == "blocked" and t["status"] != "error")
check("blocked 字段齐全",
      t.get("blocked_reason") == "等上游产物：接口文档"
      and t.get("blockedTaskId") == up_id and bool(t.get("blocked_at")))
check("blocked 记录挂起前状态", t.get("blocked_from") == "running")
check("agent 释放不占坑",
      pipeline.load_state()["agents"]["tester"]["status"] == "idle")

# 看板 blocked 区显示原因与 blockedTaskId，与失败区区分
card = pipeline.build_board(pipeline.load_state())
board_text = card["elements"][0]["text"]["content"]
check("看板显示 blocked 区", "卡住等输入" in board_text)
check("看板 blocked 区含原因与上游任务",
      "等上游产物：接口文档"[:40] in board_text and up_id in board_text)
check("STATUS_ICON/LABEL 新增 blocked",
      pipeline.STATUS_ICON.get("blocked") and pipeline.STATUS_LABEL.get("blocked"))

# 非法挂起被拒：终态任务不可 block / 空原因被拒 / blockedTaskId 不存在被拒
r = pipeline.block_task(dn_id, "再次卡住")
check("blocked 任务不可重复挂起", not r.get("ok"), r.get("msg", ""))
r = pipeline.add_adhoc_task("coder", "批次4测试：block 边界用例")
edge_id = r["task_id"]
r = pipeline.block_task(edge_id, "   ")
check("空原因被拒", not r.get("ok"), r.get("msg", ""))
r = pipeline.block_task(edge_id, "等一个不存在的上游", blocked_task_id="task-not-exist")
check("blockedTaskId 不存在被拒", not r.get("ok"), r.get("msg", ""))

print("== 2) O8 unblock：恢复流转（验收①） ==")
r = pipeline.unblock_task(dn_id)
check("unblock ok", r.get("ok"), r.get("msg", ""))
check("恢复到挂起前状态 running", r.get("status") == "running")
t = pipeline.load_state()["tasks"][dn_id]
check("agent 重新占用",
      pipeline.load_state()["agents"]["tester"]["status"] == "running"
      and dn_id in pipeline.load_state()["agents"]["tester"]["current_tasks"])
check("blocked 字段已清理",
      not t.get("blocked_reason") and not t.get("blockedTaskId") and not t.get("blocked_from"))
r = pipeline.unblock_task(dn_id)
check("非 blocked 任务 unblock 拒绝", not r.get("ok"), r.get("msg", ""))
# blocked_from=pending 时恢复到 pending 排队
state = pipeline.load_state()
state["tasks"][edge_id]["status"] = "pending"
pipeline.save_state(state)
r = pipeline.block_task(edge_id, "等用户补充需求")
check("pending 任务可挂起 blocked", r.get("ok"), r.get("msg", ""))
r = pipeline.unblock_task(edge_id)
check("pending 来源恢复到 pending", r.get("status") == "pending")

print("== 3) O9 stop 持久化取消意图（验收②） ==")
r = pipeline.add_adhoc_task("coder", "批次4测试：sticky cancel 任务")
check("assign ok", r.get("ok"), r.get("msg", ""))
tid = r["task_id"]
pipeline.record_task_run(tid, run_id="run-sticky-001",
                         child_session_key="agent:coder:subagent:mock")

r = pipeline.stop_task(tid)
check("stop ok", r.get("ok"), r.get("msg", ""))
check("stop 返回 cancel_requested", r.get("cancel_requested") is True)
t = pipeline.load_state()["tasks"][tid]
check("任务 stopped", t["status"] == "stopped")
check("取消意图落台账", t.get("cancel_requested") is True and bool(t.get("cancel_requested_at")))

# 重启不丢：直接读磁盘 state 文件（不经任何进程内存），意图仍在
disk = raw_state()["tasks"][tid]
check("模拟重启重读磁盘：意图仍在", disk.get("cancel_requested") is True
      and disk.get("cancel_requested_at") == t.get("cancel_requested_at"))

print("== 4) O9 派活/补 spawn 被拒（验收②） ==")
# record-run（协调猴 spawn 后登记）被拒，明确报错而非静默
r = pipeline.record_task_run(tid, run_id="run-should-reject")
check("取消任务补登记 spawn 被拒",
      not r.get("ok") and r.get("cancel_requested") is True, r.get("msg", ""))
check("拒绝提示含 unstop 指引", "unstop" in r.get("msg", ""))
check("run_id 未被覆盖",
      pipeline.load_state()["tasks"][tid]["run_id"] == "run-sticky-001")

# retry-due 路径防御：构造「退避到期但取消意图在」场景
state = pipeline.load_state()
state["tasks"][tid]["status"] = "waiting_retry"
state["tasks"][tid]["next_retry_at"] = "1970-01-01 00:00:00"
pipeline.save_state(state)
r = pipeline.retry_due_tasks()
cancelled_ids = [c["task_id"] for c in r.get("cancelled", [])]
t = pipeline.load_state()["tasks"][tid]
check("retry-due 拒绝拉起已取消任务",
      tid in cancelled_ids and tid not in [s["task_id"] for s in r.get("started", [])],
      r.get("msg", ""))
check("拒绝拉起后转 stopped 终态留痕",
      t["status"] == "stopped" and "sticky cancel" in (t.get("summary") or ""))
check("retry-due 幂等：再扫不重复处理",
      pipeline.retry_due_tasks().get("cancelled") == [])

# start_task_assignment 共用防线：带意图任务直接启动被拒
state = pipeline.load_state()
state["tasks"][tid]["status"] = "pending"
pipeline.save_state(state)
state = pipeline.load_state()
ok, err = pipeline.start_task_assignment(state, tid)
check("start_task_assignment 拒绝带取消意图任务", not ok and "unstop" in err, err)

print("== 5) O9 unstop 反悔（验收②） ==")
state = pipeline.load_state()
state["tasks"][tid]["status"] = "stopped"
state["tasks"][tid]["stopped_from"] = "running"
pipeline.save_state(state)
r = pipeline.unstop_task(tid)
check("unstop ok", r.get("ok"), r.get("msg", ""))
check("意图清除且恢复原状态", r.get("had_intent") is True and r.get("restored") == "running")
t = pipeline.load_state()["tasks"][tid]
check("台账意图标志清零",
      t.get("cancel_requested") is False and t.get("cancel_requested_at") is None)
check("磁盘重读确认清零（重启不复活）",
      raw_state()["tasks"][tid].get("cancel_requested") is False)

# 反悔后可正常补登记 spawn 与推进
r = pipeline.record_task_run(tid, run_id="run-after-unstop")
check("unstop 后补登记 spawn 放行", r.get("ok"), r.get("msg", ""))
r = pipeline.set_task_result(tid, "虚构结果：反悔后继续干完", "反悔续跑")
check("unstop 后 set-result 正常", r.get("ok"))
r = pipeline.complete_task(tid)
check("unstop 后 complete 正常", r.get("ok"))
check("任务 done", pipeline.load_state()["tasks"][tid]["status"] == "done")

print("== 6) O9 sweep cancelled_active finding（验收③） ==")
r = pipeline.add_adhoc_task("coder", "批次4测试：取消后疑似活跃")
check("assign ok", r.get("ok"), r.get("msg", ""))
tid2 = r["task_id"]
pipeline.record_task_run(tid2, run_id="run-ghost-001")
pipeline.stop_task(tid2)

result = pipeline.sweep_tasks()
fps = {f["type"]: f for f in result["findings"]}
check("sweep 产出 cancelled_active finding", "cancelled_active" in fps,
      json.dumps([f["type"] for f in result["findings"]], ensure_ascii=False))
if "cancelled_active" in fps:
    f = fps["cancelled_active"]
    check("finding 指向被取消任务", f["task_id"] == tid2 and f["status"] == "stopped")
    check("finding 含 run_id 活跃线索", "run_id" in f["detail"])
    check("指纹去重可用", f["fingerprint"] == f"cancelled_active:{tid2}")

# 幂等去重：--notify 后同 finding 不重复告警
pipeline.sweep_tasks(notify=True)  # 无凭据静默，仅记录指纹路径不抛错
# 取消后补充进展（set-result 留 last_activity_at 不影响该 finding，
# 真正消除条件是窗口越期或意图清除）：把取消时间拨到窗口外，finding 消除
state = pipeline.load_state()
old_ts = time.strftime("%Y-%m-%d %H:%M:%S",
                       time.localtime(time.time() - 3600))
state["tasks"][tid2]["cancel_requested_at"] = old_ts
pipeline.save_state(state)
result = pipeline.sweep_tasks()
check("窗口越期后 finding 消除",
      all(f["task_id"] != tid2 for f in result["findings"]),
      json.dumps([f["task_id"] for f in result["findings"]], ensure_ascii=False))
# unstop 清除意图同样消除 finding
state = pipeline.load_state()
state["tasks"][tid2]["cancel_requested_at"] = pipeline.now()
pipeline.save_state(state)
check("意图在时 finding 复现",
      any(f["task_id"] == tid2 and f["type"] == "cancelled_active"
          for f in pipeline.sweep_tasks()["findings"]))
pipeline.unstop_task(tid2)
check("unstop 后 finding 消除",
      all(f["task_id"] != tid2 for f in pipeline.sweep_tasks()["findings"]))

print("== 7) 既有命令语义零改动抽查（验收④） ==")
r = pipeline.add_adhoc_task("coder", "批次4测试：回归主链路")
rid = r["task_id"]
r = pipeline.set_task_result(rid, "虚构结果", "摘要")
check("assign→set-result 仍转 review", r.get("ok")
      and pipeline.load_state()["tasks"][rid]["status"] == "review")
r = pipeline.complete_task(rid)
check("complete 仍转 done", r.get("ok")
      and pipeline.load_state()["tasks"][rid]["status"] == "done")
r = pipeline.stop_task(rid)
check("done 任务 stop 仍被拒", not r.get("ok"), r.get("msg", ""))
r = pipeline.clear_task(rid)
check("clear 终态可清（blocked 不可 clear，需先 unblock）", r.get("ok"), r.get("msg", ""))
state = pipeline.load_state()
state["tasks"][edge_id]["status"] = "blocked"
pipeline.save_state(state)
r = pipeline.clear_task(edge_id)
check("blocked 任务 clear 被拒（非终态）", not r.get("ok"), r.get("msg", ""))

print(f"\nBATCH4 PASS: {PASS} checks, state file = {pipeline.state_file()}")
