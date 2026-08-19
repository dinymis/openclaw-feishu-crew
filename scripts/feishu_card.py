#!/usr/bin/env python3
"""通用飞书卡片发送器（Agent 管理控制台卡片）。

职责：
1. 渲染「Agent 管理控制台」看板卡片（已注册 Agent 列表 + 流水线阶段状态 + 操作按钮）；
2. 发送新卡片，或更新（update）一张已存在的卡片。

配置分层（与 scripts/pipeline.py 保持一致，代码内不含任何业务敏感值）：
  凭据优先级：FEISHU_APP_ID/FEISHU_APP_SECRET 环境变量
            > config/accounts/<account_id>.json
            > OpenClaw openclaw.json（兜底）
  open_id 优先级：命令行参数 > BOARD_OPEN_ID 环境变量 > config/team.json accounts 表
  openclaw.json 路径：OPENCLAW_CONFIG_PATH 环境变量 > team.json openclaw_config_path
                     > ~/.openclaw/openclaw.json
  配置目录：PIPELINE_CONFIG_DIR 环境变量 > <仓库根>/config

用法：
  # 发送新卡片（open_id 为接收者在该 bot 应用下的 open_id，open_id 是 per-app 的）
  BOARD_ACCOUNT=bot01 python3 scripts/feishu_card.py <open_id> [pipeline_state_json]

  # 更新一张已存在的卡片（message_id 为飞书消息 id）
  BOARD_ACCOUNT=bot01 python3 scripts/feishu_card.py --update <message_id> [pipeline_state_json]

  # --account <id> 可显式指定账号（等价于 BOARD_ACCOUNT）

pipeline_state_json 可选，形如：
  {"stages": [{"name": "需求分析", "status": "running", "agent": "requirement-analyst"}, ...]}
status 取值：done / running / idle / error。缺省时渲染全 idle 的默认五阶段流水线。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

FEISHU_API = "https://open.feishu.cn/open-apis"
DEFAULT_OPENCLAW_CONFIG_PATH = "~/.openclaw/openclaw.json"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.environ.get("PIPELINE_CONFIG_DIR") or os.path.join(WORKSPACE, "config")
TEAM_CONFIG_PATH = os.path.join(CONFIG_DIR, "team.json")
ACCOUNTS_CONFIG_DIR = os.path.join(CONFIG_DIR, "accounts")

# 默认五阶段流水线（与 pipeline.py PIPELINE_STAGES 保持一致）。
DEFAULT_STAGES = [
    {"name": "需求分析", "status": "idle", "agent": "requirement-analyst"},
    {"name": "技术评审", "status": "idle", "agent": "architect"},
    {"name": "编码开发", "status": "idle", "agent": "coder"},
    {"name": "代码评审", "status": "idle", "agent": "code-reviewer"},
    {"name": "测试", "status": "idle", "agent": "tester"},
]


def load_team_config():
    """加载 team.json；缺失或格式错误时返回空 dict（走代码默认值）。"""
    try:
        with open(TEAM_CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


TEAM = load_team_config()
ACCOUNTS = TEAM.get("accounts") or {}
CURRENT_ACCOUNT = os.environ.get("BOARD_ACCOUNT") or TEAM.get("default_account") or "bot01"


def account_config():
    return ACCOUNTS.get(CURRENT_ACCOUNT) or {}


def engineer_name():
    """工程师昵称：来自 team.json accounts 表（配置文件注入，代码不写死任何人名）。"""
    return account_config().get("engineer") or CURRENT_ACCOUNT


def resolve_open_id(cli_open_id):
    """open_id 解析：命令行参数 > BOARD_OPEN_ID 环境变量 > team.json accounts 表。"""
    return (cli_open_id
            or os.environ.get("BOARD_OPEN_ID")
            or account_config().get("open_id"))


def openclaw_config_path():
    path = (os.environ.get("OPENCLAW_CONFIG_PATH")
            or TEAM.get("openclaw_config_path")
            or DEFAULT_OPENCLAW_CONFIG_PATH)
    return os.path.expanduser(path)


def resolve_feishu_credentials():
    """飞书凭据解析（优先级见模块 docstring），与 pipeline.py 行为一致。"""
    env_id = os.environ.get("FEISHU_APP_ID")
    env_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_id and env_secret:
        return {"appId": env_id, "appSecret": env_secret}

    path = os.path.join(ACCOUNTS_CONFIG_DIR, f"{CURRENT_ACCOUNT}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            app_id = data.get("app_id") or data.get("appId")
            app_secret = data.get("app_secret") or data.get("appSecret")
            if app_id and app_secret:
                return {"appId": app_id, "appSecret": app_secret}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    with open(openclaw_config_path()) as f:
        config = json.load(f)
    feishu = config["channels"]["feishu"]
    accounts = feishu.get("accounts", {})
    acct = accounts.get(CURRENT_ACCOUNT)
    if acct and "appId" in acct and "appSecret" in acct:
        return acct
    if "appId" in feishu and "appSecret" in feishu:
        return feishu
    acct = accounts.get(feishu.get("defaultAccount")) or next(iter(accounts.values()), None)
    if not acct or "appId" not in acct or "appSecret" not in acct:
        raise RuntimeError(
            "feishu credentials not found (checked env, config/accounts/, openclaw.json)")
    return acct


def get_token():
    creds = resolve_feishu_credentials()
    url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": creds["appId"], "app_secret": creds["appSecret"]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise RuntimeError(f"Failed to get token: {result}")
    return result["tenant_access_token"]


def get_agent_statuses():
    """从 `openclaw agents list --json` 获取已注册 Agent 列表（失败时返回空列表）。"""
    import subprocess
    try:
        result = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            capture_output=True, text=True, timeout=15
        )
        agents = []
        data = json.loads(result.stdout)
        for a in data:
            agents.append({
                "id": a.get("id", "?"),
                "model": a.get("model", "?"),
                "workspace": a.get("workspace", "?"),
                "bindings": a.get("bindings", 0),
                "isDefault": a.get("isDefault", False),
            })
        return agents
    except Exception:
        return []


def make_button(text, btn_type, command):
    """Create a schema 2.0 button with callback behavior."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "width": "fill",
        "behaviors": [
            {
                "type": "callback",
                "value": {"text": command},
            }
        ],
    }


