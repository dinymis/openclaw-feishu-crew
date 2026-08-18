#!/usr/bin/env python3
"""add-engineer.py —— 一键新增一位工程师（飞书 bot 账号）。

纯标准库（Python ≥ 3.8），零第三方依赖。

它帮你把「新增工程师」这件需要同步改三处的事，收拢成一条命令：
    1. 生成 config/accounts/<account_id>.json 凭证模板（app_id/app_secret 留占位符）
    2. 在 config/team.json 的 accounts 段登记该工程师（昵称，open_id 留占位符）
    3. 打印需并入 openclaw.json 的 channels.feishu.accounts.<id> 段与 bindings 片段
       （JSON5 带注释，方便照抄）
    4. 收尾自动调用 scripts/doctor.py 复查，提示该工程师还没填完的项
       （如凭证 / open_id 仍为占位符）

用法：
    python3 scripts/add-engineer.py <account_id> <昵称>

示例：
    python3 scripts/add-engineer.py bot03 Carole

幂等：重复添加同一 account_id 会友好提示并进入「更新」模式（昵称覆盖、凭证文件保留）。

环境变量：
    PIPELINE_CONFIG_DIR   配置目录覆盖（与 pipeline.py / doctor.py 一致）
    OPENCLAW_CONFIG_PATH  openclaw.json 路径（用于「可选自动合并」提示）
"""

import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)

ACCOUNT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
OPEN_ID_PLACEHOLDER = "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # 与 team.json.example 一致


def config_dir():
    """配置目录：PIPELINE_CONFIG_DIR 环境变量 > <repo>/config。"""
    return os.environ.get("PIPELINE_CONFIG_DIR") or os.path.join(ROOT, "config")


def team_path():
    return os.path.join(config_dir(), "team.json")


def accounts_dir():
    return os.path.join(config_dir(), "accounts")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def validate_account_id(account_id):
    if not account_id:
        return "account_id 不能为空"
    if not ACCOUNT_ID_RE.match(account_id):
        return ("account_id 需以字母开头，仅含字母/数字/下划线/连字符，"
                "长度 ≤ 64，例如 bot03、eng_alice")
    return None


def ensure_team_exists(team_path_):
    """team.json 不存在时，提示先复制 example（不擅自创建空表）。"""
    if not os.path.isfile(team_path_):
        example = team_path_ + ".example"
        hint = f"（可先 `cp {os.path.basename(example)} team.json` 再补）"
        if os.path.isfile(example):
            hint = (f"config/team.json 不存在。请先复制模板：\n"
                    f"    cp {example} {team_path_}\n"
                    f"  然后重跑本命令。")
        raise SystemExit(hint)


def ensure_accounts_dir(accounts_dir_):
    os.makedirs(accounts_dir_, exist_ok=True)


def write_account_template(account_id):
    """生成凭证模板（占位符），已存在则不覆盖。返回是否新建。"""
    path = os.path.join(accounts_dir(), f"{account_id}.json")
    if os.path.isfile(path):
        return path, False
    data = {
        "_comment": ("accounts/<account_id>.json。填入该飞书自建应用的 app_id / app_secret，"
                     "也可不填、改由环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET 注入。"),
        "app_id": "cli_xxxxxxxxxxxxxxxx",
        "app_secret": "your-app-secret-here",
    }
    dump_json(path, data)
    return path, True


def register_engineer(team, account_id, nickname):
    """在 team.json accounts 表登记。返回 (team, 是否新增)。"""
    accounts = team.setdefault("accounts", {})
    existed = account_id in accounts
    if existed:
        # 更新模式：只覆盖昵称，open_id 保留原值（避免误清掉已填的 open_id）
        if isinstance(accounts[account_id], dict):
            accounts[account_id]["engineer"] = nickname
        else:
            accounts[account_id] = {"engineer": nickname,
                                    "open_id": OPEN_ID_PLACEHOLDER}
    else:
        accounts[account_id] = {"engineer": nickname,
                                "open_id": OPEN_ID_PLACEHOLDER}
    return team, existed


