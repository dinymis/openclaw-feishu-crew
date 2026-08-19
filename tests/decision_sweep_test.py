#!/usr/bin/env python3
"""批次 3 验收：O6 waiting_decision 决策卡 + O7b audit/sweeper 完整版。

不发任何真实飞书消息：pipeline.get_token/send_card 与 feishu_card 的
get_token/_post_json 均被 mock，决策卡/告警卡只断言渲染结构与 code 路径；
凭据缺失时真发送路径静默跳过不报错（验收①）。

覆盖（方案 §5 批次 3 验收标准）：
  ① 任务可挂 waiting_decision，决策卡渲染+发送路径冒烟通过；
  ② approve/reject/defer 三动作状态流转正确、限时默认策略留痕；
  ③ sweep 产出 findings 四类齐全，模拟卡死任务一个周期内命中；
  ④ 幂等：同一 finding 不重复告警（sweep-state 记 last_notified 指纹）；
  ⑤ 脱敏自查由仓库级 grep 兜底（CI 之外本文件只断言代码路径无内部标识）。

用法：
    python3 tests/decision_sweep_test.py
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
TMP = tempfile.mkdtemp(prefix="feishu-crew-batch3-")
FAKE_OPEN_ID = "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

TEAM = {
    "default_account": "bot01",
    "accounts": {
        "bot01": {"engineer": "Alice", "open_id": FAKE_OPEN_ID},
    },
    "max_attempts": 3,
    "agents": {"coder": {"model_hint": "your-provider/your-model"}},
    "state_dir": TMP,
    # 测试专用：决策限时极短便于验限时默认策略；巡检阈值极小便于命中
    "decision": {
        "timeout_seconds": 3600,
        "default_action": "approve",
        "tiers": {
            "high": {"label": "高危 · 默认否决（需明确批准）",
                     "default_action": "reject", "timeout_seconds": 3600},
        },
    },
    "sweep": {
        "stale_running_seconds": 60,
        "pending_age_seconds": 120,
        "waiting_retry_grace_seconds": 30,
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
SENT_CARDS = []  # 记录 feishu_card 发出的卡片（url/payload）


def fake_get_token():
    return "mock-tenant-access-token"


def fake_send_card(card, token, message_id=None):
    _seq[0] += 1
    return {"code": 0, "data": {"message_id": message_id or f"mock-msg-{_seq[0]:03d}"}}


def fake_post_json(url, payload, token, method=None):
    SENT_CARDS.append({"url": url, "payload": payload, "method": method})
    return {"code": 0, "data": {"message_id": f"mock-card-{len(SENT_CARDS):03d}"}}


import pipeline  # noqa: E402（必须在环境变量设置后导入）
import feishu_card  # noqa: E402

pipeline.get_token = fake_get_token
pipeline.send_card = fake_send_card
feishu_card.get_token = fake_get_token
feishu_card._post_json = fake_post_json

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name} {extra}")


print("== 1) O6 挂起 waiting_decision + 决策卡渲染/发送（验收①） ==")
r = pipeline.add_adhoc_task("coder", "批次3测试：需要用户拍板的任务")
check("前置 assign ok", r.get("ok"), r.get("msg", ""))
tid = r["task_id"]

SENT_CARDS.clear()
r = pipeline.request_decision(
    tid, "方案 A 与方案 B 选哪个？", options=["方案A", "方案B"],
    tier="medium", timeout_seconds=3600, default_action="approve")
check("request-decision ok", r.get("ok") and r.get("waiting") is True, r.get("msg", ""))
check("返回限时与默认策略", bool(r.get("timeout_at")) and r.get("default_action") == "approve")
check("决策卡已发送（mock code 路径）", r.get("card_sent") is True,
      f"sent={len(SENT_CARDS)}")

state = pipeline.load_state()
t = state["tasks"][tid]
check("任务状态 waiting_decision", t["status"] == "waiting_decision")
check("agent 释放（并行线不阻塞）", state["agents"]["coder"]["status"] == "idle")
d = t["decision"]
check("决策元数据齐全", d.get("kind") == "user_decision" and d.get("question")
      and d.get("options") == ["方案A", "方案B"] and d.get("timeout_at")
      and d.get("default_action") == "approve", json.dumps(d, ensure_ascii=False)[:120])
check("决策卡 message_id 已记账", bool(d.get("card_message_id")))

# 决策卡结构断言：三按钮（批准/否决/转交）+ schema 2.0 + 回调命令带 task_id
card_json = json.loads(SENT_CARDS[0]["payload"]["content"])
check("决策卡 schema 2.0", card_json.get("schema") == "2.0")
blob = json.dumps(card_json, ensure_ascii=False)
check("决策卡含三按钮命令", all(f"!decide {tid} {a}" in blob for a in ("approve", "reject", "defer")))
check("决策卡含问题与选项", "方案 A 与方案 B 选哪个" in blob and "方案A" in blob)

# 看板显示待决事项区
card = pipeline.build_board(state)
board_text = card["elements"][0]["text"]["content"]
check("board 显示待决事项区", "待决事项" in board_text and tid in board_text and "限时" in board_text)

# STATUS_ICON/LABEL 已登记
check("STATUS_ICON/LABEL 含 waiting_decision",
      "waiting_decision" in pipeline.STATUS_ICON and "waiting_decision" in pipeline.STATUS_LABEL)

# 凭据缺失静默：去掉 open_id 后 request_decision 仍成功且 card_sent=False
r2 = pipeline.add_adhoc_task("coder", "批次3测试：无凭据挂起")
tid_nocred = r2["task_id"]
pipeline.CURRENT_OPEN_ID = None
saved_resolve = feishu_card.resolve_open_id
feishu_card.resolve_open_id = lambda cli=None: None
try:
    r2 = pipeline.request_decision(tid_nocred, "无凭据也要能挂起")
finally:
    feishu_card.resolve_open_id = saved_resolve
    pipeline.CURRENT_OPEN_ID = FAKE_OPEN_ID
check("凭据缺失静默挂起（不报错）", r2.get("ok") is True and r2.get("card_sent") is False,
      r2.get("msg", ""))
pipeline.stop_task(tid_nocred)
pipeline.clear_task(tid_nocred)

print("== 2) O6 approve/reject/defer 状态流转 + 限时默认策略（验收②） ==")
# defer：保持挂起，限时顺延，留痕
state = pipeline.load_state()
old_timeout = state["tasks"][tid]["decision"]["timeout_at"]
time.sleep(1.1)  # 保证时间串不同
r = pipeline.decide_task(tid, "defer", source="card-callback")
t = pipeline.load_state()["tasks"][tid]
check("defer 保持 waiting_decision", r.get("ok") and t["status"] == "waiting_decision")
check("defer 限时顺延", (t["decision"]["timeout_at"] or "") > old_timeout)
check("defer 留痕", any(e.get("action") == "defer" for e in t["decision"]["log"]))

# approve：恢复 running，agent 重新占用，留痕
r = pipeline.decide_task(tid, "approve", source="card-callback")
t = pipeline.load_state()["tasks"][tid]
check("approve → running", r.get("ok") and t["status"] == "running", r.get("msg", ""))
check("approve 重新占用 agent", pipeline.load_state()["agents"]["coder"]["status"] == "running")
check("approve 留痕（含来源）",
      any(e.get("action") == "approve" and e.get("source") == "card-callback"
          for e in t["decision"]["log"]))
pipeline.complete_task(tid)  # 收尾
pipeline.clear_task(tid)

# reject：stopped 终态，留痕
r = pipeline.add_adhoc_task("coder", "批次3测试：待否决任务")
tid_r = r["task_id"]
r = pipeline.request_decision(tid_r, "这个改动要不要上？")
check("reject 前置挂起 ok", r.get("ok"))
r = pipeline.decide_task(tid_r, "reject", source="cli")
t = pipeline.load_state()["tasks"][tid_r]
check("reject → stopped", r.get("ok") and t["status"] == "stopped", r.get("msg", ""))
check("reject 留痕", any(e.get("action") == "reject" and e.get("source") == "cli"
                        for e in t["decision"]["log"]))
check("非 waiting_decision 拒绝 decide",
      not pipeline.decide_task(tid_r, "approve").get("ok"))
pipeline.clear_task(tid_r)

# 限时默认策略：把 timeout_at 改到过去，retry-due 按默认策略执行并留痕
r = pipeline.add_adhoc_task("coder", "批次3测试：限时未决任务（默认批准）")
tid_t = r["task_id"]
r = pipeline.request_decision(tid_t, "超时默认批准示例", default_action="approve")
state = pipeline.load_state()
state["tasks"][tid_t]["decision"]["timeout_at"] = "1970-01-01 00:00:00"
pipeline.save_state(state)
r = pipeline.retry_due_tasks()
decided = {d["task_id"]: d for d in r.get("decided", [])}
t = pipeline.load_state()["tasks"][tid_t]
check("限时未决按默认策略执行", tid_t in decided and t["status"] == "running",
      r.get("msg", ""))
check("限时默认执行留痕（source=timeout-default）",
      any(e.get("source") == "timeout-default" and e.get("action") == "approve"
          for e in t["decision"]["log"]))
pipeline.stop_task(tid_t)
pipeline.clear_task(tid_t)

# 高危档位：默认策略为 reject
r = pipeline.add_adhoc_task("coder", "批次3测试：高危限时任务")
tid_h = r["task_id"]
r = pipeline.request_decision(tid_h, "高危操作确认", tier="high")
check("high 档默认策略 reject", r.get("default_action") == "reject", r.get("msg", ""))
state = pipeline.load_state()
state["tasks"][tid_h]["decision"]["timeout_at"] = "1970-01-01 00:00:00"
pipeline.save_state(state)
r = pipeline.retry_due_tasks()
t = pipeline.load_state()["tasks"][tid_h]
check("高危限时未决默认否决 → stopped", t["status"] == "stopped")
pipeline.clear_task(tid_h)

# retry-due 幂等：无到期任务返回空
r = pipeline.retry_due_tasks()
check("retry-due 无到期任务幂等", r.get("started") == [] and r.get("decided") == [])

print("== 3) O7b sweep findings 四类齐全 + 卡死一个周期内命中（验收③） ==")
state = pipeline.load_state()
# 造四类异常任务（直接改台账，模拟真实事故现场）。
# 时间戳一律用「当前时间 - N 秒」动态生成，不写死时间串：
# CI 为 UTC 时区，写死的本地时间串在 CI 上可能在未来，导致 elapsed 为负不命中。
def ago_ts(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds))

OLD = ago_ts(3600)  # 1 小时前，远超各测试阈值（stale 60s / pending 120s / grace 30s）

# 3.1 stale_running：running 超阈值无进展（无 run_id）
tid_stale = "task-1700000001-stale1"
state["tasks"][tid_stale] = {
    "id": tid_stale, "title": "模拟卡死任务", "status": "running", "agent": "coder",
    "stage": "编码开发", "progress": 0, "result": None, "summary": "",
    "created_at": OLD, "started_at": OLD,
    "completed_at": None, "message_id": None, "parent_id": None, "sequence": 1,
    "attempts": 1, "max_attempts": 3, "last_error": "", "run_id": "",
    "child_session_key": "", "next_retry_at": None,
    "last_activity_at": OLD,
}
# 3.2 ledger_no_progress：有 run_id 但长期无进展
tid_ledger = "task-1700000002-ledgr2"
state["tasks"][tid_ledger] = dict(state["tasks"][tid_stale])
state["tasks"][tid_ledger].update(
    {"id": tid_ledger, "title": "模拟有账无会话任务", "run_id": "run-mock-dead"})
# 3.3 pending_aged：pending 超龄
tid_pend = "task-1700000003-pendg3"
state["tasks"][tid_pend] = dict(state["tasks"][tid_stale])
state["tasks"][tid_pend].update(
    {"id": tid_pend, "title": "模拟待分配超龄任务", "status": "pending",
     "attempts": 0, "run_id": ""})
# 3.4 retry_overdue：waiting_retry 超过 next_retry_at+宽限仍未拉起
tid_ovd = "task-1700000004-ovdue4"
state["tasks"][tid_ovd] = dict(state["tasks"][tid_stale])
state["tasks"][tid_ovd].update(
    {"id": tid_ovd, "title": "模拟重试超期任务", "status": "waiting_retry",
     "next_retry_at": OLD, "attempts": 1, "run_id": ""})
pipeline.save_state(state)

r = pipeline.sweep_tasks()
by_type = {}
for f in r["findings"]:
    by_type.setdefault(f["type"], []).append(f)
check("stale_running 命中", tid_stale in [f["task_id"] for f in by_type.get("stale_running", [])])
check("ledger_no_progress 命中", tid_ledger in [f["task_id"] for f in by_type.get("ledger_no_progress", [])])
check("pending_aged 命中", tid_pend in [f["task_id"] for f in by_type.get("pending_aged", [])])
check("retry_overdue 命中", tid_ovd in [f["task_id"] for f in by_type.get("retry_overdue", [])])
check("findings 四类齐全", len(by_type) == 4, f"types={sorted(by_type)}")
check("findings 结构化字段齐全",
      all(f.get("fingerprint") and f.get("detail") and isinstance(f.get("age_seconds"), int)
          for f in r["findings"]))
check("人类可读摘要含四类", all(t in r["summary"] for t in
      ("stale_running", "pending_aged", "retry_overdue", "ledger_no_progress")))

# 模拟卡死任务一个周期内命中（新增一个刚卡死的 running，把阈值调到 0 验即时命中）
tid_dead = "task-1700000005-deadn5"
state = pipeline.load_state()
state["tasks"][tid_dead] = dict(state["tasks"][tid_stale])
state["tasks"][tid_dead].update(
    {"id": tid_dead, "title": "刚卡死的任务", "last_activity_at": ago_ts(120)})
pipeline.save_state(state)
orig = pipeline.sweep_stale_running_seconds
pipeline.sweep_stale_running_seconds = lambda: 60  # 测试阈值 60s，120s 前活动必命中
try:
    r = pipeline.sweep_tasks()
finally:
    pipeline.sweep_stale_running_seconds = orig
check("卡死任务一个 sweep 周期内命中",
      tid_dead in [f["task_id"] for f in r["findings"] if f["type"] == "stale_running"])

print("== 4) O7b 幂等：同一 finding 不重复告警（验收④） ==")
SENT_CARDS.clear()
r = pipeline.sweep_tasks(notify=True)
check("首次告警发送", r.get("notified") is True and len(SENT_CARDS) == 1,
      f"sent={len(SENT_CARDS)}")
sweep_state = pipeline.load_sweep_state()
check("sweep-state 记录 last_notified 指纹",
      all(fp in sweep_state["notified"] and sweep_state["notified"][fp].get("last_notified")
          for fp in [f["fingerprint"] for f in r["findings"]]))

r2 = pipeline.sweep_tasks(notify=True)
check("二次 sweep 幂等不重复告警", r2.get("notified") is False and len(SENT_CARDS) == 1,
      f"new={len(r2['new_findings'])} sent={len(SENT_CARDS)}")

# finding 消除后指纹清除，复发可再告警
state = pipeline.load_state()
state["tasks"][tid_dead]["status"] = "done"
pipeline.save_state(state)
r3 = pipeline.sweep_tasks(notify=False)
check("消除后不再命中", tid_dead not in [f["task_id"] for f in r3["findings"]])
sweep_state = pipeline.load_sweep_state()
check("消除后指纹被清除", f"stale_running:{tid_dead}" not in sweep_state["notified"])

# 告警卡结构断言
card_json = json.loads(SENT_CARDS[0]["payload"]["content"])
blob = json.dumps(card_json, ensure_ascii=False)
check("告警卡含四类异常摘要", all(t in blob for t in
      ("stale_running", "pending_aged", "retry_overdue", "ledger_no_progress"))
      or all(k in blob for k in ("卡死", "待分配超龄", "重试超期", "有账无进展")))

# --notify 凭据缺失静默（不报错、notified=False）
saved_resolve = feishu_card.resolve_open_id
feishu_card.resolve_open_id = lambda cli=None: None
try:
    # 先清指纹制造新 finding
    ss = pipeline.load_sweep_state()
    ss["notified"] = {}
    pipeline.save_sweep_state(ss)
    r4 = pipeline.sweep_tasks(notify=True)
finally:
    feishu_card.resolve_open_id = saved_resolve
check("凭据缺失时告警静默跳过不报错", r4.get("ok") is True and r4.get("notified") is False)

print("== 5) 收尾：清理测试任务，存量命令语义不受影响 ==")
for x in (tid_stale, tid_ledger, tid_pend, tid_ovd, tid_dead):
    state = pipeline.load_state()
    task = state["tasks"].get(x)
    if task:
        task["status"] = "stopped"
        pipeline.save_state(state)
    pipeline.clear_task(x)
leftover = pipeline.load_state()["tasks"]
check("清理完成", len(leftover) == 0, f"leftover={list(leftover)}")

print(f"\nBATCH3 PASS: {PASS} checks, state dir = {TMP}")
