#!/usr/bin/env python3
"""Multi-agent parallel task manager.

Supports both full pipeline tasks (4 stages) and ad-hoc assignments
to individual agents. Each agent can hold one task at a time.
"""

__version__ = "2.3.0"

import fcntl
import json
import os
import sys
import time
import uuid
import subprocess
import urllib.request
import urllib.error
from contextlib import contextmanager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
FEISHU_API = "https://open.feishu.cn/open-apis"
DEFAULT_MAX_ATTEMPTS = 3

# --- O1 revision 乐观锁参数 -----------------------------------------------
# 并发写冲突时最多重读重试次数（每次重试 = 重新 load + 重放本次修改）。
REVISION_MAX_RETRIES = 10

# --- O2 退避重试参数 ------------------------------------------------------
# 瞬时错误失败后进入 waiting_retry 状态的默认退避时长（秒），
# 可由 team.json 的 retry_backoff_seconds 覆盖。
DEFAULT_RETRY_BACKOFF_SECONDS = 1800

# --- O6 决策等待参数 ------------------------------------------------------
# waiting_decision 限时（秒），到期未否决按默认策略执行；team.json
# decision.timeout_seconds 可配。默认 24h。
DEFAULT_DECISION_TIMEOUT_SECONDS = 86400
# 限时未决的默认策略：approve=按批准执行 / reject=按否决终止。
# team.json decision.default_action 可配。
DEFAULT_DECISION_DEFAULT_ACTION = "approve"

# --- O7b 巡检（sweep）参数 -------------------------------------------------
# running 超过该时长无任何进展（record-run/set-result 等）→ stale 告警；
# team.json sweep.stale_running_seconds 可配，默认 2h。
DEFAULT_SWEEP_STALE_RUNNING_SECONDS = 7200
# pending 超龄阈值（秒）；team.json sweep.pending_age_seconds 可配，默认 6h。
DEFAULT_SWEEP_PENDING_AGE_SECONDS = 21600
# waiting_retry 超过 next_retry_at 多久仍未被 retry-due 拉起视为异常（秒）；
# team.json sweep.waiting_retry_grace_seconds 可配，默认 1800。
DEFAULT_SWEEP_WAITING_RETRY_GRACE_SECONDS = 1800
# O9：已取消（stop 且取消意图在）但子会话疑似仍活跃的关注窗口（秒）：
# 取消后窗口内且 run_id 已登记 → 产出 cancelled_active finding；
# team.json sweep.cancelled_active_window_seconds 可配，默认 2h。
DEFAULT_SWEEP_CANCELLED_ACTIVE_WINDOW_SECONDS = 7200

# O2：瞬时错误关键词（fail 命令自动检测，命中 → waiting_retry 退避，
# 不烧 attempts；未命中视为权限/配置类错误 → error 终态不重试）。
TRANSIENT_ERROR_KEYWORDS = (
    "quota", "配额", "rate limit", "rate_limit", "ratelimit", "限流",
    "429", "timeout", "timed out", "超时", "5xx", "500", "502", "503", "504",
    "temporarily unavailable", "暂时不可用", "connection reset",
    "overloaded", "server busy", "服务繁忙", "网络", "network",
)

# --- 配置分层（方案 B：单代码库 + 配置分层，v2.0.0） --------------------
# 代码中不再内嵌业务敏感值（飞书凭证/open_id/工程师真名/私有模型名），
# 全部由配置文件注入：
#   config/team.json              team 配置：accounts 表（工程师名/open_id）、
#                                 默认账号、状态文件目录、重试参数、model_hint、
#                                 看板卡片文案模板
#   config/accounts/<id>.json     每个 bot 账号的飞书凭证（app_id/app_secret）
# 配置目录可用 PIPELINE_CONFIG_DIR 环境变量覆盖；配置文件缺失时退化到
# 代码内默认值，保证命令接口行为不变。
CONFIG_DIR = os.environ.get("PIPELINE_CONFIG_DIR") or os.path.join(WORKSPACE, "config")
TEAM_CONFIG_PATH = os.path.join(CONFIG_DIR, "team.json")
ACCOUNTS_CONFIG_DIR = os.path.join(CONFIG_DIR, "accounts")
DEFAULT_OPENCLAW_CONFIG_PATH = "~/.openclaw/openclaw.json"