def render_openclaw_snippet(account_id, nickname):
    """打印需并入 openclaw.json 的配置片段（JSON5 带注释）。"""
    lines = []
    lines.append(f"// channels.feishu.accounts 追加（{account_id} = {nickname}）")
    lines.append('"' + account_id + '": {')
    lines.append('  "appId": "cli_xxxxxxxxxxxxxxxx",   // 换成该应用的 app_id')
    lines.append('  "appSecret": "***"                   // 换成该应用的 app_secret')
    lines.append("},")
    lines.append("")
    lines.append(f"// bindings 追加（把 {account_id} 的消息路由给 coordinator）")
    lines.append('{ "agentId": "coordinator", "match": '
                 f'{{ "channel": "feishu", "accountId": "{account_id}" }} }},')
    return lines


def maybe_automerge(account_id, nickname):
    """若 OPENCLAW_CONFIG_PATH 指向真实 openclaw.json，提示可选自动合并（默认不写）。"""
    path = os.path.expanduser(os.environ.get("OPENCLAW_CONFIG_PATH") or "")
    if not path or not os.path.isfile(path):
        return
    print()
    print(f"检测到 OPENCLAW_CONFIG_PATH 指向真实 openclaw.json：{path}")
    print("（默认不自动写改，避免误改现有配置。若确认要自动合并，可自行把上面的片段抄进去。）")
    print("如需自动合并，请后续在 openclaw.json 中手动补上 channels.feishu.accounts "
          f"与 bindings 两处 {account_id} 条目。")


def run_doctor():
    """收尾自动调用 doctor.py 复查（offline，避免真实联网探测）。"""
    doctor = os.path.join(SCRIPT_DIR, "doctor.py")
    print()
    print("=" * 60)
    print("收尾自动复查（doctor.py --offline）：")
    print("=" * 60)
    try:
        subprocess.run([sys.executable, doctor, "--offline"], check=False)
    except OSError as ex:
        print(f"（无法调用 doctor.py：{ex}，请手动执行 python3 scripts/doctor.py）")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        print("错误：需要 <account_id> <昵称> 两个参数。")
        return 2

    account_id, nickname = argv[1], argv[2]
    err = validate_account_id(account_id)
    if err:
        print(f"错误：{err}")
        return 2
    if not nickname or not nickname.strip():
        print("错误：昵称不能为空。")
        return 2
    nickname = nickname.strip()

    ensure_team_exists(team_path())
    ensure_accounts_dir(accounts_dir())

    # 1) 凭证模板
    acct_path, acct_new = write_account_template(account_id)

    # 2) 登记到 team.json
    team = load_json(team_path())
    if not isinstance(team, dict):
        print("错误：config/team.json 顶层必须是 JSON 对象。")
        return 1
    team, existed = register_engineer(team, account_id, nickname)
    dump_json(team_path(), team)

    # 汇总输出
    print("✅ add-engineer 完成")
    print(f"   工程师：{nickname}（account_id={account_id}）")
    if existed:
        print("   模式：更新（该 account_id 已存在，昵称已覆盖，open_id/凭证文件保留）")
    else:
        print("   模式：新增")
    print(f"   账号凭证模板：{acct_path}" + ("（已新建）" if acct_new else "（已存在，未覆盖）"))
    print("   已登记：config/team.json accounts 段（open_id 占位待填）")
    print()
    print("接下来手把手补全 openclaw.json（照抄下面片段）：")
    print("-" * 60)
    for line in render_openclaw_snippet(account_id, nickname):
        print(line)
    print("-" * 60)
    print(f"并把 config/accounts/{account_id}.json 里的 app_id/app_secret，"
          f"以及 config/team.json 里 {account_id} 的 open_id 填成真实值。")

    maybe_automerge(account_id, nickname)
    sys.stdout.flush()  # 先刷父进程缓冲，再调 doctor，避免输出乱序
    run_doctor()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