def dashboard_title():
    """卡片标题：team.json card.dashboard_title 模板可定制，{engineer} 会被替换。"""
    tpl = (TEAM.get("card") or {}).get("dashboard_title") or "🤖 Agent 管理控制台"
    try:
        return tpl.format(engineer=engineer_name())
    except (KeyError, IndexError, ValueError):
        return tpl


def build_dashboard_card(agents, pipeline_state=None):
    """渲染看板卡片：已注册 Agent + 流水线阶段状态 + 操作按钮。"""
    stages = (pipeline_state or {}).get("stages") or DEFAULT_STAGES

    status_icons = {"done": "✅", "running": "🔄", "idle": "⏸", "error": "❌"}
    status_labels = {"done": "完成", "running": "进行中", "idle": "待执行", "error": "失败"}

    agent_lines = []
    for a in agents:
        default_mark = " ⭐" if a.get("isDefault") else ""
        agent_lines.append(f"**{a['id']}**{default_mark} · `{a['model'].split('/')[-1]}`")
    agent_md = "\n".join(agent_lines) if agent_lines else "（未获取到 openclaw agents 列表）"

    stage_lines = []
    for i, s in enumerate(stages, 1):
        icon = status_icons.get(s.get("status"), "⏸")
        label = status_labels.get(s.get("status"), s.get("status", "idle"))
        agent_name = s.get("agent", "")
        stage_lines.append(
            f"{i}. **{s.get('name', '')}** {icon} {label}"
            + (f" · _{agent_name}_" if agent_name else ""))
    pipeline_md = "\n".join(stage_lines)

    detail_buttons = []
    for i in range(len(stages)):
        detail_buttons.append(
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                make_button(f"📄 阶段{i + 1}详情", "default", f"!detail {i + 1}"),
            ]})

    card = {
        "schema": "2.0",
        "config": {"width_mode": "fill", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": dashboard_title()},
            "template": "indigo",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**已注册 Agent（{len(agents)}）**\n{agent_md}",
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**开发流水线**\n{pipeline_md}",
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button("🔄 刷新", "default", "!board"),
                        ]},
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button("▶️ 启动流水线", "primary", "!pipeline start"),
                        ]},
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button("📊 会话列表", "default", "!sessions"),
                        ]},
                    ],
                },
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": detail_buttons,
                },
                make_button("🛑 终止流水线", "danger", "!pipeline stop"),
            ]
        },
    }
    return card


def decision_card_config():
    """O6：决策卡文案配置（team.json decision_card 段，脱敏可定制）。"""
    return TEAM.get("decision_card") or {}