def load_team_config():
    """加载 team 配置 team.json；缺失或格式错误时返回空 dict（走代码默认值）。"""
    try:
        with open(TEAM_CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


TEAM = load_team_config()

# --- Per-account (per-engineer) isolation -------------------------------
# Each Feishu bot account is one engineer with its own board. State files,
# credentials and cards are isolated per account so one engineer's board
# is never visible to another. The coordinator must set
# BOARD_ACCOUNT (account_id from inbound metadata) and BOARD_OPEN_ID
# (sender open_id in that bot app; open_id is per-app) on every call.
DEFAULT_ACCOUNT = TEAM.get("default_account") or "bot01"
# 参数化点 P3：ACCOUNTS 表（工程师名/open_id）外置到 team.json 的 accounts 段。
ACCOUNTS = TEAM.get("accounts") or {}
CURRENT_ACCOUNT = os.environ.get("BOARD_ACCOUNT") or DEFAULT_ACCOUNT


def account_config():
    return ACCOUNTS.get(CURRENT_ACCOUNT) or {}


def current_open_id():
    """参数化点 P2：open_id 解析。

    优先级：BOARD_OPEN_ID 环境变量 > team.json accounts 表。
    open_id 是 per-app 的，协调猴在未知时通过环境变量传入发送者 open_id，
    因此环境变量必须保持覆盖能力。
    """
    return (os.environ.get("BOARD_OPEN_ID")
            or account_config().get("open_id"))


CURRENT_OPEN_ID = current_open_id()


def openclaw_config_path():
    """参数化点 P6：openclaw.json 路径不再写死。

    优先级：OPENCLAW_CONFIG_PATH 环境变量 > team.json openclaw_config_path > 默认值。
    """
    path = (os.environ.get("OPENCLAW_CONFIG_PATH")
            or TEAM.get("openclaw_config_path")
            or DEFAULT_OPENCLAW_CONFIG_PATH)
    return os.path.expanduser(path)


def state_file():
    # 参数化点 P3：状态文件目录可配置（team.json state_dir），默认工作区根目录。
    state_dir = TEAM.get("state_dir")
    base = os.path.expanduser(state_dir) if state_dir else WORKSPACE
    return os.path.join(base, f"task-state-{CURRENT_ACCOUNT}.json")


def engineer_name():
    return account_config().get("engineer") or CURRENT_ACCOUNT


def default_max_attempts():
    """参数化点 P7：重试默认参数（max_attempts）可配置。"""
    return int(TEAM.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)

# Board agent pool. `openclaw_agent_id` must match the configured
# OpenClaw agent id used with sessions_spawn(agentId=...).
# 参数化点 P4：猴名/图标/别名保留为代码内产品默认值；model_hint 等私有/可定制
# 值不再内嵌（避免暴露私有网关模型名），从 team.json 的 agents 段注入。
AGENT_DEFAULTS = {
    "requirement-analyst": {"name": "需求猴", "icon": "📋", "openclaw_agent_id": "requirement-analyst", "model_hint": "", "aliases": ["需求", "需求分析", "分析师", "分析需求", "需求猴"]},
    "architect": {"name": "架构猴", "icon": "🔍", "openclaw_agent_id": "architect", "model_hint": "", "aliases": ["评审", "技术评审", "review", "审核", "架构", "架构猴"]},
    "code-reviewer": {"name": "质检猴", "icon": "✅", "openclaw_agent_id": "code-reviewer", "model_hint": "", "aliases": ["代码review", "代码评审", "质检", "review代码", "质检猴"]},
    "coder": {"name": "编码猴", "icon": "💻", "openclaw_agent_id": "coder", "model_hint": "", "aliases": ["编码", "开发", "写代码", "coder", "程序员", "编码猴"]},
    "tester": {"name": "测试猴", "icon": "🧪", "openclaw_agent_id": "tester", "model_hint": "", "aliases": ["测试", "tester", "写测试", "qa", "测试猴"]},
    "ops": {"name": "运维猴", "icon": "🔧", "openclaw_agent_id": "ops", "model_hint": "", "aliases": ["运维", "部署", "上线", "sre", "ops", "运维猴"]},
}


def _merged_agents():
    """team.json agents 段覆盖默认定义（name/icon/aliases/model_hint 均可配置）。"""
    overrides = TEAM.get("agents") or {}
    merged = {}
    for aid, info in AGENT_DEFAULTS.items():
        item = dict(info)
        item.update(overrides.get(aid) or {})
        merged[aid] = item
    return merged


AGENTS = _merged_agents()


def board_title():
    """参数化点 P5：看板卡片标题文案模板化（team.json card.board_title）。"""
    tpl = (TEAM.get("card") or {}).get("board_title") or "🤖 {engineer}智能体 · 多Agent任务看板"
    try:
        return tpl.format(engineer=engineer_name())
    except (KeyError, IndexError, ValueError):
        return tpl

PIPELINE_STAGES = [
    {"id": 1, "name": "需求分析", "agent": "requirement-analyst", "icon": "📋"},
    {"id": 2, "name": "技术评审", "agent": "architect", "icon": "🔍"},
    {"id": 3, "name": "编码开发", "agent": "coder", "icon": "💻"},
    {"id": 4, "name": "代码评审", "agent": "code-reviewer", "icon": "✅"},
    {"id": 5, "name": "测试", "agent": "tester", "icon": "🧪"},
]

STATUS_ICON = {
    "pending": "⏳",
    "running": "🔄",
    "review": "👀",
    "done": "✅",
    "stopped": "🛑",
    "error": "❌",
    "idle": "⏸",
    "waiting_retry": "⏰",
    "waiting_decision": "🗳️",
    "blocked": "🚧",
}

STATUS_LABEL = {
    "pending": "待分配",
    "running": "进行中",
    "review": "待审核",
    "done": "完成",
    "stopped": "已停止",
    "error": "失败",
    "idle": "空闲",
    "waiting_retry": "等待重试",
    "waiting_decision": "等待决策",
    "blocked": "卡住等输入",
}


def load_config():
    with open(openclaw_config_path()) as f:
        return json.load(f)


def load_account_credentials():
    """参数化点 P1：读取 per-account 凭证文件 config/accounts/<id>.json。"""
    path = os.path.join(ACCOUNTS_CONFIG_DIR, f"{CURRENT_ACCOUNT}.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    app_id = data.get("app_id") or data.get("appId")
    app_secret = data.get("app_secret") or data.get("appSecret")
    if app_id and app_secret:
        return {"appId": app_id, "appSecret": app_secret}
    return None


def resolve_feishu_account():
    """飞书凭证解析优先级：环境变量覆盖 > accounts/<id>.json > openclaw.json。

    v1.x 从 openclaw.json 读取的行为保留为兜底，现有部署不改配置也能跑。
    """
    env_id = os.environ.get("FEISHU_APP_ID")
    env_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_id and env_secret:
        return {"appId": env_id, "appSecret": env_secret}
    acct = load_account_credentials()
    if acct:
        return acct
    f = load_config()["channels"]["feishu"]
    accounts = f.get("accounts", {})
    acct = accounts.get(CURRENT_ACCOUNT)
    if acct and "appId" in acct and "appSecret" in acct:
        return acct
    if "appId" in f and "appSecret" in f:
        return f
    acct = accounts.get(f.get("defaultAccount")) or next(iter(accounts.values()), None)
    if not acct or "appId" not in acct or "appSecret" not in acct:
        raise RuntimeError("feishu credentials not found in config (checked channels.feishu and accounts)")
    return acct


def get_token():
    f = resolve_feishu_account()
    req = urllib.request.Request(
        f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": f["appId"], "app_secret": f["appSecret"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["tenant_access_token"]


def load_state():
    path = state_file()
    try:
        with open(path) as f:
            state = json.load(f)
        if state.get("version") != 2:
            state = migrate_state(state)
        return normalize_state(state)
    except (FileNotFoundError, json.JSONDecodeError):
        return normalize_state(make_fresh_state())


def make_fresh_state():
    return {
        "version": 2,
        "revision": 0,
        "account": CURRENT_ACCOUNT,
        "engineer": engineer_name(),
        "agents": {aid: {"status": "idle", "current_tasks": []} for aid in AGENTS},
        "tasks": {},
        "history": [],
        "board_message_id": None,
    }


def normalize_task(task):
    task.setdefault("attempts", 0)
    task.setdefault("max_attempts", default_max_attempts())
    task.setdefault("last_error", "")
    task.setdefault("run_id", "")
    task.setdefault("child_session_key", "")
    task.setdefault("next_retry_at", None)
    task.setdefault("last_activity_at", None)
    # O8 blocked 态：blockedTaskId=卡住来源任务 id（等上游产物时），
    # blocked_reason=卡住原因，blocked_from=挂起前状态（unblock 恢复用）。
    task.setdefault("blockedTaskId", "")
    task.setdefault("blocked_reason", "")
    task.setdefault("blocked_from", "")
    task.setdefault("blocked_at", None)
    # O9 sticky cancel：取消意图持久化（stop 时写入，重启不丢），
    # assign/dispatch 补 spawn 前检查该标志，在则拒绝并提示 unstop。
    task.setdefault("cancel_requested", False)
    task.setdefault("cancel_requested_at", None)
    return task


def normalize_agent_state(state):
    for aid in AGENTS:
        agent = state.setdefault("agents", {}).setdefault(aid, {})
        current = agent.get("current_tasks", [])
        legacy = agent.pop("current_task", None)
        if isinstance(current, str):
            current = [current]
        if legacy and legacy not in current:
            current.append(legacy)
        current = [
            tid for tid in current
            if tid in state.get("tasks", {}) and state["tasks"][tid].get("status") == "running"
        ]
        agent["current_tasks"] = current
        agent["status"] = "running" if current else "idle"


def normalize_state(state):
    state.setdefault("version", 2)
    # O1：存量 state 文件（无 revision 字段）平滑升级，首次写入后 revision=1。
    state.setdefault("revision", 0)
    state.setdefault("account", CURRENT_ACCOUNT)
    state.setdefault("engineer", engineer_name())
    state.setdefault("agents", {})
    state.setdefault("tasks", {})
    state.setdefault("history", [])
    state.setdefault("board_message_id", None)
    for task in state["tasks"].values():
        normalize_task(task)
    normalize_agent_state(state)
    return state


def migrate_state(old):
    """Migrate v1 state (current_task + history) to v2."""
    state = make_fresh_state()

    def migrate_task(t, default_status="stopped"):
        """Convert a v1 single-pipeline task into v2 tasks per stage."""
        base_id = t.get("id") or f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        title = t.get("title", "未命名任务")
        created_at = t.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        message_id = t.get("message_id")
        out = []
        for s in PIPELINE_STAGES:
            sid = str(s["id"])
            st = t.get("stages", {}).get(sid, {})
            stage_status = st.get("status", "idle")
            if stage_status == "running":
                status = "running"
            elif stage_status == "review":
                status = "review"
            elif stage_status == "done":
                status = "done"
            else:
                status = "stopped" if default_status == "stopped" else "pending"
            task_id = f"{base_id}-{sid}"
            out.append({
                "id": task_id,
                "title": f"{title} · {s['name']}",
                "status": status,
                "agent": s["agent"],
                "stage": s["name"],
                "progress": 0,
                "result": st.get("result"),
                "summary": st.get("summary", ""),
                "created_at": created_at,
                "started_at": st.get("started_at"),
                "completed_at": st.get("completed_at"),
                "message_id": message_id if s["id"] == 1 else None,
                "parent_id": base_id,
                "sequence": s["id"],
                "attempts": 1 if status == "running" else 0,
                "max_attempts": default_max_attempts(),
                "last_error": "",
                "run_id": "",
                "child_session_key": "",
            })
            if status == "running":
                state["agents"][s["agent"]]["status"] = "running"
                state["agents"][s["agent"]]["current_tasks"] = [task_id]
        return out

    if old.get("current_task"):
        for t in migrate_task(old["current_task"], default_status="running"):
            state["tasks"][t["id"]] = t
    for h in old.get("history", []):
        for t in migrate_task(h, default_status="stopped"):
            state["tasks"][t["id"]] = t
    return state


def save_state(state):
    """O1 revision 乐观锁写入：

    写前比较磁盘 revision 与本次读取时的期望值（state["revision"]），
    一致才写入并 +1；不一致（并发写已抢先）抛 RevisionConflictError，
    由调用方的 revision_retry 重读重试。全程持文件锁，避免
    「读-比-写」竞态。旧文件无 revision 字段视为 0，兼容存量升级。
    """
    path = state_file()
    with _state_file_lock():
        disk_revision = 0
        try:
            with open(path) as f:
                disk_revision = int(json.load(f).get("revision") or 0)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            disk_revision = 0
        expected = int(state.get("revision") or 0)
        if disk_revision != expected:
            raise RevisionConflictError(
                f"state revision conflict: disk={disk_revision} expected={expected}")
        state["revision"] = expected + 1
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


class RevisionConflictError(Exception):
    """O1：并发写冲突（磁盘 revision 已超前于本次读取的期望值）。"""


@contextmanager
def _state_file_lock():
    """state 文件互斥锁（纯标准库 fcntl），保证 revision 检查+写入原子。"""
    lock_path = state_file() + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def revision_retry(func):
    """O1：命令级重试装饰器。

    被装饰函数每次执行都会重新 load_state，因此保存冲突时直接
    重放整个命令（重读最新状态 + 重新应用修改），最多
    REVISION_MAX_RETRIES 次。
    """
    def wrapper(*args, **kwargs):
        for attempt in range(REVISION_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except RevisionConflictError:
                if attempt == REVISION_MAX_RETRIES - 1:
                    raise
                continue
        raise RevisionConflictError("unreachable")
    return wrapper


# --- Workboard 镜像层（1 档，单向只读镜像，可选功能） -----------------
# 设计文档：docs/workboard-bridge.md
# pipeline 台账是唯一事实源；镜像失败由镜像层自行降级（pending_ops），
# 绝不阻塞主流程、绝不抛异常。未启用 workboard 插件/gateway 不可达时
# 触发器与镜像层均静默降级，不影响任何 pipeline 命令。
# 回滚本层：删除下方各命令尾部的 _trigger_workboard_mirror(...) 调用即可，
# 镜像层其余部分完全独立。
def _trigger_workboard_mirror(task_ids, comment=None):
    """命令成功后异步触发 workboard-mirror.py event（不等待、不读返回）。"""
    try:
        script = os.path.join(SCRIPT_DIR, "workboard-mirror.py")
        if not os.path.exists(script):
            return
        for tid in task_ids:
            if not tid:
                continue
            cmd = [sys.executable, script, "event", CURRENT_ACCOUNT, tid]
            if comment:
                cmd += ["--comment", comment]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        # 触发器自身静默失败，绝不影响主流程
        pass


def new_task_id():
    return f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def make_button(text, btn_type, command):
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "value": {"text": command},
    }


def send_card(card, token, message_id=None):
    content = json.dumps(card)
    if message_id:
        # Feishu card update uses PATCH with partial content only.
        url = f"{FEISHU_API}/im/v1/messages/{message_id}"
        payload = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="PATCH",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {token}"},
        )
    else:
        if not CURRENT_OPEN_ID:
            raise RuntimeError(
                f"no open_id known for account {CURRENT_ACCOUNT}; "
                f"pass BOARD_OPEN_ID=<sender open_id in this bot app>"
            )
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type=open_id"
        payload = json.dumps({
            "receive_id": CURRENT_OPEN_ID, "msg_type": "interactive",
            "content": content,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {token}"},
        )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        raise


def agent_is_available(state, agent_id):
    # No concurrency limit for now; agents are always available.
    return True


def assign_agent(state, agent_id, task_id):
    a = state["agents"].get(agent_id, {})
    a["status"] = "running"
    current = a.get("current_tasks", [])
    if task_id not in current:
        current.append(task_id)
    a["current_tasks"] = current


def release_agent(state, agent_id):
    a = state["agents"].get(agent_id, {})
    current = [tid for tid in a.get("current_tasks", [])
               if tid in state["tasks"] and state["tasks"][tid].get("status") == "running"]
    a["current_tasks"] = current
    if not current:
        a["status"] = "idle"
    else:
        a["status"] = "running"


def create_task(state, title, agent_id, stage=None, parent_id=None, sequence=1):
    if agent_id not in AGENTS:
        return None, f"未知 agent：{agent_id}"
    task_id = new_task_id()
    task = {
        "id": task_id,
        "title": title,
        "status": "pending",
        "agent": agent_id,
        "stage": stage or AGENTS[agent_id]["name"],
        "progress": 0,
        "result": None,
        "summary": "",
        "created_at": now(),
        "started_at": None,
        "completed_at": None,
        "message_id": None,
        "parent_id": parent_id,
        "sequence": sequence,
        "attempts": 0,
        "max_attempts": default_max_attempts(),
        "last_error": "",
        "run_id": "",
        "child_session_key": "",
    }
    state["tasks"][task_id] = task
    return task_id, None


def start_task_assignment(state, task_id):
    """Try to start a pending task by assigning it to its agent."""
    task = state["tasks"].get(task_id)
    if not task:
        return False, "任务不存在"
    # O9 sticky cancel：带取消意图的任务拒绝启动（retry-due/补派活路径共用）。
    if task.get("cancel_requested"):
        return False, (f"任务已取消（sticky 意图在，取消于 "
                       f"{task.get('cancel_requested_at') or '未知'}），"
                       f"拒绝补 spawn；如确需继续请先 unstop {task_id}")
    if task["status"] != "pending":
        return False, f"任务状态不是待分配：{task['status']}"
    agent_id = task["agent"]
    if not agent_is_available(state, agent_id):
        return False, f"{AGENTS[agent_id]['name']} 正忙"
    assign_agent(state, agent_id, task_id)
    task["status"] = "running"
    task["started_at"] = now()
    task["last_activity_at"] = now()
    task["attempts"] = int(task.get("attempts") or 0) + 1
    task["last_error"] = ""
    return True, None


@revision_retry
def add_adhoc_task(agent_id, title):
    state = load_state()
    task_id, err = create_task(state, title, agent_id)
    if err:
        return {"ok": False, "msg": err}
    ok, err = start_task_assignment(state, task_id)
    if not ok:
        # Keep it pending; it will queue for that agent.
        save_state(state)
        _trigger_workboard_mirror([task_id])
        return {"ok": True, "msg": f"任务已创建并排队（{err}）", "task_id": task_id}
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"已分配给 {AGENTS[agent_id]['name']}", "task_id": task_id}


@revision_retry
def start_pipeline(title):
    state = load_state()
    parent_id = new_task_id()
    created = []
    for s in PIPELINE_STAGES:
        agent_id = s["agent"]
        task_id, err = create_task(
            state, f"{title} · {s['name']}", agent_id,
            stage=s["name"], parent_id=parent_id, sequence=s["id"]
        )
        if err:
            return {"ok": False, "msg": err}
        created.append((task_id, agent_id))

    # Try to start stage 1 immediately.
    first_task_id = created[0][0]
    ok, err = start_task_assignment(state, first_task_id)
    if not ok:
        save_state(state)
        _trigger_workboard_mirror([tid for tid, _ in created])
        return {"ok": True, "msg": f"流水线已创建，阶段1排队中（{err}）", "parent_id": parent_id}

    save_state(state)
    _trigger_workboard_mirror([tid for tid, _ in created])
    update_board(state)
    return {"ok": True, "msg": f"流水线启动：{title}", "parent_id": parent_id}


@revision_retry
def complete_task(task_id):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] not in ("running", "review"):
        return {"ok": False, "msg": f"无法完成，当前状态：{task['status']}"}

    agent_id = task["agent"]
    release_agent(state, agent_id)
    task["status"] = "done"
    task["completed_at"] = now()
    task["progress"] = 100

    # Auto-start next pipeline stage if applicable.
    next_task_id = None
    if task.get("parent_id") and task.get("sequence"):
        next_seq = task["sequence"] + 1
        next_task = None
        for t in state["tasks"].values():
            if t.get("parent_id") == task["parent_id"] and t.get("sequence") == next_seq:
                next_task = t
                break
        if next_task:
            started, err = start_task_assignment(state, next_task["id"])
            if started:
                state["tasks"][next_task["id"]]["result"] = "（前置阶段已完成，自动开始）"
            next_task_id = next_task["id"]

    save_state(state)
    _trigger_workboard_mirror([task_id, next_task_id])
    update_board(state)
    return {"ok": True, "msg": f"任务完成：{task['title']}"}


@revision_retry
def review_task(task_id):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] != "running":
        return {"ok": False, "msg": f"当前不是进行中，无法提交审核：{task['status']}"}
    task["status"] = "review"
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"已提交审核：{task['title']}"}


