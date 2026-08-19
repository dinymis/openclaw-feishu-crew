#!/usr/bin/env python3
"""Multi-agent parallel task manager.

Supports both full pipeline tasks (4 stages) and ad-hoc assignments
to individual agents. Each agent can hold one task at a time.
"""

__version__ = "2.1.0"

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
    if task["status"] != "pending":
        return False, f"任务状态不是待分配：{task['status']}"
    agent_id = task["agent"]
    if not agent_is_available(state, agent_id):
        return False, f"{AGENTS[agent_id]['name']} 正忙"
    assign_agent(state, agent_id, task_id)
    task["status"] = "running"
    task["started_at"] = now()
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
    task["status"] = "stopped"
    task["completed_at"] = now()
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"已停止：{task['title']}"}


@revision_retry
def set_task_result(task_id, result_text, summary=None):
    state = load_state()
    task = state["tasks"].get(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    task["result"] = result_text
    task["summary"] = summary or result_text[:200].strip()
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
    if run_id:
        task["run_id"] = run_id
    if child_session_key:
        task["child_session_key"] = child_session_key
    save_state(state)
    _trigger_workboard_mirror([task_id])
    update_board(state)
    return {"ok": True, "msg": f"运行信息已记录：{task['title']}"}


def retry_backoff_seconds():
    """O2：瞬时错误退避时长（秒），team.json retry_backoff_seconds 可配，默认 1800。"""
    return int(TEAM.get("retry_backoff_seconds") or DEFAULT_RETRY_BACKOFF_SECONDS)


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
    """O2：扫描到点的 waiting_retry 任务并真启动（sweep，幂等）。

    驱动端由 cron 定期调用（如每分钟）。仅当 next_retry_at <= 当前时间
    时启动：任务转 running，attempts 此时才 +1（账实相符：账上 +1
    即代表真启动了一次）。返回本次启动的任务列表，由协调猴/调用方
    负责 sessions_spawn 拉起子会话。幂等：未到点/无 waiting 任务时
    返回空列表，不写状态。
    """
    state = load_state()
    now_str = now()
    due = [t for t in state["tasks"].values()
           if t.get("status") == "waiting_retry"
           and (t.get("next_retry_at") or "") <= now_str]
    if not due:
        return {"ok": True, "started": [], "msg": "无到点重试任务"}

    started = []
    for task in sorted(due, key=lambda x: x.get("next_retry_at") or ""):
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
    save_state(state)
    _trigger_workboard_mirror([s["task_id"] for s in started])
    update_board(state)
    return {
        "ok": True,
        "retry": bool(started),
        "started": started,
        "msg": f"已启动 {len(started)} 个退避到期任务",
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
            task["status"] = "stopped"
            task["completed_at"] = now()
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
              if t["status"] in ("running", "review", "pending", "error", "waiting_retry")]
    running = [t for t in active if t["status"] == "running"]
    review = [t for t in active if t["status"] == "review"]
    pending = [t for t in active if t["status"] == "pending"]
    errors = [t for t in active if t["status"] == "error"]
    waiting = [t for t in active if t["status"] == "waiting_retry"]

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

    render_section("🔄 进行中", running,
                   key=lambda x: x.get("started_at") or x.get("created_at") or "", with_id=True)
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
        print("Usage: BOARD_ACCOUNT=<bot01|bot02|...> [BOARD_OPEN_ID=<open_id>] pipeline.py <board|agent-detail|start|assign|dispatch|complete|review|stop|release|set-result|record-run|fail|error|retry-due|detail|history|clear> [args]")
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
    elif cmd == "retry-due":
        # O2：退避到期扫描（sweep），由 cron 定期驱动；幂等，无参
        result = retry_due_tasks()
        print(json.dumps(result, indent=2, ensure_ascii=False))
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