def build_decision_card(info):
    """O6：渲染决策卡（批准/否决/转交三按钮，schema 2.0，文案全配置化）。

    info 字段：task_id / title / question / options / tier_label /
               timeout_at / default_action。
    档位文案与按钮文字均可经 team.json decision_card 段覆盖。
    """
    cfg = decision_card_config()
    header_tpl = cfg.get("header_title") or "🗳️ 任务决策待批"
    approve_text = cfg.get("approve_button") or "✅ 批准"
    reject_text = cfg.get("reject_button") or "❌ 否决"
    defer_text = cfg.get("defer_button") or "↔️ 转交"

    task_id = info.get("task_id") or ""
    content = [
        f"**任务** {info.get('title') or task_id}",
        f"**任务 ID** `{task_id}`",
    ]
    if info.get("tier_label"):
        content.append(f"**档位** {info['tier_label']}")
    content.append(f"**问题** {info.get('question') or '（待补充）'}")
    for i, opt in enumerate(info.get("options") or [], 1):
        content.append(f"   选项{i}：{opt}")
    content.append(f"**限时** {info.get('timeout_at') or '未设置'} · 未否决按默认策略"
                   f" **{info.get('default_action') or 'approve'}** 执行")

    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_tpl},
            "template": "orange",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "\n".join(content)},
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button(approve_text, "primary",
                                        f"!decide {task_id} approve"),
                        ]},
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button(reject_text, "danger",
                                        f"!decide {task_id} reject"),
                        ]},
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                            make_button(defer_text, "default",
                                        f"!decide {task_id} defer"),
                        ]},
                    ],
                },
            ]
        },
    }


def build_alert_card(info):
    """O7b：渲染巡检告警卡（sweep findings 摘要，文案可配置）。

    info 字段：account / engineer / findings / generated_at。
    """
    cfg = TEAM.get("alert_card") or {}
    header_tpl = cfg.get("header_title") or "🚨 流水线巡检告警"
    findings = info.get("findings") or []
    lines = [
        f"**账号** {info.get('account') or '-'}（{info.get('engineer') or '-'}）",
        f"**发现异常** {len(findings)} 项 · 生成于 {info.get('generated_at') or '-'}",
    ]
    type_labels = {
        "stale_running": "卡死",
        "pending_aged": "待分配超龄",
        "retry_overdue": "重试超期",
        "ledger_no_progress": "有账无进展",
    }
    for f in findings[:10]:
        lines.append(
            f"• **{type_labels.get(f.get('type'), f.get('type'))}** "
            f"`{f.get('task_id')}`（{f.get('status')}/{f.get('agent')}）· {f.get('detail')}")
    if len(findings) > 10:
        lines.append(f"… 其余 {len(findings) - 10} 项见 sweep JSON 输出")

    return {
        "schema": "2.0",
        "config": {"width_mode": "fill", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_tpl},
            "template": "red",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {"tag": "hr"},
                make_button("🔄 刷新看板", "default", "!board"),
            ]
        },
    }


def _post_json(url, payload, token, method=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        raise


def send_card(open_id, card, token):
    """发送新卡片给指定 open_id（该 open_id 必须是当前 bot 应用下的）。"""
    url = f"{FEISHU_API}/im/v1/messages?receive_id_type=open_id"
    payload = {
        "receive_id": open_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    return _post_json(url, payload, token)


def update_card(message_id, card, token):
    """更新（整体替换）一张已存在的卡片消息。"""
    url = f"{FEISHU_API}/im/v1/messages/{message_id}"
    return _post_json(url, {"content": json.dumps(card)}, token, method="PUT")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="通用飞书看板卡片发送器（渲染 + 发送/更新）")
    parser.add_argument("target", help="接收者 open_id（发送新卡片）或 message_id（--update 时）")
    parser.add_argument("pipeline_state", nargs="?", default=None,
                        help="可选：流水线状态 JSON，如 {\"stages\": [...]}")
    parser.add_argument("--update", action="store_true",
                        help="更新模式：target 视为已有卡片的 message_id")
    parser.add_argument("--account", default=None,
                        help="账号 id（等价于 BOARD_ACCOUNT 环境变量，命令行优先）")
    return parser.parse_args(argv)


def main():
    global CURRENT_ACCOUNT

    args = parse_args(sys.argv[1:])
    if args.account:
        CURRENT_ACCOUNT = args.account

    pipeline_state = None
    if args.pipeline_state:
        try:
            pipeline_state = json.loads(args.pipeline_state)
        except json.JSONDecodeError:
            print(f"pipeline_state 不是合法 JSON，已忽略：{args.pipeline_state[:80]}",
                  file=sys.stderr)

    token = get_token()
    agents = get_agent_statuses()
    card = build_dashboard_card(agents, pipeline_state)

    if args.update:
        result = update_card(args.target, card, token)
    else:
        open_id = resolve_open_id(args.target)
        if not open_id:
            print(f"账号 {CURRENT_ACCOUNT} 无可用 open_id；"
                  f"请显式传参或用 BOARD_OPEN_ID 环境变量传入", file=sys.stderr)
            sys.exit(1)
        result = send_card(open_id, card, token)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("code") != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