@revision_retry
def stop_task(task_id):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] == "done":
        return {"ok": False, "msg": "已完成任务不能停止"}
    agent_id = task["agent"]
    release_agent(state, agent_id)
    # O9 sticky cancel：取消意图持久化到 state（重启不丢），
    # 后续对该任务的补 spawn（retry-due 拉起 / record-run 登记）会被拒绝；
    # unstop 命令可清除意图反悔。stopped_from 记录停止前状态供 unstop 恢复。
    task["stopped_from"] = task["status"]
    task["status"] = "stopped"
    task["completed_at"] = now()
    task["cancel_requested"] = True
    task["cancel_requested_at"] = now()
    save_state(state)
    _trigger_workboard_mirror([task_id], comment="人工停止（取消意图已持久化，补 spawn 将被拒绝；unstop 可反悔）")
    update_board(state)
    return {"ok": True,
            "cancel_requested": True,
            "msg": f"已停止：{task['title']}（取消意图已记录，补 spawn 将被拒绝；unstop 可反悔）"}


@revision_retry
def unstop_task(task_id):
    """O9：清除取消意图（用户反悔）。

    stopped 任务恢复为停止前状态（stopped_from，缺省 pending）：
    原状态为 running/review 时重新占用 agent；其余回到 pending 排队。
    非 stopped 任务仅清除残留意图标志（如意图在但状态已被其他命令改动）。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    had_intent = bool(task.get("cancel_requested"))
    task["cancel_requested"] = False
    task["cancel_requested_at"] = None
    restored = None
    if task["status"] == "stopped":
        prev = task.get("stopped_from") or "pending"
        if prev in ("running", "review"):
            assign_agent(state, task["agent"], task_id)
            task["status"] = prev
            task["completed_at"] = None
            task["last_activity_at"] = now()
            restored = prev
        else:
            task["status"] = "pending"
            task["completed_at"] = None
            restored = "pending"
    save_state(state)
    _trigger_workboard_mirror([task_id], comment="取消意图已清除（unstop）")
    update_board(state)
    return {"ok": True,
            "had_intent": had_intent,
            "restored": restored,
            "status": task["status"],
            "msg": (f"取消意图已清除：{task['title']}"
                    + (f"（恢复为 {restored}）" if restored else ""))}


@revision_retry
def set_task_result(task_id, result_text, summary=None):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    task["result"] = result_text
    task["summary"] = summary or result_text[:200].strip()
    task["last_activity_at"] = now()
    if task["status"] == "running":
        task["status"] = "review"
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"结果已保存：{task['title']}"}


@revision_retry
def record_task_run(task_id, run_id=None, child_session_key=None):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    # O9 sticky cancel：已停止且取消意图在的任务拒绝补登记 spawn，
    # 明确报错提示（而非静默新建/静默拉起）；unstop 可反悔。
    if task.get("cancel_requested") and task.get("status") in ("stopped", "error"):
        return {"ok": False,
                "cancel_requested": True,
                "msg": (f"任务已取消（sticky 意图在，取消于 {task.get('cancel_requested_at') or '未知'}），"
                        f"拒绝补登记 spawn；如确需继续请先 unstop {task_id}")}
    if run_id:
        task["run_id"] = run_id
    if child_session_key:
        task["child_session_key"] = child_session_key
    task["last_activity_at"] = now()
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"运行信息已记录：{task['title']}"}


def retry_backoff_seconds():
    """O2：瞬时错误退避时长（秒），team.json retry_backoff_seconds 可配，默认 1800。"""
    return int(TEAM.get("retry_backoff_seconds") or DEFAULT_RETRY_BACKOFF_SECONDS)


# --- O6 waiting_decision 决策等待 -----------------------------------------
# 任务等用户决策时挂起为 waiting_decision（复用 waiting 基础设施思路，
# kind=user_decision）：带决策元数据（问题/选项/档位/限时/默认策略）。
# 决策卡统一经 scripts/feishu_card.py 通道发送（批准/否决/转交三按钮，
# 档位文案全部配置化：team.json decision.tiers / decision_card 段，脱敏）。
# 用户拍板入口：pipeline.py decide <task_id> approve|reject|defer
# （卡片回调与手工命令共用）。限时未否决按默认策略执行并留痕，
# 到期执行由 retry-due（现有每分钟 cron）驱动。

DECISION_ACTIONS = ("approve", "reject", "defer")

# 允许挂起等待决策的来源状态（非终态活动任务）。
DECISION_SUSPENDABLE_STATUS = ("running", "review", "pending")


def decision_config():
    return TEAM.get("decision") or {}


def decision_timeout_seconds():
    return int(decision_config().get("timeout_seconds") or DEFAULT_DECISION_TIMEOUT_SECONDS)


def decision_default_action():
    action = decision_config().get("default_action") or DEFAULT_DECISION_DEFAULT_ACTION
    return action if action in ("approve", "reject") else DEFAULT_DECISION_DEFAULT_ACTION


def decision_tiers():
    """决策档位表：代码内为通用默认文案，team.json decision.tiers 可全量覆盖。

    档位语义与决策分级清单对齐：低危可逆默认可批、高危默认否决（需明确批准）。
    文案不含任何内部标识，可按部署环境自由定制。
    """
    defaults = {
        "low": {
            "label": "低危可逆 · 默认批准",
            "default_action": "approve",
            "timeout_seconds": 3600,
        },
        "medium": {
            "label": "中危 · 限时未否决按默认策略执行",
            "default_action": "approve",
            "timeout_seconds": DEFAULT_DECISION_TIMEOUT_SECONDS,
        },
        "high": {
            "label": "高危 · 默认否决（需明确批准）",
            "default_action": "reject",
            "timeout_seconds": DEFAULT_DECISION_TIMEOUT_SECONDS,
        },
    }
    overrides = decision_config().get("tiers") or {}
    merged = {}
    for key, info in defaults.items():
        item = dict(info)
        item.update(overrides.get(key) or {})
        merged[key] = item
    return merged


def _parse_ts(ts):
    """解析 '%Y-%m-%d %H:%M:%S' 时间串为 epoch 秒；非法/空返回 None。"""
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _send_decision_card(task):
    """O6：决策卡经 feishu_card.py 通道发送（批准/否决/转交三按钮）。

    凭据或收件人 open_id 缺失时静默跳过（返回 None），绝不阻塞主流程；
    发送成功返回卡片 message_id（记入决策元数据供后续更新）。
    """
    try:
        real_dir = os.path.dirname(os.path.realpath(__file__))
        if real_dir not in sys.path:
            sys.path.insert(0, real_dir)
        import feishu_card
        open_id = feishu_card.resolve_open_id(None)
        if not open_id:
            return None
        token = feishu_card.get_token()
        d = task.get("decision") or {}
        tier_label = decision_tiers().get(d.get("tier") or "", {}).get("label", "")
        card = feishu_card.build_decision_card({
            "task_id": task["id"],
            "title": task.get("title") or task["id"],
            "question": d.get("question") or "",
            "options": d.get("options") or [],
            "tier_label": tier_label,
            "timeout_at": d.get("timeout_at") or "",
            "default_action": d.get("default_action") or decision_default_action(),
        })
        result = feishu_card.send_card(open_id, card, token)
        if isinstance(result, dict) and result.get("code") == 0:
            return (result.get("data") or {}).get("message_id")
        return None
    except Exception:
        # 凭据缺失/网络异常：静默降级，决策状态已在台账生效，卡片可后补
        return None


def _record_decision_card_id(task_id, message_id):
    """决策卡发送成功后补记 message_id（O1：自带冲突重读重试）。"""
    for _ in range(REVISION_MAX_RETRIES):
        state = load_state()
        task = state["tasks"].get(task_id)
        if not task:
            return
        d = task.setdefault("decision", {})
        if d.get("card_message_id") == message_id:
            return
        d["card_message_id"] = message_id
        try:
            save_state(state)
            return
        except RevisionConflictError:
            continue


@revision_retry
def request_decision(task_id, question, options=None, tier=None,
                     default_action=None, timeout_seconds=None):
    """O6：把任务挂起为 waiting_decision，等待用户决策。

    决策元数据（问题/选项/档位/限时/默认策略）写入 task["decision"]；
    优先级：显式参数 > 档位默认 > team.json decision 全局默认。
    挂起后 agent 释放（不占坑），并行线不互相阻塞；决策卡经
    feishu_card.py 发送，凭据缺失静默跳过不报错。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] not in DECISION_SUSPENDABLE_STATUS:
        return {"ok": False,
                "msg": f"当前状态 {task['status']} 不可挂起等待决策"
                       f"（仅 {'/'.join(DECISION_SUSPENDABLE_STATUS)}）"}

    tiers = decision_tiers()
    tier_info = tiers.get(tier or "", {}) if tier else {}
    if tier and not tier_info:
        return {"ok": False, "msg": f"未知决策档位：{tier}（可选 {'/'.join(tiers)}）"}

    timeout = int(timeout_seconds or tier_info.get("timeout_seconds")
                  or decision_timeout_seconds())
    action = default_action or tier_info.get("default_action") or decision_default_action()
    if action not in ("approve", "reject"):
        return {"ok": False, "msg": f"默认策略只能是 approve/reject，收到：{action}"}

    release_agent(state, task["agent"])
    prev_status = task["status"]
    task["status"] = "waiting_decision"
    timeout_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + timeout))
    task["decision"] = {
        "kind": "user_decision",
        "question": question,
        "options": list(options or []),
        "tier": tier or "",
        "tier_label": tier_info.get("label", ""),
        "requested_at": now(),
        "timeout_at": timeout_at,
        "timeout_seconds": timeout,
        "default_action": action,
        "prev_status": prev_status,
        "status": "waiting",
        "card_message_id": None,
        "log": [],
    }
    task["summary"] = f"等待用户决策：{question[:80]}（限时 {timeout_at}）"
    save_state(state)
    _trigger_workboard_mirror(
        [task_id], comment=f"挂起等待决策：{question[:80]}（限时 {timeout_at}）")
    update_board(state)

    # 决策卡发送：失败/无凭据静默，不影响挂起结果
    card_message_id = _send_decision_card(task)
    if card_message_id:
        _record_decision_card_id(task_id, card_message_id)

    return {
        "ok": True,
        "waiting": True,
        "task_id": task_id,
        "status": "waiting_decision",
        "timeout_at": timeout_at,
        "default_action": action,
        "card_sent": bool(card_message_id),
        "msg": (f"任务已挂起等待决策（限时 {timeout_at}，未否决默认 {action}）："
                f"{task['title']}"),
    }


