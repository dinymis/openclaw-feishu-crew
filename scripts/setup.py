#!/usr/bin/env python3
"""setup.py —— 一键初始化：clone 本仓库后跑一条命令，生成配置骨架并自检。

纯标准库（Python ≥ 3.8），零第三方依赖，幂等（重复运行不覆盖已有配置）。

它把「人工拷贝 *.example + 逐项手改配置」收拢成一条命令：
    1. 从 *.example 生成真实配置骨架：config/team.json（若不存在）与
       config/accounts/<account_id>.json（若不存在）；已存在的配置跳过不覆盖；
    2. 交互式问答收集工程师昵称 / appId / appSecret / open_id；
       同时支持非交互参数（便于脚本化/CI）：
       --account-id <id> --name <昵称> --app-id <id> --app-secret *** --open-id <ou_xxx>
    3. 若 OPENCLAW_CONFIG_PATH 指向真实 openclaw.json：默认只打印需并入的
       channels.feishu.accounts.<id> 段 + bindings 片段；显式 --apply 才合并写入
       （合并前先备份原文件为 .bak）；
    4. 收尾自动调用 scripts/doctor.py 自检，有 ❌ 逐项列出并提示补填。

用法：
    python3 scripts/setup.py                     # 交互式：逐项问答（回车可跳过）
    python3 scripts/setup.py --account-id bot01 --name Alice \
        --app-id cli_xxx --app-secret *** --open-id ou_xxx   # 非交互/CI
    python3 scripts/setup.py --apply             # 并把账号并入 openclaw.json（先备份 .bak）
    python3 scripts/setup.py --offline           # 跳过 doctor 联网探测

环境变量：
    PIPELINE_CONFIG_DIR        配置目录覆盖（与 pipeline.py / doctor.py 一致）
    OPENCLAW_CONFIG_PATH       openclaw.json 路径（默认 ~/.openclaw/openclaw.json）
    DOCTOR_FEISHU_TOKEN_URL    透传给 doctor 的连通性探测接口（测试用）
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)

# 复用 doctor 的配置目录解析、占位符判断与 JSON5 注释剥离（同目录 import）
sys.path.insert(0, SCRIPT_DIR)
import doctor  # noqa: E402

ACCOUNT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

TEAM_NAME_PLACEHOLDER = "请填写工程师昵称"
APP_ID_PLACEHOLDER = "cli_xxxxxxxxxxxxxxxx"
APP_SECRET_PLACEHOLDER = "***"


# ---------------------------------------------------------------------------
# 参数与问答收集
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="openclaw-feishu-crew 一键初始化：生成配置骨架 + doctor 自检")
    parser.add_argument("--account-id", help="bot 账号 id（如 bot01），team.json accounts 段的 key")
    parser.add_argument("--name", help="工程师昵称（如 Alice）")
    parser.add_argument("--app-id", help="飞书自建应用 app_id（cli_ 开头）")
    parser.add_argument("--app-secret", help="飞书自建应用 app_secret")
    parser.add_argument("--open-id", help="你本人在该 bot 应用内的 open_id（ou_ 开头，可跳过）")
    parser.add_argument("--apply", action="store_true",
                        help="把账号并入 openclaw.json（默认只打印片段；合并前先备份 .bak）")
    parser.add_argument("--offline", action="store_true",
                        help="跳过 doctor 联网探测（无网环境）")
    return parser.parse_args(argv)


def validate_account_id(account_id):
    """与 add-engineer.py 同规则；返回错误信息或 None。"""
    if not account_id:
        return "account_id 不能为空"
    if not ACCOUNT_ID_RE.match(account_id):
        return ("account_id 需以字母开头，仅含字母/数字/下划线/连字符，"
                "长度 ≤ 64，例如 bot01、eng_alice")
    return None


def _ask(prompt, default=""):
    """交互输入；EOF/Ctrl-C 视为回车（用默认值），不中断整体流程。"""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        val = ""
    return val if val else default


def collect_answers(args, out):
    """合并命令行参数与交互问答。参数优先；缺项逐项提问（回车可跳过）。"""
    missing = [k for k, v in (
        ("account-id", args.account_id), ("name", args.name),
        ("app-id", args.app_id), ("app-secret", args.app_secret),
        ("open-id", args.open_id)) if v is None]
    if missing:
        print(f"进入交互问答（缺 {len(missing)} 项，回车可跳过，稍后 doctor 会列出待补项）", file=out)
        print("-" * 60, file=out)

    account_id = args.account_id
    if account_id is None:
        account_id = _ask("工程师账号 id（account_id，如 bot01）", default="bot01")
    err = validate_account_id(account_id)
    if err:
        raise SystemExit(f"错误：{err}")

    name = args.name
    if name is None:
        name = _ask("工程师昵称（如 Alice，回车跳过稍后补）")

    app_id = args.app_id
    if app_id is None:
        app_id = _ask("飞书应用 app_id（cli_ 开头，回车跳过稍后补）")

    app_secret = args.app_secret
    if app_secret is None:
        app_secret = _ask("飞书应用 app_secret（回车跳过稍后补）")

    open_id = args.open_id
    if open_id is None:
        open_id = _ask("你在该 bot 应用内的 open_id（ou_ 开头，回车跳过，"
                       "运行时可用 BOARD_OPEN_ID 传入）")

    if open_id and not open_id.startswith("ou_"):
        print(f"⚠️ open_id={open_id!r} 不以 ou_ 开头，可能取错了；"
              "open_id 是 per-app 的，需从该 bot 的消息事件 sender 元数据获取。", file=out)

    return {
        "account_id": account_id,
        "name": name.strip() if name else "",
        "app_id": app_id.strip() if app_id else "",
        "app_secret": app_secret.strip() if app_secret else "",
        "open_id": open_id.strip() if open_id else "",
    }


# ---------------------------------------------------------------------------
# 配置骨架生成（幂等：已存在则跳过）
# ---------------------------------------------------------------------------

def ensure_team_config(account_id, name, open_id, out):
    """从 team.json.example 生成 config/team.json 骨架；已存在则跳过。返回是否新建。"""
    path = doctor.team_config_path()
    if os.path.isfile(path):
        print(f"✅ config/team.json 已存在，保留不覆盖：{path}", file=out)
        return False

    # 以 example 为骨架基线（agents / card / max_attempts 段沿用模板）：
    # 优先配置目录旁的 team.json.example，其次仓库根 config/team.json.example
    base = {}
    for example in (path + ".example", os.path.join(ROOT, "config", "team.json.example")):
        if not os.path.isfile(example):
            continue
        try:
            with open(example, encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError) as ex:
            print(f"⚠️ 读取 {example} 失败（{ex}），改用内置最小骨架", file=out)
            continue
        if isinstance(loaded, dict):
            base = loaded
            break

    team = {
        "_comment": "由 scripts/setup.py 自动生成。字段说明见 config/README.md。",
        "default_account": account_id,
        "accounts": {
            account_id: {
                "engineer": name if name else TEAM_NAME_PLACEHOLDER,
                "open_id": open_id if open_id else None,
            }
        },
        "max_attempts": base.get("max_attempts", 3),
    }
    if isinstance(base.get("agents"), dict):
        team["agents"] = base["agents"]
    else:
        team["agents"] = {aid: {"model_hint": "your-provider/your-model"}
                          for aid in ("requirement-analyst", "architect",
                                      "code-reviewer", "coder", "tester")}
    if isinstance(base.get("card"), dict):
        team["card"] = base["card"]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(team, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ 已生成 config/team.json 骨架（account_id={account_id}）：{path}", file=out)
    return True


def ensure_account_credentials(account_id, app_id, app_secret, out):
    """生成 config/accounts/<account_id>.json；已存在则跳过。返回是否新建。"""
    accounts_dir = doctor.accounts_config_dir()
    path = os.path.join(accounts_dir, f"{account_id}.json")
    if os.path.isfile(path):
        print(f"✅ config/accounts/{account_id}.json 已存在，保留不覆盖：{path}", file=out)
        return False

    os.makedirs(accounts_dir, exist_ok=True)
    data = {
        "_comment": ("由 scripts/setup.py 自动生成。填入该飞书自建应用的 app_id / app_secret；"
                     "也可改由环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET 注入。"),
        "app_id": app_id if app_id else APP_ID_PLACEHOLDER,
        "app_secret": app_secret if app_secret else APP_SECRET_PLACEHOLDER,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    filled = "凭证已填入" if (app_id and app_secret) else "凭证留占位符，待补填"
    print(f"✅ 已生成 config/accounts/{account_id}.json（{filled}）：{path}", file=out)
    return True


# ---------------------------------------------------------------------------
# openclaw.json：片段输出 / --apply 合并（先备份 .bak）
# ---------------------------------------------------------------------------

def resolve_openclaw_path():
    """OPENCLAW_CONFIG_PATH 环境变量 > 默认 ~/.openclaw/openclaw.json。"""
    return os.path.expanduser(os.environ.get("OPENCLAW_CONFIG_PATH")
                              or "~/.openclaw/openclaw.json")


def render_openclaw_snippet(account_id, name, app_id, app_secret):
    """需并入 openclaw.json 的配置片段（JSON5 带注释，方便照抄）。"""
    app_id = app_id if app_id else APP_ID_PLACEHOLDER
    app_secret = app_secret if app_secret else APP_SECRET_PLACEHOLDER
    label = f"（{account_id} = {name}）" if name else f"（{account_id}）"
    lines = [
        f"// channels.feishu.accounts 追加 {label}",
        f'"{account_id}": {{',
        f'  "appId": "{app_id}",',
        f'  "appSecret": "{app_secret}"',
        "},",
        "",
        f"// bindings 追加（把 {account_id} 的消息路由给 coordinator）",
        '{ "agentId": "coordinator", "match": '
        f'{{ "channel": "feishu", "accountId": "{account_id}" }} }},',
    ]
    return lines


def merge_openclaw_config(path, account_id, app_id, app_secret, out):
    """--apply：把账号并入 openclaw.json。合并前先备份为 .bak；解析失败不写。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        cfg = json.loads(doctor.strip_json5_comments(raw))
    except json.JSONDecodeError as ex:
        print(f"❌ openclaw.json 解析失败（{ex}），未做任何修改。"
              "请手动并入上面打印的片段。", file=out)
        return False
    if not isinstance(cfg, dict):
        print("❌ openclaw.json 顶层必须是 JSON 对象，未做任何修改。", file=out)
        return False

    # 解析成功后先备份，再动笔
    backup = path + ".bak"
    shutil.copy2(path, backup)
    print(f"✅ 已备份 openclaw.json → {backup}", file=out)

    feishu = cfg.setdefault("channels", {}).setdefault("feishu", {})
    oc_accounts = feishu.setdefault("accounts", {})
    if not isinstance(oc_accounts, dict):
        print("❌ channels.feishu.accounts 结构异常（不是对象），跳过合并。", file=out)
        return False
    if account_id in oc_accounts:
        print(f"   channels.feishu.accounts.{account_id} 已存在，保留不覆盖", file=out)
    else:
        oc_accounts[account_id] = {
            "appId": app_id if app_id else APP_ID_PLACEHOLDER,
            "appSecret": app_secret if app_secret else APP_SECRET_PLACEHOLDER,
        }
        print(f"   已并入 channels.feishu.accounts.{account_id}", file=out)
    if not feishu.get("defaultAccount"):
        feishu["defaultAccount"] = account_id

    bindings = cfg.setdefault("bindings", [])
    bound = isinstance(bindings, list) and any(
        isinstance(b, dict)
        and (b.get("match") or {}).get("accountId") == account_id
        for b in bindings)
    if bound:
        print(f"   bindings 已含 {account_id} 路由，保留不重复追加", file=out)
    elif isinstance(bindings, list):
        bindings.append({"agentId": "coordinator",
                         "match": {"channel": "feishu", "accountId": account_id}})
        print(f"   已追加 bindings：{account_id} → coordinator", file=out)
    else:
        print("⚠️ bindings 结构异常（不是数组），未追加路由，请手动补。", file=out)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("✅ openclaw.json 已合并写入（注意：重写为严格 JSON，原 JSON5 注释不保留，"
          "原件在 .bak）", file=out)
    return True


