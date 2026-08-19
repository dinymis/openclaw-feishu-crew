#!/usr/bin/env python3
"""workboard-mirror.py — pipeline → Workboard 单向镜像层（1 档）。

设计文档：docs/workboard-bridge.md

铁律：
  1. pipeline 台账（task-state-<account>.json）是唯一事实源；
  2. 绝不反向写：Workboard 任何变化不回写 pipeline；
  3. 最终一致：镜像失败不阻塞主流程，降级进 pending_ops 队列。

红线（代码内强制断言，见 assert_red_lines）：
  R1 绝不把镜像卡置为 ready（dispatch 拉起 worker 的唯一入口）；
  R2 绝不写 sessionKey/execution/runId（会触发 lifecycle sync 自动移卡）；
     run_id/child_session_key 只写进 notes 纯文本；
  R3 永不实现/调用 cards.dispatch；
  R4 只用 todo/running/review/blocked/done 五态，不用 triage/scheduled/backlog。

同步通道：`openclaw gateway call workboard.*` RPC 子进程（设计文档 §5.3，
已实机验证）。

用法：
  workboard-mirror.py event <account> <task_id> [--comment TEXT]
      单任务事件同步（pipeline.py 各命令成功后异步触发本命令）
  workboard-mirror.py reconcile <account>
      全量对账：以 pipeline 为准补齐差异（存量任务首次同步也用它）
  workboard-mirror.py cleanup <account> <task_id> [<task_id> ...]
      清理指定任务的镜像卡（archive + delete），并从 mirror-state 移除

纯标准库实现，Python ≥ 3.8。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)

CONFIG_DIR = os.environ.get("PIPELINE_CONFIG_DIR") or os.path.join(WORKSPACE, "config")
TEAM_CONFIG_PATH = os.path.join(CONFIG_DIR, "team.json")

# 镜像状态文件（与 task-state 同目录，独立文件，不复用 sync-state.json）
# 结构见设计文档 §6.3
MIRROR_STATE_VERSION = 1

# RPC 超时（毫秒），设计文档 §7.1
GATEWAY_TIMEOUT_MS = 5000

# ---------------------------------------------------------------------------
# 状态映射（设计文档 §2）
# ---------------------------------------------------------------------------
# pipeline 任务态 → workboard 五态（stopped/error/waiting_retry/waiting_decision → blocked + label 区分）
STATUS_MAP = {
    "pending": "todo",
    "running": "running",
    "review": "review",
    "done": "done",
    "stopped": "blocked",
    "error": "blocked",
    "waiting_retry": "blocked",
    "waiting_decision": "blocked",
}

# R1/R4：镜像卡只允许出现的 workboard 状态
ALLOWED_WB_STATUS = {"todo", "running", "review", "blocked", "done"}

# R2：镜像卡参数中绝对禁止出现的字段
FORBIDDEN_CARD_FIELDS = ("sessionKey", "execution", "runId")

# reconcile / 首帧全量同步时主动建卡的任务状态集合（done/stopped 历史跳过，
# 设计文档 §2 边界 / §6.1）
ACTIVE_STATUSES = {"pending", "running", "review", "error", "waiting_retry", "waiting_decision"}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr)


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def truncate(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 配置与台账读取（只读 pipeline 台账，绝不写回 —— 单向铁律）
# ---------------------------------------------------------------------------

def load_team_config():
    try:
        with open(TEAM_CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def state_dir():
    team = load_team_config()
    d = team.get("state_dir")
    return os.path.expanduser(d) if d else WORKSPACE


def engineer_name(account):
    accounts = load_team_config().get("accounts") or {}
    return (accounts.get(account) or {}).get("engineer") or account


def board_id_for(account):
    return f"pipeline-{account}"


def task_state_path(account):
    return os.path.join(state_dir(), f"task-state-{account}.json")


def mirror_state_path(account):
    return os.path.join(state_dir(), f"workboard-mirror-{account}.json")


def mirror_log_path(account):
    return os.path.join(state_dir(), f"workboard-mirror-{account}.log")


def load_task_state(account):
    """只读 pipeline 台账；读不到返回 None（调用方决定降级）。"""
    try:
        with open(task_state_path(account)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f"读取 task-state 失败 account=[{account}] err=[{e}]")
        return None


def load_mirror_state(account):
    blank = {
        "version": MIRROR_STATE_VERSION,
        "account": account,
        "board_id": board_id_for(account),
        "cards": {},
        "pending_ops": [],
    }
    try:
        with open(mirror_state_path(account)) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return blank
        data.setdefault("cards", {})
        data.setdefault("pending_ops", [])
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return blank


def save_mirror_state(account, ms):
    """临时文件 + os.replace 原子替换（与 task-state 同等对待）。"""
    path = mirror_state_path(account)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(ms, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Gateway RPC 通道（设计文档 §5.3）
# ---------------------------------------------------------------------------

def _openclaw_bin():
    return shutil.which("openclaw") or "openclaw"


def assert_red_lines(method, params):
    """红线 R1/R2/R3/R4 强制断言：违反直接抛错，绝不让镜像卡越界。"""
    if method.endswith(".dispatch"):
        # R3：镜像层永不 dispatch
        raise RuntimeError(f"红线 R3 违反：镜像层禁止调用 {method}")
    blob = json.dumps(params, ensure_ascii=False)
    for field in FORBIDDEN_CARD_FIELDS:
        # R2：禁止 sessionKey/execution/runId 出现在任何写参数里
        if f'"{field}"' in blob:
            raise RuntimeError(f"红线 R2 违反：参数中禁止出现 {field}")
    status = None
    if isinstance(params, dict):
        status = params.get("status")
        patch = params.get("patch")
        if status is None and isinstance(patch, dict):
            status = patch.get("status")
    if status is not None and status not in ALLOWED_WB_STATUS:
        # R1（ready）/ R4（triage/scheduled/backlog）
        raise RuntimeError(f"红线 R1/R4 违反：禁止状态 {status}")


def gateway_call(method, params, timeout_ms=GATEWAY_TIMEOUT_MS):
    """通过 `openclaw gateway call` 子进程调用 Workboard RPC。

    失败抛 RuntimeError，由上层 try/except 捕获降级（绝不 raise 到 pipeline）。
    """
    assert_red_lines(method, params)
    cmd = [
        _openclaw_bin(), "gateway", "call", method,
        "--params", json.dumps(params, ensure_ascii=False),
        "--json", "--timeout", str(timeout_ms),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_ms / 1000.0 + 15,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gateway call 超时 method=[{method}] err=[{e}]")
    except OSError as e:
        raise RuntimeError(f"gateway call 启动失败 method=[{method}] err=[{e}]")
    if proc.returncode != 0:
        raise RuntimeError(
            f"gateway call 失败 method=[{method}] rc=[{proc.returncode}] "
            f"stderr=[{truncate(proc.stderr, 300)}]"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gateway call 返回非 JSON method=[{method}] err=[{e}] "
            f"out=[{truncate(proc.stdout, 300)}]"
        )


# ---------------------------------------------------------------------------
# 卡片内容构造（设计文档 §4）
# ---------------------------------------------------------------------------

def card_title(task):
    return f"[{task['id']}] {truncate(task.get('title'), 60)}"


def card_labels(account, task):
    labels = [
        f"acct:{account}",
        f"agent:{task.get('agent') or 'unknown'}",
        "src:pipeline",
    ]
    stage = (task.get("stage") or "").strip()
    if stage:
        labels.append(f"stage:{stage}")
    status = task.get("status")
    if status == "error":
        labels.append("pipe:error")
    elif status == "stopped":
        labels.append("pipe:stopped")
    elif status == "waiting_retry":
        labels.append("pipe:waiting_retry")
    elif status == "waiting_decision":
        labels.append("pipe:waiting_decision")
    return labels


def card_notes(task):
    """notes：任务描述 + attempts/max + summary(200) + last_error(120)
    + run_id/child_session_key（纯文本，R2 禁止写 sessionKey 字段）。"""
    lines = [
        f"任务: {task.get('title') or ''}",
        f"阶段: {task.get('stage') or '-'} · agent: {task.get('agent') or '-'}",
        f"attempts: {task.get('attempts') or 0}/{task.get('max_attempts') or 0}",
        f"创建: {task.get('created_at') or '-'} · 开始: {task.get('started_at') or '-'} · 完成: {task.get('completed_at') or '-'}",
    ]
    summary = truncate(task.get("summary"), 200)
    if summary:
        lines.append(f"summary: {summary}")
    result = truncate(task.get("result"), 200)
    if result:
        lines.append(f"result: {result}")
    last_error = truncate(task.get("last_error"), 120)
    if last_error:
        lines.append(f"last_error: {last_error}")
    if task.get("run_id"):
        lines.append(f"run_id: {task['run_id']}")
    if task.get("child_session_key"):
        lines.append(f"child_session_key: {task['child_session_key']}")
    lines.append("—— pipeline 单向只读镜像，事实源为 task-state，请勿手工改动状态")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 镜像动作
# ---------------------------------------------------------------------------

def ensure_board(account):
    board_id = board_id_for(account)
    gateway_call("workboard.boards.upsert", {
        "id": board_id,
        "name": f"{engineer_name(account)} · 流水线镜像",
        "description": "pipeline.py 单向只读镜像，勿手工改动状态",
    })
    return board_id


def ensure_card(account, ms, task):
    """幂等建卡：idempotencyKey=task_id（服务端原生支持，重复返回已有卡）。

    mirror-state 已有 card_id 则直接复用，跳过 create。
    """
    entry = ms["cards"].get(task["id"])
    if entry and entry.get("card_id"):
        return entry["card_id"], False
    params = {
        "boardId": board_id_for(account),
        "title": card_title(task),
        # 初始状态也走五态白名单；后续 move 校准到目标态
        "status": STATUS_MAP.get(task.get("status"), "todo"),
        "labels": card_labels(account, task),
        "notes": card_notes(task),
        "priority": "normal",
        "idempotencyKey": task["id"],
    }
    agent = task.get("agent")
    if agent:
        # agentId 仅展示/过滤用；R1-R3 保证不会被 dispatch 拉起
        params["agentId"] = agent
    resp = gateway_call("workboard.cards.create", params)
    card_id = (resp.get("card") or {}).get("id")
    if not card_id:
        raise RuntimeError(f"cards.create 未返回 card_id resp=[{truncate(json.dumps(resp), 200)}]")
    return card_id, True


def transition_comment(task, target, last_synced):
    """状态迁移自动附带的 comment（仅在迁移成功那次写入）。"""
    status = task.get("status")
    if target == "done" and last_synced != "done":
        return f"完成：{truncate(task.get('summary') or task.get('result') or '（无摘要）', 200)}"
    if target == "blocked" and last_synced != "blocked":
        if status == "stopped":
            return "人工停止"
        if status == "error":
            return (f"任务失败：{truncate(task.get('last_error') or '（无错误信息）', 120)}"
                    f"（attempts {task.get('attempts') or 0}/{task.get('max_attempts') or 0}）")
        if status == "waiting_retry":
            return (f"瞬时失败退避中，预计重试 {task.get('next_retry_at') or '未知'}："
                    f"{truncate(task.get('last_error') or '（无错误信息）', 120)}")
        if status == "waiting_decision":
            d = task.get("decision") or {}
            return (f"等待用户决策（限时 {d.get('timeout_at') or '未知'}，"
                    f"未决默认 {d.get('default_action') or '-'}）："
                    f"{truncate(d.get('question') or '（无问题描述）', 120)}")
    return None


def sync_task(account, task_id, comment=None, quiet=False):
    """把单个任务同步到 workboard。返回 (action, card_id)；失败抛异常。"""
    state = load_task_state(account)
    if state is None:
        raise RuntimeError("task-state 不可读，跳过同步")
    task = (state.get("tasks") or {}).get(task_id)
    ms = load_mirror_state(account)

    if task is None:
        # 台账里已无此任务：如 mirror-state 有卡记录则归档（clear 语义预留）
        entry = ms["cards"].get(task_id)
        if entry and entry.get("card_id"):
            try:
                gateway_call("workboard.cards.archive", {"id": entry["card_id"]})
            except RuntimeError as e:
                log(f"归档孤儿卡失败 task=[{task_id}] err=[{e}]")
            ms["cards"].pop(task_id, None)
            save_mirror_state(account, ms)
            return "archived-orphan", entry["card_id"]
        return "skip-no-task", None

    ensure_board(account)
    ms = load_mirror_state(account)  # 重读，ensure_board 后取最新

    card_id, created = ensure_card(account, ms, task)
    target = STATUS_MAP.get(task.get("status"))
    if target is None:
        raise RuntimeError(f"未知 pipeline 状态 status=[{task.get('status')}]")
    # 红线双保险（assert_red_lines 在 gateway_call 内也会查）
    if target not in ALLOWED_WB_STATUS:
        raise RuntimeError(f"红线违反：目标状态 {target}")

    entry = ms["cards"].setdefault(task_id, {"card_id": card_id})
    entry["card_id"] = card_id
    last_synced = entry.get("last_synced_status")

    actions = []
    if created:
        actions.append("created")

    # 状态迁移：无条件 move（同态 move 幂等无害，且能覆盖人工拖动，§8 风险3）
    gateway_call("workboard.cards.move", {"id": card_id, "status": target})
    if last_synced != target:
        actions.append(f"moved->{target}")

    # notes/labels 刷新（set-result/fail/error/record-run 均经此落地）
    gateway_call("workboard.cards.update", {
        "id": card_id,
        "patch": {
            "title": card_title(task),
            "notes": card_notes(task),
            "labels": card_labels(account, task),
        },
    })
    actions.append("updated")

    # comment：显式传入优先（fail 重试期用），否则仅状态迁移时自动附带
    body = comment or transition_comment(task, target, last_synced)
    if body:
        gateway_call("workboard.cards.comment", {"id": card_id, "body": body})
        actions.append("commented")

    entry["last_synced_status"] = target
    entry["last_sync_at"] = now_str()
    save_mirror_state(account, ms)

    action = "+".join(actions)
    if not quiet:
        log(f"sync task=[{task_id}] pipeline=[{task.get('status')}] wb=[{target}] card=[{card_id}] {action}")
    return action, card_id


# ---------------------------------------------------------------------------
# pending_ops 降级队列（设计文档 §7 失败策略）
# ---------------------------------------------------------------------------

def queue_pending(account, task_id, comment=None):
    try:
        ms = load_mirror_state(account)
        ms["pending_ops"].append({
            "task_id": task_id,
            "action": "sync",
            "comment": comment,
            "queued_at": now_str(),
        })
        save_mirror_state(account, ms)
    except Exception as e:  # 降级路径自身也绝不抛
        log(f"pending_ops 写入失败 task=[{task_id}] err=[{e}]")


def flush_pending(account):
    """事件驱动补同步：本次动作前先清队列。返回补齐条数。"""
    ms = load_mirror_state(account)
    ops = list(ms.get("pending_ops") or [])
    if not ops:
        return 0
    ms["pending_ops"] = []
    save_mirror_state(account, ms)
    done = 0
    for op in ops:
        task_id = op.get("task_id")
        if not task_id:
            continue
        try:
            sync_task(account, task_id, comment=op.get("comment"), quiet=True)
            done += 1
        except Exception as e:
            log(f"pending 补同步失败 task=[{task_id}] err=[{e}]")
            queue_pending(account, task_id, op.get("comment"))
    return done


# ---------------------------------------------------------------------------
# reconcile 全量对账（设计文档 §6.1 / §7.3）
# ---------------------------------------------------------------------------

def reconcile(account):
    """以 pipeline 为准全量对账补同步。存量任务首次同步也用它。"""
    state = load_task_state(account)
    if state is None:
        print(f"错误：无法读取 {task_state_path(account)}", file=sys.stderr)
        return 1

    tasks = state.get("tasks") or {}
    ms_before = load_mirror_state(account)
    print(f"== reconcile account=[{account}] board=[{board_id_for(account)}] ==")
    print(f"台账任务数={len(tasks)} 已有镜像卡={len(ms_before.get('cards') or {})} "
          f"pending_ops={len(ms_before.get('pending_ops') or [])}")

    flushed = 0
    try:
        flushed = flush_pending(account)
    except Exception as e:
        log(f"flush_pending 异常（继续对账）err=[{e}]")
    if flushed:
        print(f"pending_ops 补齐: {flushed} 条")

    counts = {"created": 0, "synced": 0, "skipped-history": 0, "failed": 0}
    for task_id in sorted(tasks):
        task = tasks[task_id]
        status = task.get("status")
        has_card = task_id in (load_mirror_state(account).get("cards") or {})
        if status not in ACTIVE_STATUSES and not has_card:
            # done/stopped 历史任务首帧跳过（§2 边界），避免看板被历史淹没
            counts["skipped-history"] += 1
            print(f"  [skip ] {task_id} status={status} （历史任务跳过）")
            continue
        try:
            action, card_id = sync_task(account, task_id, quiet=True)
            if "created" in action:
                counts["created"] += 1
            counts["synced"] += 1
            print(f"  [sync ] {task_id} {status} -> {STATUS_MAP.get(status)} "
                  f"card={card_id} ({action})")
        except Exception as e:
            counts["failed"] += 1
            queue_pending(account, task_id)
            print(f"  [fail ] {task_id} status={status} err=[{truncate(str(e), 160)}] （已入 pending_ops）")

    # 孤儿检测：mirror-state 有卡但台账无任务（仅报告，不自动删）
    ms_after = load_mirror_state(account)
    orphans = [tid for tid in (ms_after.get("cards") or {}) if tid not in tasks]
    if orphans:
        print(f"孤儿卡（台账已无任务，仅报告）: {', '.join(orphans)}")

    print(f"== 对账完成 synced={counts['synced']} created={counts['created']} "
          f"skipped-history={counts['skipped-history']} failed={counts['failed']} ==")
    return 0 if counts["failed"] == 0 else 2


# ---------------------------------------------------------------------------
# cleanup 清理镜像卡（验收/运维用：archive + delete）
# ---------------------------------------------------------------------------

def cleanup(account, task_ids):
    rc = 0
    ms = load_mirror_state(account)
    for task_id in task_ids:
        entry = ms.get("cards", {}).get(task_id)
        card_id = entry.get("card_id") if entry else None
        if not card_id:
            print(f"  [miss ] {task_id} 无镜像卡记录")
            continue
        try:
            gateway_call("workboard.cards.archive", {"id": card_id})
            gateway_call("workboard.cards.delete", {"id": card_id})
            ms["cards"].pop(task_id, None)
            print(f"  [clean] {task_id} card={card_id} 已 archive+delete")
        except Exception as e:
            rc = 1
            print(f"  [fail ] {task_id} card={card_id} err=[{truncate(str(e), 160)}]")
    save_mirror_state(account, ms)
    return rc


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="pipeline → Workboard 单向镜像层")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_event = sub.add_parser("event", help="单任务事件同步（pipeline.py 异步触发）")
    p_event.add_argument("account")
    p_event.add_argument("task_id")
    p_event.add_argument("--comment", default=None)

    p_rec = sub.add_parser("reconcile", help="全量对账补同步")
    p_rec.add_argument("account")

    p_clean = sub.add_parser("cleanup", help="archive+delete 指定任务镜像卡")
    p_clean.add_argument("account")
    p_clean.add_argument("task_ids", nargs="+")

    args = parser.parse_args()

    if args.cmd == "event":
        # 先清降级队列，再执行本次动作（设计文档 §7.2）
        try:
            flush_pending(args.account)
            sync_task(args.account, args.task_id, comment=args.comment)
            return 0
        except Exception as e:
            # 降级：绝不非零退出影响调用方，绝不抛异常
            log(f"镜像同步失败，降级入 pending_ops task=[{args.task_id}] err=[{e}]")
            queue_pending(args.account, args.task_id, args.comment)
            return 0

    if args.cmd == "reconcile":
        return reconcile(args.account)

    if args.cmd == "cleanup":
        return cleanup(args.account, args.task_ids)

    return 1


if __name__ == "__main__":
    sys.exit(main())