def _apply_decision(state, task, action, source, note=""):
    """O6：把决策结果应用到任务（approve 恢复执行 / reject 终止），留痕到决策记录。"""
    d = task.setdefault("decision", {})
    ts = now()
    d.setdefault("log", []).append(
        {"ts": ts, "action": action, "source": source, "note": note})
    d["resolved_at"] = ts
    d["resolution"] = f"{action}（{source}）"
    d["status"] = "resolved"
    if action == "approve":
        task["status"] = "running"
        task["completed_at"] = None
        task["last_activity_at"] = ts
        assign_agent(state, task["agent"], task["id"])
        task["summary"] = f"决策批准（{source}），恢复执行"
    else:  # reject
        release_agent(state, task["agent"])
        task["status"] = "stopped"
        task["completed_at"] = ts
        task["summary"] = f"决策否决（{source}），任务终止"


@revision_retry
def decide_task(task_id, action, source="cli"):
    """O6：用户拍板入口（卡片回调与手工命令共用）。

    approve → 恢复 running（重新占用 agent）；reject → stopped 终态；
    defer → 保持 waiting_decision，限时顺延一个周期并留痕（转交他人处理）。
    决策记录全部写入 task["decision"]["log"]。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] != "waiting_decision":
        return {"ok": False,
                "msg": f"任务不在等待决策状态（当前：{task['status']}），无法 decide"}
    if action not in DECISION_ACTIONS:
        return {"ok": False, "msg": f"未知决策动作：{action}（可选 {'/'.join(DECISION_ACTIONS)}）"}

    d = task.get("decision") or {}
    if action == "defer":
        # 转交/顺延：保持挂起，限时顺延一个周期，留痕
        timeout = int(d.get("timeout_seconds") or decision_timeout_seconds())
        new_timeout_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(time.time() + timeout))
        d.setdefault("log", []).append(
            {"ts": now(), "action": "defer", "source": source,
             "note": f"转交/顺延，限时更新为 {new_timeout_at}"})
        d["timeout_at"] = new_timeout_at
        d["status"] = "deferred"
        task["decision"] = d
        task["summary"] = f"决策已转交，限时顺延至 {new_timeout_at}"
        save_state(state)
        _trigger_workboard_mirror([task_id], comment=f"决策转交（{source}），限时顺延至 {new_timeout_at}")
        update_board(state)
        return {"ok": True, "waiting": True, "task_id": task_id,
                "action": "defer", "timeout_at": new_timeout_at,
                "msg": f"决策已转交，限时顺延至 {new_timeout_at}：{task['title']}"}

    _apply_decision(state, task, action, source)
    save_state(state)
    _trigger_workboard_mirror([task_id], comment=f"决策 {action}（{source}）")
    update_board(state)
    return {
        "ok": True,
        "waiting": False,
        "task_id": task_id,
        "action": action,
        "status": task["status"],
        "agent_id": task["agent"],
        "msg": f"决策已生效（{action}/{source}）：{task['title']}",
    }


def _collect_expired_decisions(state):
    """O6：找出限时已到且仍未决策的 waiting_decision 任务。"""
    now_ts = time.time()
    expired = []
    for task in state["tasks"].values():
        if task.get("status") != "waiting_decision":
            continue
        timeout_at = _parse_ts((task.get("decision") or {}).get("timeout_at"))
        if timeout_at is not None and now_ts >= timeout_at:
            expired.append(task)
    return expired


def is_transient_error(error_text):
    """O2：判断错误是否为瞬时错误（配额/限流/超时/5xx 等）。

    命中 TRANSIENT_ERROR_KEYWORDS 任一关键词即视为瞬时错误，
    适合退避后重试；否则视为权限/配置类错误，重试无益。
    """
    text = (error_text or "").lower()
    return any(kw.lower() in text for kw in TRANSIENT_ERROR_KEYWORDS)


@revision_retry
def fail_task(task_id, error_text):
    """Mark a task failure; transient errors back off instead of burning attempts.

    O2 重试纪律（2026-08-13 定，2026-08-19 落地）：
    - 瞬时错误（配额/限流/429/超时/5xx 等）→ 任务转 waiting_retry 状态，
      记 next_retry_at（默认退避 1800s），attempts 不增；到点后由
      retry-due 扫描转回并真启动，此时 attempts 才 +1。
    - 非瞬时错误（权限/配置类）→ 直接转 error 终态，不可重试，
      与 error 命令路径一致。

    返回体兼容旧字段 retry/agent_id：waiting_retry 时 retry=false、
    waiting=true，由 retry-due（cron 驱动）到点拉起，协调猴无需
    在 fail 返回后立即 spawn。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}

    task["last_error"] = error_text
    agent_id = task["agent"]
    release_agent(state, agent_id)

    attempts = int(task.get("attempts") or 0)
    max_attempts = int(task.get("max_attempts") or default_max_attempts())

    # 非瞬时错误：权限/配置类，重试无益，直接 error 终态
    if not is_transient_error(error_text):
        task["status"] = "error"
        task["completed_at"] = now()
        task["summary"] = f"不可重试（权限/配置类）：{error_text[:120]}"
        save_state(state)
        _trigger_workboard_mirror([task_id])
        update_board(state)
        return {
            "ok": True,
            "retry": False,
            "waiting": False,
            "msg": f"任务失败（非瞬时错误，不重试）：{task['title']}",
            "task_id": task_id,
            "agent_id": agent_id,
            "attempts": attempts,
            "max_attempts": max_attempts,
        }

    # 瞬时错误但重试额度已耗尽 → error 终态
    if attempts >= max_attempts:
        task["status"] = "error"
        task["completed_at"] = now()
        task["summary"] = f"重试耗尽 {attempts}/{max_attempts}：{error_text[:120]}"
        save_state(state)
        _trigger_workboard_mirror([task_id])
        update_board(state)
        return {
            "ok": True,
            "retry": False,
            "waiting": False,
            "msg": f"任务失败且重试耗尽：{task['title']}",
            "task_id": task_id,
            "agent_id": agent_id,
            "attempts": attempts,
            "max_attempts": max_attempts,
        }

    # 瞬时错误且有重试额度 → waiting_retry + 退避，attempts 不增
    backoff = retry_backoff_seconds()
    next_retry_ts = time.time() + backoff
    task["status"] = "waiting_retry"
    task["next_retry_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_retry_ts))
    task["completed_at"] = None
    task["summary"] = (f"瞬时失败退避中（attempts 不增），预计重试 "
                       f"{task['next_retry_at']}：{error_text[:80]}")
    save_state(state)
    _trigger_workboard_mirror(
        [task_id],
        comment=f"瞬时失败，退避至 {task['next_retry_at']}：{error_text[:80]}",
    )
    update_board(state)
    return {
        "ok": True,
        "retry": False,
        "waiting": True,
        "next_retry_at": task["next_retry_at"],
        "msg": f"任务瞬时失败，已转等待重试（预计 {task['next_retry_at']}）：{task['title']}",
        "task_id": task_id,
        "agent_id": agent_id,
        "attempts": attempts,
        "max_attempts": max_attempts,
    }


@revision_retry
def retry_due_tasks():
    """O2/O6：扫描到点任务并执行（sweep，幂等）。

    驱动端由 cron 定期调用（如每分钟）。两类到期动作：
    1. O2：waiting_retry 且 next_retry_at <= 当前时间 → 真启动，
       attempts 此时才 +1（账实相符：账上 +1 即代表真启动了一次）；
    2. O6：waiting_decision 限时已到且未决策 → 按配置的默认策略
       执行（approve 恢复 / reject 终止）并留痕到决策记录。
    返回本次启动/决策的任务列表，由协调猴/调用方负责后续动作。
    幂等：无到点任务时返回空列表，不写状态。
    """
    state = load_state()
    now_str = now()
    due = [t for t in state["tasks"].values()
           if t.get("status") == "waiting_retry"
           and (t.get("next_retry_at") or "") <= now_str]
    expired_decisions = _collect_expired_decisions(state)
    if not due and not expired_decisions:
        return {"ok": True, "started": [], "decided": [], "cancelled": [],
                "msg": "无到点重试/决策任务"}

    started = []
    cancelled = []
    for task in sorted(due, key=lambda x: x.get("next_retry_at") or ""):
        # O9 sticky cancel：退避期内被 stop 的理论上已不是 waiting_retry，
        # 但防御性检查：若取消意图在则不拉起，转 stopped 终态并留痕。
        if task.get("cancel_requested"):
            task["next_retry_at"] = None
            task["status"] = "stopped"
            task["summary"] = "退避到期但取消意图在，拒绝拉起（sticky cancel）"
            cancelled.append({"task_id": task["id"], "agent_id": task["agent"]})
            continue
        task["next_retry_at"] = None
        task["status"] = "pending"  # 回归可分配态，再由 start_task_assignment 真启动
        ok, err = start_task_assignment(state, task["id"])
        if ok:
            started.append({
                "task_id": task["id"],
                "agent_id": task["agent"],
                "attempts": task["attempts"],
                "max_attempts": int(task.get("max_attempts") or default_max_attempts()),
            })
        else:
            # 启动失败（理论上不应发生）：回退等待下个周期再扫
            task["status"] = "waiting_retry"
            task["next_retry_at"] = now_str

    decided = []
    for task in sorted(expired_decisions, key=lambda x: (x.get("decision") or {}).get("timeout_at") or ""):
        action = (task.get("decision") or {}).get("default_action") or decision_default_action()
        _apply_decision(state, task, action, "timeout-default",
                        note=f"限时未否决，按默认策略 {action} 执行")
        decided.append({"task_id": task["id"], "action": action,
                        "status": task["status"], "agent_id": task["agent"]})

    save_state(state)
    _trigger_workboard_mirror([s["task_id"] for s in started] +
                              [d["task_id"] for d in decided] +
                              [c["task_id"] for c in cancelled])
    update_board(state)
    return {
        "ok": True,
        "retry": bool(started),
        "started": started,
        "decided": decided,
        "cancelled": cancelled,
        "msg": (f"已启动 {len(started)} 个退避到期任务，"
                f"已按默认策略决策 {len(decided)} 个限时任务"
                + (f"，拒绝拉起已取消 {len(cancelled)} 个" if cancelled else "")),
    }


@revision_retry
def error_task(task_id, error_text):
    """Mark a task as non-retryable error.

    Use this for permission/config failures where another immediate attempt
    would not help and would make the board state misleading. Provider quota,
    rate limit, timeout, and 5xx failures should use fail_task so they can go
    through the board task-level retry.
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    agent_id = task["agent"]
    release_agent(state, agent_id)
    task["status"] = "error"
    task["last_error"] = error_text
    task["summary"] = f"不可自动重试（权限/配置类）：{error_text[:120]}"
    task["completed_at"] = now()
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {
        "ok": True,
        "retry": False,
        "msg": f"任务已标记失败：{task['title']}",
        "task_id": task_id,
        "agent_id": agent_id,
        "attempts": int(task.get("attempts") or 0),
        "max_attempts": int(task.get("max_attempts") or default_max_attempts()),
    }


CLEARABLE_STATUS = ("done", "error", "stopped")


# --- O8 blocked 态 ---------------------------------------------------------
# blocked = 「卡住等输入」（等用户确认/等上游产物），与「失败」（error）明确区分：
# 不是错误，也不烧 attempts，只是需要外部输入才能继续。看板显示 blocked 区
# 与卡住原因（blocked_reason），若卡在等上游产物可同时记 blockedTaskId
# （卡住来源任务 id）。unblock 恢复到挂起前状态（blocked_from）继续流转。

BLOCK_SUSPENDABLE_STATUS = ("running", "review", "pending")


@revision_retry
def block_task(task_id, reason, blocked_task_id=None):
    """O8：把任务挂起为 blocked（卡住等输入），agent 释放不占坑。

    reason 为卡住原因（必填）；blocked_task_id 可选：若卡住是等上游任务
    产物，记来源任务 id（blockedTaskId）。仅非终态活动任务可挂起。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if not (reason or "").strip():
        return {"ok": False, "msg": "卡住原因不能为空"}
    if task["status"] not in BLOCK_SUSPENDABLE_STATUS:
        return {"ok": False,
                "msg": f"当前状态 {task['status']} 不可挂起为 blocked"
                       f"（仅 {'/'.join(BLOCK_SUSPENDABLE_STATUS)}）"}
    if blocked_task_id and blocked_task_id not in state["tasks"]:
        return {"ok": False, "msg": f"blockedTaskId 不存在：{blocked_task_id}"}

    release_agent(state, task["agent"])
    task["blocked_from"] = task["status"]
    task["status"] = "blocked"
    task["blocked_reason"] = reason.strip()
    task["blockedTaskId"] = blocked_task_id or ""
    task["blocked_at"] = now()
    task["summary"] = f"卡住等输入：{task['blocked_reason'][:120]}"
    save_state(state)
    _trigger_workboard_mirror(
        [task_id],
        comment=(f"卡住等输入：{task['blocked_reason'][:120]}"
                 + (f"（等上游任务 {blocked_task_id}）" if blocked_task_id else "")))
    update_board(state)
    return {
        "ok": True,
        "task_id": task_id,
        "status": "blocked",
        "blockedTaskId": task["blockedTaskId"],
        "msg": (f"任务已挂起为 blocked（卡住等输入）：{task['title']}"
                f"；原因：{task['blocked_reason']}"
                + (f"；等上游任务：{blocked_task_id}" if blocked_task_id else "")),
    }


@revision_retry
def unblock_task(task_id):
    """O8：解除 blocked，恢复到挂起前状态继续流转。

    blocked_from 为 running/review 时重新占用 agent；其余回到 pending 排队。
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] != "blocked":
        return {"ok": False, "msg": f"任务不在 blocked 状态（当前：{task['status']}），无需 unblock"}
    prev = task.get("blocked_from") or "pending"
    if prev in ("running", "review"):
        assign_agent(state, task["agent"], task_id)
        task["status"] = prev
        task["last_activity_at"] = now()
    else:
        task["status"] = "pending"
    task["summary"] = f"已解除卡住（原原因：{(task.get('blocked_reason') or '')[:80]}），恢复为 {task['status']}"
    restored = task["status"]
    task["blocked_reason"] = ""
    task["blockedTaskId"] = ""
    task["blocked_from"] = ""
    task["blocked_at"] = None
    save_state(state)
    _trigger_workboard_mirror([task_id], comment=f"解除卡住，恢复为 {restored}")
    update_board(state)
    return {"ok": True, "task_id": task_id, "status": restored,
            "msg": f"已解除卡住，恢复为 {restored}：{task['title']}"}


# --- O7b audit/sweeper 完整版 ---------------------------------------------
# pipeline.py sweep：巡检五类异常 findings：
#   1. stale_running      running 超阈值无任何进展（last_activity_at/started_at）
#   2. pending_aged       pending 超龄未分配
#   3. retry_overdue      waiting_retry 超过 next_retry_at+宽限仍未被拉起
#   4. ledger_no_progress 有账无会话：run_id 已登记但长期无状态变化
#   5. cancelled_active   O9：已取消但子会话疑似仍活跃（有 run_id 无进展）
# findings 输出结构化 JSON + 人类可读摘要；--notify 经 feishu_card.py 发
# 告警卡（凭据缺失静默跳过）；幂等：同一 finding（type+task_id 指纹）
# 已告警过且未消除前不重复告警（sweep-state 记 last_notified 指纹）。

def sweep_config():
    return TEAM.get("sweep") or {}


def sweep_stale_running_seconds():
    return int(sweep_config().get("stale_running_seconds")
               or DEFAULT_SWEEP_STALE_RUNNING_SECONDS)


def sweep_pending_age_seconds():
    return int(sweep_config().get("pending_age_seconds")
               or DEFAULT_SWEEP_PENDING_AGE_SECONDS)


def sweep_waiting_retry_grace_seconds():
    return int(sweep_config().get("waiting_retry_grace_seconds")
               or DEFAULT_SWEEP_WAITING_RETRY_GRACE_SECONDS)


def sweep_cancelled_active_window_seconds():
    return int(sweep_config().get("cancelled_active_window_seconds")
               or DEFAULT_SWEEP_CANCELLED_ACTIVE_WINDOW_SECONDS)


def sweep_state_path():
    """O7b：巡检告警去重状态文件（与 task-state 同目录，按账号隔离）。"""
    return os.path.join(os.path.dirname(state_file()),
                        f"sweep-state-{CURRENT_ACCOUNT}.json")


def load_sweep_state():
    blank = {"version": 1, "account": CURRENT_ACCOUNT, "notified": {}}
    try:
        with open(sweep_state_path()) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return blank
        data.setdefault("notified", {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return blank


def save_sweep_state(sweep_state):
    path = sweep_state_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(sweep_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _task_progress_ts(task):
    """任务最近一次进展时间（epoch 秒）：last_activity_at 优先，回退 started_at/created_at。"""
    for key in ("last_activity_at", "started_at", "created_at"):
        ts = _parse_ts(task.get(key))
        if ts is not None:
            return ts
    return None


def collect_findings(state, now_ts=None):
    """O7b/O9：扫描台账产出 findings（纯计算，不改台账）。

    四类常规 findings（stale_running/pending_aged/retry_overdue/
    ledger_no_progress）+ O9 一类：cancelled_active（已取消但子会话
    疑似仍活跃：stop 且取消意图在、run_id 已登记、取消后窗口内无任何进展）。
    """
    now_ts = now_ts if now_ts is not None else time.time()
    stale_s = sweep_stale_running_seconds()
    pending_s = sweep_pending_age_seconds()
    grace_s = sweep_waiting_retry_grace_seconds()
    cancel_window_s = sweep_cancelled_active_window_seconds()
    findings = []

    def add(ftype, task, detail, age):
        findings.append({
            "type": ftype,
            "task_id": task["id"],
            "title": task.get("title") or "",
            "status": task.get("status"),
            "agent": task.get("agent") or "",
            "detail": detail,
            "age_seconds": int(age),
            "fingerprint": f"{ftype}:{task['id']}",
        })

    for task in state.get("tasks", {}).values():
        status = task.get("status")
        if status == "running":
            ts = _task_progress_ts(task)
            age = now_ts - ts if ts else 0
            if task.get("run_id"):
                # 有账（run_id 已登记）无会话进展：子会话可能已死
                if age >= stale_s:
                    add("ledger_no_progress", task,
                        f"run_id 已登记但 {int(age // 3600)}h{(int(age % 3600)) // 60}m 无进展，子会话可能已死",
                        age)
            elif age >= stale_s:
                add("stale_running", task,
                    f"running 超阈值 {stale_s}s 无 record-run/set-result 进展",
                    age)
        elif status == "pending":
            ts = _parse_ts(task.get("created_at"))
            age = now_ts - ts if ts else 0
            if age >= pending_s:
                add("pending_aged", task,
                    f"pending 超龄 {int(age // 3600)}h{int(age % 3600) // 60}m 未分配",
                    age)
        elif status == "waiting_retry":
            due_ts = _parse_ts(task.get("next_retry_at"))
            if due_ts is not None and now_ts >= due_ts + grace_s:
                add("retry_overdue", task,
                    f"next_retry_at={task.get('next_retry_at')} 已过宽限期仍未被 retry-due 拉起",
                    now_ts - due_ts)
        elif status == "stopped" and task.get("cancel_requested") and task.get("run_id"):
            # O9：已取消但子会话疑似仍活跃——run_id 已登记（说明子会话被拉起过），
            # 取消后窗口内仍无任何后续进展（set-result/complete），提醒人工确认
            # 子会话是否真的已停。超过关注窗口后不再提醒（大概率已自然结束）。
            cancel_ts = _parse_ts(task.get("cancel_requested_at"))
            if cancel_ts is not None and 0 <= now_ts - cancel_ts <= cancel_window_s:
                add("cancelled_active", task,
                    f"已取消（{task.get('cancel_requested_at')}）但 run_id 已登记且无后续进展，"
                    f"子会话可能仍在活跃，请确认是否需手动终止",
                    now_ts - cancel_ts)
    return findings


def _send_sweep_alert_card(findings):
    """O7b：巡检告警卡经 feishu_card.py 发送；凭据/收件人缺失静默返回 False。"""
    try:
        real_dir = os.path.dirname(os.path.realpath(__file__))
        if real_dir not in sys.path:
            sys.path.insert(0, real_dir)
        import feishu_card
        open_id = feishu_card.resolve_open_id(None)
        if not open_id:
            return False
        token = feishu_card.get_token()
        card = feishu_card.build_alert_card({
            "account": CURRENT_ACCOUNT,
            "engineer": engineer_name(),
            "findings": findings,
            "generated_at": now(),
        })
        result = feishu_card.send_card(open_id, card, token)
        return isinstance(result, dict) and result.get("code") == 0
    except Exception:
        return False


def sweep_tasks(notify=False, now_ts=None):
    """O7b：巡检命令入口。返回结构化 findings + 人类可读摘要。

    幂等去重：sweep-state 记录已告警指纹（type+task_id）与 last_notified；
    同一 finding 未消除前不重复告警；任务恢复正常（不再命中）后自动清除
    指纹，下次再犯可重新告警。
    """
    state = load_state()
    findings = collect_findings(state, now_ts=now_ts)
    sweep_state = load_sweep_state()
    notified = sweep_state.get("notified", {})

    # 清理已消除的指纹（任务不再命中），保证复发可再告警
    current_fps = {f["fingerprint"] for f in findings}
    for fp in list(notified.keys()):
        if fp not in current_fps:
            del notified[fp]

    new_findings = [f for f in findings if f["fingerprint"] not in notified]
    notified_sent = False
    if notify and new_findings:
        notified_sent = _send_sweep_alert_card(new_findings)
        if notified_sent:
            for f in new_findings:
                notified[f["fingerprint"]] = {
                    "task_id": f["task_id"],
                    "type": f["type"],
                    "last_notified": now(),
                }
    elif notify and not new_findings:
        # 有存量 finding 但均已告警过：幂等不重复发
        notified_sent = False
    sweep_state["notified"] = notified
    save_sweep_state(sweep_state)

    # 人类可读摘要
    summary_lines = [f"[sweep account={CURRENT_ACCOUNT}] findings={len(findings)} 新增待告警={len(new_findings)}"]
    for f in findings:
        marker = "新" if f["fingerprint"] in {nf["fingerprint"] for nf in new_findings} else "已告警"
        summary_lines.append(
            f"  [{f['type']}] {f['task_id']} ({f['status']}/{f['agent']}) {f['detail']} [{marker}]")
    if not findings:
        summary_lines.append("  无异常，台账健康")

    return {
        "ok": True,
        "account": CURRENT_ACCOUNT,
        "findings": findings,
        "new_findings": new_findings,
        "notified": notified_sent,
        "summary": "\n".join(summary_lines),
    }



@revision_retry
def clear_task(task_id):
    """Remove a terminal-state task from the board.

    Only done / error / stopped tasks can be cleared; running / review /
    pending tasks are rejected (stop or complete them first). The cleared
    task is archived into state["history"] by default so `history` keeps
    showing it, while the board and `detail` no longer list it.
    """
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task["status"] not in CLEARABLE_STATUS:
        return {
            "ok": False,
            "msg": f"仅终态任务（done/error/stopped）可清除，当前状态：{task['status']}，请先 stop/complete",
        }
    release_agent(state, task["agent"])
    # 默认保留 history 记录：归档快照后从 tasks 移除。
    snapshot = dict(task)
    snapshot["cleared_at"] = now()
    state["history"].append(snapshot)
    del state["tasks"][task_id]
    save_state(state)
    # 台账已移除该任务：镜像层将归档孤儿卡（clear 归档语义，见设计文档）
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"已清除（history 保留记录）：{task['title']}", "task_id": task_id}


@revision_retry
def release_agent_cmd(agent_id):
    state = load_state()
    if agent_id not in AGENTS:
        return {"ok": False, "msg": f"未知 agent：{agent_id}"}
    current = list(state["agents"][agent_id].get("current_tasks", []))
    for task_id in current:
        task = state["tasks"].get(task_id)
        if task and task["status"] == "running":
            # 与 stop 语义一致：release 停掉的任务同样持久化取消意图（O9）
            task["stopped_from"] = task["status"]
            task["status"] = "stopped"
            task["completed_at"] = now()
            task["cancel_requested"] = True
            task["cancel_requested_at"] = now()
    release_agent(state, agent_id)
    save_state(state)
    _trigger_workboard_mirror(current)
    update_board(state)
    return {"ok": True, "msg": f"{AGENTS[agent_id]['name']} 已释放"}


BOARD_SECTION_LIMIT = 3


def brief(title, limit=26):
    """Shorten a task title for overview display."""
    title = (title or "").strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def build_board(state):
    lines = []

    # Agent status
    lines.append("**👤 Agent 状态**")
    for aid, info in AGENTS.items():
        a = state["agents"].get(aid, {})
        status = a.get("status", "idle")
        icon = STATUS_ICON.get(status, "⏸")
        label = STATUS_LABEL.get(status, status)
        n = len(a.get("current_tasks", []))
        detail = f" · {n}个任务" if n > 1 else ""
        lines.append(f"{info['icon']} **{info['name']}** {icon} {label}{detail}")

    # Active tasks (overview only: short titles, capped at BOARD_SECTION_LIMIT)
    active = [t for t in state["tasks"].values()
              if t["status"] in ("running", "review", "pending", "error",
                                 "waiting_retry", "waiting_decision", "blocked")]
    running = [t for t in active if t["status"] == "running"]
    review = [t for t in active if t["status"] == "review"]
    pending = [t for t in active if t["status"] == "pending"]
    errors = [t for t in active if t["status"] == "error"]
    waiting = [t for t in active if t["status"] == "waiting_retry"]
    decisions = [t for t in active if t["status"] == "waiting_decision"]
    blocked = [t for t in active if t["status"] == "blocked"]

    def render_section(title, tasks, key=None, with_id=False):
        lines.append(f"\n**{title}**")
        if not tasks:
            lines.append("无")
            return
        if key:
            tasks = sorted(tasks, key=key, reverse=True)
        shown = tasks[:BOARD_SECTION_LIMIT]
        for t in shown:
            icon = AGENTS[t["agent"]]["icon"]
            retry = ""
            if with_id:
                retry = f" · 尝试 {t.get('attempts', 0)}/{t.get('max_attempts', default_max_attempts())}"
            lines.append(f"{icon} {brief(t['title'])} · `{t['id']}`{retry}")
        if len(tasks) > BOARD_SECTION_LIMIT:
            lines.append(f"… 共 {len(tasks)} 条")

    def render_waiting_section(tasks):
        """O2：waiting_retry 任务显示预计重试时间，不显示误导性失败计数。"""
        lines.append("\n**⏰ 等待重试（瞬时失败退避）**")
        if not tasks:
            lines.append("无")
            return
        shown = sorted(tasks, key=lambda x: x.get("next_retry_at") or "")[:BOARD_SECTION_LIMIT]
        for t in shown:
            icon = AGENTS[t["agent"]]["icon"]
            lines.append(f"{icon} {brief(t['title'])} · `{t['id']}` · 预计重试 {t.get('next_retry_at') or '未知'}")
        if len(tasks) > BOARD_SECTION_LIMIT:
            lines.append(f"… 共 {len(tasks)} 条")

    def render_decision_section(tasks):
        """O6：waiting_decision 待决事项区，显示问题摘要与限时。"""
        lines.append("\n**🗳️ 待决事项（等待用户决策）**")
        if not tasks:
            lines.append("无")
            return
        shown = sorted(
            tasks,
            key=lambda x: (x.get("decision") or {}).get("timeout_at") or "")[:BOARD_SECTION_LIMIT]
        for t in shown:
            icon = AGENTS[t["agent"]]["icon"]
            d = t.get("decision") or {}
            lines.append(
                f"{icon} {brief(t['title'])} · `{t['id']}`"
                f" · 限时 {d.get('timeout_at') or '未知'}"
                f" · 未决默认 {d.get('default_action') or '-'}")
        if len(tasks) > BOARD_SECTION_LIMIT:
            lines.append(f"… 共 {len(tasks)} 条")

    def render_blocked_section(tasks):
        """O8：blocked 卡住等输入区，显示卡住原因与 blockedTaskId（若等上游产物）。

        与 error（失败）明确区分：blocked 不是错误，只是需要外部输入才能继续。
        """
        lines.append("\n**🚧 卡住等输入（blocked）**")
        if not tasks:
            lines.append("无")
            return
        shown = sorted(tasks, key=lambda x: x.get("blocked_at") or "")[:BOARD_SECTION_LIMIT]
        for t in shown:
            icon = AGENTS[t["agent"]]["icon"]
            extra = ""
            if t.get("blockedTaskId"):
                extra = f" · 等上游 `{t['blockedTaskId']}`"
            lines.append(
                f"{icon} {brief(t['title'])} · `{t['id']}`"
                f" · {(t.get('blocked_reason') or '未知原因')[:40]}{extra}")
        if len(tasks) > BOARD_SECTION_LIMIT:
            lines.append(f"… 共 {len(tasks)} 条")

    render_section("🔄 进行中", running,
                   key=lambda x: x.get("started_at") or x.get("created_at") or "", with_id=True)
    render_blocked_section(blocked)
    render_decision_section(decisions)
    render_waiting_section(waiting)
    render_section("❌ 失败", errors,
                   key=lambda x: x.get("completed_at") or x.get("created_at") or "", with_id=True)
    render_section("👀 待审核", review,
                   key=lambda x: x.get("completed_at") or x.get("created_at") or "")
    render_section("⏳ 待分配", pending,
                   key=lambda x: x.get("created_at") or "")

    # Done summary (last 3)
    done = [t for t in state["tasks"].values() if t["status"] == "done"]
    render_section("✅ 最近完成", done,
                   key=lambda x: x.get("completed_at") or "")

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                make_button("🔄 刷新", "default", "!board"),
                make_button("📋 新流水线", "primary", "!pipeline start"),
                make_button("🧑‍💻 派活", "primary", "!assign"),
                make_button("📜 历史", "default", "!history"),
            ],
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                make_button(f"{info['icon']}{info['name']}", "default", f"!agent-detail {aid}")
                for aid, info in AGENTS.items()
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": board_title()}, "template": "indigo"},
        "elements": elements,
    }


def _save_board_message_id(message_id):
    """把 board_message_id 落盘（O1：自带冲突重读重试，不拖累主命令）。"""
    for _ in range(REVISION_MAX_RETRIES):
        state = load_state()
        if state.get("board_message_id") == message_id:
            return
        state["board_message_id"] = message_id
        try:
            save_state(state)
            return
        except RevisionConflictError:
            continue
    # 重试耗尽：卡片已发出，仅 message_id 未记账，下次 board 会新发一张，可接受


def update_board(state, force_new=False):
    token = get_token()
    card = build_board(state)
    mid = state.get("board_message_id") if not force_new else None
    result = send_card(card, token, mid)
    if result.get("code") == 0:
        state["board_message_id"] = result["data"].get("message_id") or mid
        _save_board_message_id(state["board_message_id"])
    else:
        return result

    # Feishu card PATCH updates occasionally lag/cache on the client side,
    # leaving stale content visible even though the update succeeded
    # server-side. Re-push once after a short delay as a cheap safeguard
    # instead of relying on a second manual refresh.
    try:
        time.sleep(0.6)
        send_card(card, token, state.get("board_message_id"))
    except Exception:
        pass
    return result


def get_live_models():
    """Fetch real-time model per agent from `openclaw agents list --json`."""
    try:
        result = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        return {a.get("id"): a.get("model", "?") for a in data}
    except Exception:
        return {}


def build_agent_card(state, agent_id):
    """Build the per-agent detail card (second-level view overwriting the board)."""
    info = AGENTS[agent_id]
    a = state["agents"].get(agent_id, {})
    status = a.get("status", "idle")
    models = get_live_models()
    live_model = models.get(agent_id)

    lines = []
    lines.append(f"**状态** {STATUS_ICON.get(status, '⏸')} {STATUS_LABEL.get(status, status)}")
    if live_model:
        lines.append(f"**Model（实时）** `{live_model}`")
    else:
        lines.append(f"**Model（实时）** 获取失败，配置 hint: `{info.get('model_hint', '未知')}`")
    hint = info.get("model_hint")
    if hint and live_model and hint != live_model:
        lines.append(f"⚠️ 配置 hint `{hint}` 与实际运行不一致")
    lines.append(f"**OpenClaw agent id** `{agent_id}`")

    running = [t for t in state["tasks"].values()
               if t["agent"] == agent_id and t["status"] == "running"]
    waiting = [t for t in state["tasks"].values()
               if t["agent"] == agent_id and t["status"] == "waiting_retry"]
    decisions = [t for t in state["tasks"].values()
                 if t["agent"] == agent_id and t["status"] == "waiting_decision"]
    blocked = [t for t in state["tasks"].values()
               if t["agent"] == agent_id and t["status"] == "blocked"]
    lines.append("\n**🔄 正在处理**")
    if running:
        for t in running:
            lines.append(f"**{t['title']}** · `{t['id']}`")
            if t.get("started_at"):
                lines.append(f"   └ 开始 {t['started_at']} · 尝试 {t.get('attempts', 0)}/{t.get('max_attempts', default_max_attempts())}")
            if t.get("run_id"):
                lines.append(f"   └ run `{t['run_id'][:16]}…`")
            if t.get("summary"):
                lines.append(f"   └ {t['summary'][:80]}")
    else:
        lines.append("无")

    lines.append("\n**🚧 卡住等输入（blocked）**")
    if blocked:
        for t in blocked:
            lines.append(f"**{t['title']}** · `{t['id']}`")
            lines.append(f"   └ 原因：{(t.get('blocked_reason') or '未知')[:80]}")
            if t.get("blockedTaskId"):
                lines.append(f"   └ 等上游任务 `{t['blockedTaskId']}`")
            if t.get("blocked_at"):
                lines.append(f"   └ 卡住于 {t['blocked_at']}")
    else:
        lines.append("无")

    lines.append("\n**🗳️ 待决事项（等待用户决策）**")
    if decisions:
        for t in decisions:
            d = t.get("decision") or {}
            lines.append(f"**{t['title']}** · `{t['id']}`")
            lines.append(f"   └ 问题：{(d.get('question') or '')[:80]}")
            lines.append(f"   └ 限时 {d.get('timeout_at') or '未知'} · 未决默认 {d.get('default_action') or '-'}")
    else:
        lines.append("无")

    lines.append("\n**⏰ 等待重试（瞬时失败退避）**")
    if waiting:
        for t in waiting:
            lines.append(f"**{t['title']}** · `{t['id']}`")
            lines.append(f"   └ 预计重试 {t.get('next_retry_at') or '未知'}")
            if t.get("last_error"):
                lines.append(f"   └ 上次错误：{t['last_error'][:80]}")
    else:
        lines.append("无")

    hist = [t for t in state["tasks"].values()
            if t["agent"] == agent_id and t["status"] in ("done", "stopped", "error", "review")]
    hist.sort(key=lambda x: x.get("completed_at") or x.get("started_at") or x.get("created_at") or "",
              reverse=True)
    shown = hist[:10]
    lines.append(f"\n**📜 已处理（最近 {len(shown)} 条）**")
    if shown:
        for t in shown:
            icon = STATUS_ICON.get(t["status"], "")
            ts = t.get("completed_at") or t.get("started_at") or t.get("created_at") or ""
            lines.append(f"{icon} {t['title']} · {ts}")
            if t.get("summary"):
                lines.append(f"   └ {t['summary'][:60]}")
    else:
        lines.append("无")

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                make_button("🔄 刷新", "default", f"!agent-detail {agent_id}"),
                make_button("← 返回看板", "primary", "!board"),
            ],
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"{info['icon']} {info['name']} · Agent详情"}, "template": "wathet"},
        "elements": elements,
    }


def resolve_agent_id(text):
    text = (text or "").strip()
    if text in AGENTS:
        return text
    return detect_agent(text)


def show_agent_detail(agent_id):
    """Overwrite the board card in place with the per-agent detail card."""
    resolved = resolve_agent_id(agent_id)
    if not resolved:
        return {"ok": False, "msg": f"未知 agent：{agent_id}"}
    state = load_state()
    token = get_token()
    card = build_agent_card(state, resolved)
    mid = state.get("board_message_id")
    result = send_card(card, token, mid)
    if result.get("code") == 0:
        state["board_message_id"] = result["data"].get("message_id") or mid
        _save_board_message_id(state["board_message_id"])
    else:
        return result
    # Same PATCH-cache safeguard as update_board.
    try:
        time.sleep(0.6)
        send_card(card, token, state.get("board_message_id"))
    except Exception:
        pass
    return result


def show_board():
    state = load_state()
    # Keep the multi-Agent board stable: refresh/update the recorded card
    # instead of creating a new card every time. This lets users pin the board
    # once and use the card's refresh button to see current Agent state.
    return update_board(state, force_new=False)


def get_task_detail(task_id):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    return {"ok": True, "task": task}


def show_history():
    state = load_state()
    history = [t for t in state["tasks"].values() if t["status"] in ("done", "stopped")]
    # clear 命令归档的任务快照同样计入历史。
    for h in state.get("history", []):
        if isinstance(h, dict) and h.get("id"):
            history.append(h)
    history.sort(
        key=lambda x: x.get("completed_at") or x.get("created_at") or "",
        reverse=True,
    )
    return {"ok": True, "history": history}


def detect_agent(text):
    """Map natural language to agent id."""
    text = text.lower()
    for aid, info in AGENTS.items():
        for alias in info["aliases"]:
            if alias.lower() in text:
                return aid
    return None


def dispatch_natural(text):
    """Parse natural language request like '技术评审帮我评审下...' and assign."""
    agent_id = detect_agent(text)
    if not agent_id:
        return {"ok": False, "msg": "没听出来要派给谁。可以说：技术评审、编码开发、测试、需求分析。"}
    # Remove the agent keyword from the title to keep it clean.
    title = text
    for alias in AGENTS[agent_id]["aliases"]:
        title = title.replace(alias, "")
    title = title.strip("，,.。 \t帮我请麻烦")
    if not title:
        return {"ok": False, "msg": "请补充一下具体要做什么。"}
    return add_adhoc_task(agent_id, title)


def main():
    if len(sys.argv) < 2:
        print("Usage: BOARD_ACCOUNT=<bot01|bot02|...> [BOARD_OPEN_ID=<open_id>] pipeline.py <board|agent-detail|start|assign|dispatch|complete|review|stop|unstop|release|set-result|record-run|fail|error|block|unblock|retry-due|request-decision|decide|sweep|detail|history|clear> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "board":
        result = show_board()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "agent-detail":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py agent-detail <agent_id>")
            sys.exit(1)
        result = show_agent_detail(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "start":
        title = " ".join(sys.argv[2:]) or "未命名任务"
        result = start_pipeline(title)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "assign":
        if len(sys.argv) < 4:
            print("Usage: pipeline.py assign <agent_id> <title>")
            sys.exit(1)
        result = add_adhoc_task(sys.argv[2], " ".join(sys.argv[3:]))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "dispatch":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not text:
            print("Usage: pipeline.py dispatch <natural language>")
            sys.exit(1)
        result = dispatch_natural(text)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "complete":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py complete <task_id>")
            sys.exit(1)
        result = complete_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "review":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py review <task_id>")
            sys.exit(1)
        result = review_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "stop":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py stop <task_id>")
            sys.exit(1)
        result = stop_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "unstop":
        # O9：清除取消意图（用户反悔），stopped 任务恢复为停止前状态
        if len(sys.argv) < 3:
            print("Usage: pipeline.py unstop <task_id>")
            sys.exit(1)
        result = unstop_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "release":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py release <agent_id>")
            sys.exit(1)
        result = release_agent_cmd(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "set-result":
        if len(sys.argv) < 4:
            print("Usage: pipeline.py set-result <task_id> <result_text> [summary]")
            sys.exit(1)
        result = set_task_result(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "record-run":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py record-run <task_id> [run_id] [child_session_key]")
            sys.exit(1)
        result = record_task_run(
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else None,
            sys.argv[4] if len(sys.argv) > 4 else None,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "fail":
        if len(sys.argv) < 4:
            print("Usage: pipeline.py fail <task_id> <error_text>")
            sys.exit(1)
        result = fail_task(sys.argv[2], " ".join(sys.argv[3:]))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "error":
        if len(sys.argv) < 4:
            print("Usage: pipeline.py error <task_id> <error_text>")
            sys.exit(1)
        result = error_task(sys.argv[2], " ".join(sys.argv[3:]))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "block":
        # O8：把任务挂起为 blocked（卡住等输入）
        if len(sys.argv) < 4:
            print("Usage: pipeline.py block <task_id> <卡住原因> [--blocked-task-id <上游任务id>]")
            sys.exit(1)
        blocked_task_id = None
        if "--blocked-task-id" in sys.argv:
            idx = sys.argv.index("--blocked-task-id")
            if idx + 1 < len(sys.argv):
                blocked_task_id = sys.argv[idx + 1]
                sys.argv = sys.argv[:idx] + sys.argv[idx + 2:]
        reason = " ".join(sys.argv[3:])
        result = block_task(sys.argv[2], reason, blocked_task_id=blocked_task_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "unblock":
        # O8：解除 blocked，恢复流转
        if len(sys.argv) < 3:
            print("Usage: pipeline.py unblock <task_id>")
            sys.exit(1)
        result = unblock_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "retry-due":
        # O2/O6：退避到期 + 决策限时扫描（sweep），由 cron 定期驱动；幂等，无参
        result = retry_due_tasks()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "request-decision":
        # O6：挂起任务等待用户决策（决策卡经 feishu_card.py 发送）
        if len(sys.argv) < 4:
            print("Usage: pipeline.py request-decision <task_id> <question> "
                  "[--options a,b,c] [--tier low|medium|high] "
                  "[--default approve|reject] [--timeout seconds]")
            sys.exit(1)
        task_id = sys.argv[2]
        question = sys.argv[3]
        opts = []
        tier = None
        default_action = None
        timeout_seconds = None
        i = 4
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--options" and i + 1 < len(sys.argv):
                opts = [x.strip() for x in sys.argv[i + 1].split(",") if x.strip()]
                i += 2
            elif arg == "--tier" and i + 1 < len(sys.argv):
                tier = sys.argv[i + 1]
                i += 2
            elif arg == "--default" and i + 1 < len(sys.argv):
                default_action = sys.argv[i + 1]
                i += 2
            elif arg == "--timeout" and i + 1 < len(sys.argv):
                try:
                    timeout_seconds = int(sys.argv[i + 1])
                except ValueError:
                    print("Usage: --timeout 必须是整数秒")
                    sys.exit(1)
                i += 2
            else:
                print(f"未知参数：{arg}")
                sys.exit(1)
        result = request_decision(task_id, question, options=opts, tier=tier,
                                  default_action=default_action,
                                  timeout_seconds=timeout_seconds)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "decide":
        # O6：用户拍板入口（卡片回调与手工命令共用）
        if len(sys.argv) < 4:
            print("Usage: pipeline.py decide <task_id> approve|reject|defer [--source <来源>]")
            sys.exit(1)
        source = "cli"
        if "--source" in sys.argv:
            idx = sys.argv.index("--source")
            if idx + 1 < len(sys.argv):
                source = sys.argv[idx + 1]
                sys.argv = sys.argv[:idx] + sys.argv[idx + 2:]
        result = decide_task(sys.argv[2], sys.argv[3], source=source)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "sweep":
        # O7b：巡检产出 findings（四类）；--notify 经 feishu_card.py 发告警卡
        notify = "--notify" in sys.argv
        result = sweep_tasks(notify=notify)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(result["summary"], file=sys.stderr)
    elif cmd == "detail":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py detail <task_id>")
            sys.exit(1)
        result = get_task_detail(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "history":
        result = show_history()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "clear":
        if len(sys.argv) < 3:
            print("Usage: pipeline.py clear <task_id>")
            sys.exit(1)
        result = clear_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