def handle_openclaw(answers, apply_merge, out):
    """检测 OPENCLAW_CONFIG_PATH：默认只打印片段，--apply 才合并写入。"""
    account_id, name = answers["account_id"], answers["name"]
    app_id, app_secret = answers["app_id"], answers["app_secret"]
    path = resolve_openclaw_path()
    print(file=out)
    if not os.path.isfile(path):
        print(f"ℹ️ 未找到 openclaw.json（{path}）：请参照 openclaw.example.json 创建，"
              "需并入下列片段——", file=out)
        print("-" * 60, file=out)
        for line in render_openclaw_snippet(account_id, name, app_id, app_secret):
            print(line, file=out)
        print("-" * 60, file=out)
        return

    print(f"检测到 openclaw.json：{path}", file=out)
    if apply_merge:
        merge_openclaw_config(path, account_id, app_id, app_secret, out)
        return
    print("默认不改动该文件（避免误改）。需并入以下片段——", file=out)
    print("-" * 60, file=out)
    for line in render_openclaw_snippet(account_id, name, app_id, app_secret):
        print(line, file=out)
    print("-" * 60, file=out)
    print("如需自动合并：重跑 `python3 scripts/setup.py --apply`"
          "（合并前先备份原文件为 .bak）", file=out)


# ---------------------------------------------------------------------------
# 收尾自检与指引
# ---------------------------------------------------------------------------

def run_doctor(offline, out):
    """调用 scripts/doctor.py；返回 (退出码, ❌ 条目列表)。"""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "doctor.py")]
    if offline:
        cmd.append("--offline")
    print(file=out)
    print("=" * 60, file=out)
    print(f"收尾自动自检：{' '.join(cmd[1:])}", file=out)
    print("=" * 60, file=out)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as ex:
        print(f"⚠️ 无法调用 doctor.py：{ex}，请手动执行 python3 scripts/doctor.py", file=out)
        return 1, ["无法调用 doctor.py"]
    if proc.stdout:
        out.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            out.write("\n")
    if proc.stderr:
        out.write(proc.stderr)
    fails = [line.lstrip()[2:].strip()
             for line in proc.stdout.splitlines()
             if line.lstrip().startswith("❌")]
    return proc.returncode, fails


def print_next_steps(out):
    print(file=out)
    print("下一步：", file=out)
    print("  1. openclaw gateway restart        # 让 openclaw.json 配置生效", file=out)
    print("  2. 对任一 bot 私聊说「看板」          # 验证第一张飞书卡片", file=out)


def run(argv=None, out=sys.stdout):
    """一键初始化主流程。返回退出码：doctor 全绿 0；仍有 ❌ 时 1。"""
    args = parse_args(argv)
    print("openclaw-feishu-crew setup 一键初始化", file=out)
    print(f"配置目录：{doctor.config_dir()}", file=out)
    print("-" * 60, file=out)

    answers = collect_answers(args, out)
    print(file=out)
    ensure_team_config(answers["account_id"], answers["name"], answers["open_id"], out)
    ensure_account_credentials(answers["account_id"], answers["app_id"],
                               answers["app_secret"], out)
    handle_openclaw(answers, args.apply, out)

    code, fails = run_doctor(args.offline, out)
    print(file=out)
    print("=" * 60, file=out)
    if code != 0:
        print(f"setup 完成，但自检还有 {len(fails)} 项 ❌ 待补：", file=out)
        for i, msg in enumerate(fails, 1):
            print(f"  {i}. {msg}", file=out)
        print("补填方式：重跑 `python3 scripts/setup.py`（已有配置保留，只补缺的），"
              "或 `python3 scripts/doctor.py --fix` 交互补全。", file=out)
        return 1
    print("setup 完成，doctor 自检全绿 ✅", file=out)
    print_next_steps(out)
    return 0


def main():
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
