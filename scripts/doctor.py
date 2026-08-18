#!/usr/bin/env python3
"""doctor.py —— 一键自检：clone 本仓库后，填完两份配置跑一遍即可知道还缺什么。

纯标准库（Python ≥ 3.8），零第三方依赖。检查项：

  1. config/team.json 存在且 accounts / agents 段字段完整（昵称非占位符）
  2. config/accounts/<account_id>.json 存在、appId / appSecret 非占位符
  3. accounts 里的 account_id 与 team.json accounts 表、
     openclaw.json 风格的多账号结构（channels.feishu.accounts）能对齐
  4. 飞书凭证连通性探测：appId/appSecret 换 tenant_access_token
  5. open_id 格式检查（ou_ 开头）与「open_id 是 per-app 的」提醒

用法：
    python3 scripts/doctor.py            # 全量检查（含飞书连通性探测）
    python3 scripts/doctor.py --offline  # 跳过连通性探测（无网环境）
    python3 scripts/doctor.py --fix      # 交互引导：缺什么补填什么，填完自动复查

退出码：全绿 0；存在 ❌ 时非 0（便于 CI / 脚本使用）。

环境变量：
    PIPELINE_CONFIG_DIR        配置目录覆盖（与 pipeline.py 一致）
    OPENCLAW_CONFIG_PATH       openclaw.json 路径（默认 ~/.openclaw/openclaw.json）
    DOCTOR_FEISHU_TOKEN_URL    tenant_access_token 接口地址覆盖（测试用）
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)


def config_dir():
    """配置目录：PIPELINE_CONFIG_DIR 环境变量 > <repo>/config（每次调用动态解析）。"""
    return os.environ.get("PIPELINE_CONFIG_DIR") or os.path.join(ROOT, "config")


def team_config_path():
    return os.path.join(config_dir(), "team.json")


def accounts_config_dir():
    return os.path.join(config_dir(), "accounts")

def token_url():
    """tenant_access_token 接口地址：DOCTOR_FEISHU_TOKEN_URL 可覆盖（测试用）。"""
    return (os.environ.get("DOCTOR_FEISHU_TOKEN_URL")
            or "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal")

# 常见占位符特征：xxx / your-xxx / TBD / TODO / 尖括号模板
_PLACEHOLDER_RE = re.compile(r"(xxx|your[-_]|<[^>]+>|\btbd\b|\btodo\b|占位|请填写|请替换)", re.IGNORECASE)

OK, FAIL, WARN = "✅", "❌", "⚠️"

_results = []  # (level, message)


def ok(msg):
    _results.append((OK, msg))


def fail(msg):
    _results.append((FAIL, msg))


def warn(msg):
    _results.append((WARN, msg))


def is_placeholder(value):
    """空值或带占位符特征的值视为未填写。"""
    if not value or not isinstance(value, str):
        return True
    v = value.strip()
    if not v:
        return True
    return bool(_PLACEHOLDER_RE.search(v))


def is_single_char_suffix(app_id):
    """cli_xxxxxxxx / cli_yyyy 这类「前缀后同一个字母重复」的 app_id 视为占位。"""
    m = re.fullmatch(r"cli_([a-z0-9])\1{5,}", app_id or "")
    return bool(m)


def strip_json5_comments(text):
    """尽力而为地剥掉 // 行注释与 /* */ 块注释（openclaw.json 允许 JSON5 注释）。

    注意：不处理字符串值内部的注释转义边界（极端情况），对配置模板场景足够。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1), text)
    return text


def load_json(path, allow_comments=False):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if allow_comments:
        raw = strip_json5_comments(raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 检查 1：team.json
# ---------------------------------------------------------------------------

def check_team_config():
    """返回 (team_dict, account_ids)；失败返回 ({}, [])。"""
    path = team_config_path()
    if not os.path.isfile(path):
        fail(f"config/team.json 不存在：请复制 {os.path.basename(path)}.example 并填写")
        return {}, []
    try:
        team = load_json(path)
    except (json.JSONDecodeError, OSError) as ex:
        fail(f"config/team.json 解析失败：{ex}")
        return {}, []
    if not isinstance(team, dict):
        fail("config/team.json 顶层必须是 JSON 对象")
        return {}, []
    ok("config/team.json 存在且可解析")

    # accounts 段
    accounts = team.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        fail("team.json 缺 accounts 段（account_id → {engineer, open_id}）")
        return team, []
    bad = []
    for aid, info in accounts.items():
        if not isinstance(info, dict):
            bad.append(f"{aid}: 条目必须是对象")
            continue
        engineer = info.get("engineer")
        if is_placeholder(engineer):
            bad.append(f"{aid}: engineer 昵称未填写或仍是占位符")
        open_id = info.get("open_id")
        if open_id is not None and open_id != "":
            if not (isinstance(open_id, str) and open_id.startswith("ou_")):
                bad.append(f"{aid}: open_id 格式异常（应以 ou_ 开头），当前={open_id!r}")
            elif is_placeholder(open_id):
                bad.append(f"{aid}: open_id 仍是占位符（ou_xxx…），请填真实值或置 null")
    if bad:
        for b in bad:
            fail(f"team.json accounts → {b}")
    else:
        ok(f"team.json accounts 段完整：{len(accounts)} 个账号，昵称/open_id 均非占位符")

    # agents 段
    agents = team.get("agents")
    if not isinstance(agents, dict) or not agents:
        warn("team.json 缺 agents 段（model_hint 等，缺失时看板走代码默认值）")
    else:
        bad_agents = [aid for aid, v in agents.items() if not isinstance(v, dict)]
        if bad_agents:
            fail(f"team.json agents 段条目必须是对象：{', '.join(bad_agents)}")
        else:
            ok(f"team.json agents 段完整：{', '.join(agents.keys())}")

    return team, list(accounts.keys()) if isinstance(accounts, dict) else []


# ---------------------------------------------------------------------------
# 检查 2：accounts/*.json 凭证
# ---------------------------------------------------------------------------

def check_account_credentials(account_ids):
    """返回 {account_id: {appId, appSecret}}（仅含非占位符凭证）。"""
    creds = {}
    if not account_ids:
        return creds
    if not os.path.isdir(accounts_config_dir()):
        fail(f"config/accounts/ 目录不存在：每个账号需要一个 <account_id>.json 凭证文件")
        return creds
    for aid in account_ids:
        path = os.path.join(accounts_config_dir(), f"{aid}.json")
        if not os.path.isfile(path):
            fail(f"config/accounts/{aid}.json 缺失：请复制 bot01.json.example 并填写该 bot 凭证")
            continue
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError) as ex:
            fail(f"config/accounts/{aid}.json 解析失败：{ex}")
            continue
        if not isinstance(data, dict):
            fail(f"config/accounts/{aid}.json 顶层必须是 JSON 对象")
            continue
        app_id = data.get("app_id") or data.get("appId")
        app_secret = data.get("app_secret") or data.get("appSecret")
        problems = []
        if is_placeholder(app_id) or is_single_char_suffix(app_id or ""):
            problems.append("app_id 未填写或仍是占位符（cli_xxx…）")
        if is_placeholder(app_secret):
            problems.append("app_secret 未填写或仍是占位符")
        if problems:
            for p in problems:
                fail(f"accounts/{aid}.json：{p}")
        else:
            ok(f"accounts/{aid}.json 凭证已填写（app_id={app_id[:8]}***）")
            creds[aid] = {"appId": app_id, "appSecret": app_secret}
    return creds


# ---------------------------------------------------------------------------
# 检查 3：account_id 对齐
# ---------------------------------------------------------------------------

def check_alignment(team_account_ids, openclaw_path):
    """team.json accounts 表 ↔ config/accounts/ 文件名 ↔ openclaw.json 多账号结构。"""
    file_ids = set()
    if os.path.isdir(accounts_config_dir()):
        file_ids = {fn[:-5] for fn in os.listdir(accounts_config_dir())
                    if fn.endswith(".json") and not fn.endswith(".example.json")}

    team_set = set(team_account_ids)
    missing_files = sorted(team_set - file_ids)
    orphan_files = sorted(file_ids - team_set)
    if missing_files:
        fail(f"account_id 不对齐：team.json 有但 config/accounts/ 缺凭证文件：{', '.join(missing_files)}")
    if orphan_files:
        warn(f"config/accounts/ 存在 team.json 未登记的凭证文件：{', '.join(orphan_files)}（将被忽略）")
    if team_set and not missing_files:
        ok("account_id 对齐：team.json accounts 表与 config/accounts/ 凭证文件一一对应")

    # openclaw.json 风格多账号结构（channels.feishu.accounts）
    if not openclaw_path or not os.path.isfile(openclaw_path):
        warn("未找到 openclaw.json（可设 OPENCLAW_CONFIG_PATH），跳过与 OpenClaw 多账号结构的对齐检查")
        return
    try:
        cfg = load_json(openclaw_path, allow_comments=True)
        feishu = (cfg.get("channels") or {}).get("feishu") or {}
        oc_accounts = set((feishu.get("accounts") or {}).keys())
    except (json.JSONDecodeError, OSError, AttributeError) as ex:
        warn(f"openclaw.json 解析失败（{ex}），跳过对齐检查")
        return
    missing_in_oc = sorted(team_set - oc_accounts) if oc_accounts else sorted(team_set)
    if oc_accounts and not missing_in_oc:
        ok("openclaw.json 对齐：channels.feishu.accounts 覆盖全部 account_id")
    elif oc_accounts:
        fail("openclaw.json 缺账号：channels.feishu.accounts 缺 "
             f"{', '.join(missing_in_oc)}（bot 收不到消息多半因为这）")
    else:
        warn("openclaw.json 未见 channels.feishu.accounts 多账号段，请参照 openclaw.example.json 配置")


# ---------------------------------------------------------------------------
# 检查 4：飞书凭证连通性探测
# ---------------------------------------------------------------------------

def check_connectivity(creds, offline):
    if offline:
        warn("--offline：跳过飞书凭证连通性探测")
        return
    if not creds:
        warn("没有已填写的凭证，跳过飞书连通性探测")
        return
    for aid, c in sorted(creds.items()):
        payload = json.dumps({"app_id": c["appId"], "app_secret": c["appSecret"]}).encode()
        req = urllib.request.Request(
            token_url(), data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as ex:
            fail(f"[{aid}] 飞书连通性探测失败：请求异常 {ex}（检查网络/接口地址）")
            continue
        code = body.get("code")
        if code == 0 and body.get("tenant_access_token"):
            ok(f"[{aid}] 飞书凭证有效：成功换取 tenant_access_token")
        else:
            fail(f"[{aid}] 飞书凭证无效：code={code} msg={body.get('msg')!r}"
                 "（app_id/app_secret 抄错，或应用未创建/已停用）")


# ---------------------------------------------------------------------------
# 检查 5：open_id 提醒
# ---------------------------------------------------------------------------

def check_open_id_advice(team):
    accounts = team.get("accounts") or {}
    ids = {str(v.get("open_id") or "") for v in accounts.values() if isinstance(v, dict)}
    ids.discard("")
    if not ids:
        warn("team.json 未配置任何 open_id：看板卡片将无发送目标，"
             "需要运行时通过 BOARD_OPEN_ID 环境变量传入")
        return
    if len(ids) > 1:
        warn("多个账号配置了互不相同的 open_id——请确认每个 open_id 都是在"
             "对应 bot 应用内取到的（open_id 是 per-app 的，同一个人在不同 bot 下值不同）")
    else:
        ok("open_id 提醒：open_id 是 per-app 的——同一个人对每个 bot 的 open_id 都不同，"
           "换 bot 必须重新获取")


# ---------------------------------------------------------------------------
# --fix：交互引导补全
# ---------------------------------------------------------------------------


def _prompt(prompt, default=None):
    """交互输入；异常（EOF/KeyboardInterrupt）时中断引导，不写坏配置。"""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)
    return val if val else (default or "")


def _input_nonplaceholder(prompt, current=None):
    """循环询问，直到用户输入非占位符值或直接回车跳过。"""
    if current and not is_placeholder(str(current)):
        cur = f"（当前已填：{current}）"
    else:
        cur = ""
    while True:
        val = _prompt(f"{prompt}{cur}", default="").strip()
        if not val or not is_placeholder(val):
            return val
        print("  ↳ 这个值看起来还是占位符，请填真实值（回车跳过不修改）。")


def fix_team_missing():
    """team.json 不存在 → 复制 example 并结束（后续项交给下一轮）。"""
    ex = team_config_path() + ".example"
    if not os.path.isfile(ex):
        print("  ↳ 未找到 team.json.example，无法自动生成，请手动创建 config/team.json。")
        return
    ans = _prompt("config/team.json 缺失，是否从 example 复制并继续？(y/N)", default="n").lower()
    if ans in ("y", "yes"):
        with open(ex, encoding="utf-8") as f:
            content = f.read()
        with open(team_config_path(), "w", encoding="utf-8") as f:
            f.write(content)
        print("  ✅ 已复制 team.json.example → team.json（占位值，继续填）")
    else:
        raise SystemExit(0)


def _load_team_or_none():
    if not os.path.isfile(team_config_path()):
        return None
    try:
        return load_json(team_config_path())
    except (json.JSONDecodeError, OSError):
        return None


def _save_team(team):
    with open(team_config_path(), "w", encoding="utf-8") as f:
        json.dump(team, f, ensure_ascii=False, indent=2)
        f.write("\n")


def fix_team_fields(team):
    """补 accounts 段工程师昵称 / open_id。"""
    if not isinstance(team, dict):
        return team
    accounts = team.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        return team
    changed = False
    for aid, info in list(accounts.items()):
        if not isinstance(info, dict):
            continue
        # 昵称
        if is_placeholder(info.get("engineer")):
            v = _input_nonplaceholder(f"  accounts.{aid}.engineer（工程师昵称）")
            if v:
                info["engineer"] = v
                changed = True
        # open_id
        oid = info.get("open_id")
        if oid is None or oid == "" or is_placeholder(str(oid)) or not str(oid).startswith("ou_"):
            v = _input_nonplaceholder(f"  accounts.{aid}.open_id（此 bot 下的 open_id，回车跳过）")
            if v:
                info["open_id"] = v
                changed = True
    if changed:
        _save_team(team)
        print("  ✅ team.json 已更新")
    return team


def fix_account_credentials(account_ids):
    """补 config/accounts/<id>.json 的 app_id / app_secret。"""
    if not account_ids:
        return
    os.makedirs(accounts_config_dir(), exist_ok=True)
    for aid in account_ids:
        path = os.path.join(accounts_config_dir(), f"{aid}.json")
        data = {}
        if os.path.isfile(path):
            try:
                data = load_json(path)
            except (json.JSONDecodeError, OSError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        changed = False
        app_id = data.get("app_id") or data.get("appId") or ""
        app_secret = data.get("app_secret") or data.get("appSecret") or ""
        if is_placeholder(app_id) or is_single_char_suffix(app_id):
            v = _input_nonplaceholder(f"  accounts/{aid}.json app_id（cli_ 开头）")
            if v:
                data["app_id"] = v
                data.pop("appId", None)
                changed = True
        if is_placeholder(app_secret):
            v = _input_nonplaceholder(f"  accounts/{aid}.json app_secret")
            if v:
                data["app_secret"] = v
                data.pop("appSecret", None)
                changed = True
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print(f"  ✅ accounts/{aid}.json 已更新")


def fix_openclaw_accounts(team_account_ids, openclaw_path):
    """openclaw.json 缺账号 → 提示补 channels.feishu.accounts 与 bindings（仅提示，不自动写）。"""
    if not openclaw_path or not os.path.isfile(openclaw_path):
        print(f"  ↳ 未找到 openclaw.json，请手动按 openclaw.example.json 补 channels.feishu.accounts "
              f"与 bindings 中的 {', '.join(team_account_ids)}。")
        return
    try:
        cfg = load_json(openclaw_path, allow_comments=True)
        feishu = (cfg.get("channels") or {}).get("feishu") or {}
        oc_accounts = set((feishu.get("accounts") or {}).keys())
    except (json.JSONDecodeError, OSError, AttributeError):
        print(f"  ↳ openclaw.json 解析失败，请手动对齐 channels.feishu.accounts。")
        return
    missing = sorted(set(team_account_ids) - oc_accounts)
    if missing:
        print("  ↳ openclaw.json 缺下列账号，请在 channels.feishu.accounts 与 bindings 手动补：")
        for aid in missing:
            print(f"      accounts: \"{aid}\": {{ \"appId\": ..., \"appSecret\": ... }}")
            print(f"      bindings: {{ \"agentId\": \"coordinator\", "
                  f"\"match\": {{ \"channel\": \"feishu\", \"accountId\": \"{aid}\" }} }}")


def run_fix(out=sys.stdout):
    """交互引导补全：逐项补填后自动复查。不改变非交互行为。"""
    print("openclaw-feishu-crew doctor --fix 交互引导", file=out)
    print("（缺什么补什么；提示里的 [回车跳过] 都可不填，直接回车保留现状。）", file=out)
    print("-" * 60, file=out)

    if not os.path.isfile(team_config_path()):
        fix_team_missing()

    team = _load_team_or_none()
    if isinstance(team, dict):
        account_ids = list((team.get("accounts") or {}).keys()) if isinstance(team.get("accounts"), dict) else []
    else:
        account_ids = []

    if isinstance(team, dict):
        team = fix_team_fields(team)
        account_ids = list((team.get("accounts") or {}).keys()) if isinstance(team.get("accounts"), dict) else []

    fix_account_credentials(account_ids)

    oc_path = os.path.expanduser(os.environ.get("OPENCLAW_CONFIG_PATH")
                                 or (team or {}).get("openclaw_config_path")
                                 or "~/.openclaw/openclaw.json")
    if account_ids:
        fix_openclaw_accounts(account_ids, oc_path)

    print()
    print("补填完成，自动复查：", file=out)
    print("-" * 60, file=out)
    return run(offline=True, out=out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(offline=False, out=sys.stdout):
    _results.clear()
    print("openclaw-feishu-crew doctor 自检", file=out)
    print(f"配置目录：{config_dir()}", file=out)
    print("-" * 60, file=out)

    team, account_ids = check_team_config()
    creds = check_account_credentials(account_ids)
    oc_path = os.path.expanduser(os.environ.get("OPENCLAW_CONFIG_PATH")
                                 or team.get("openclaw_config_path")
                                 or "~/.openclaw/openclaw.json")
    if account_ids:
        check_alignment(account_ids, oc_path)
    check_connectivity(creds, offline)
    if account_ids:
        check_open_id_advice(team)

    for level, msg in _results:
        print(f"{level} {msg}", file=out)

    failures = [m for lvl, m in _results if lvl == FAIL]
    print("-" * 60, file=out)
    if failures:
        print(f"自检未通过：{len(failures)} 项 ❌。修复后重跑 python3 scripts/doctor.py；", file=out)
        print("症状对照见 README「常见问题 FAQ」。", file=out)
        return 1
    print("全绿 = 配置基本能跑。下一步：", file=out)
    print("  1. 把 openclaw.json 配置生效：openclaw gateway restart", file=out)
    print("  2. 对任一 bot 说「看板」，验证第一张飞书卡片", file=out)
    return 0


def main():
    offline = "--offline" in sys.argv[1:]
    if "--fix" in sys.argv[1:]:
        sys.exit(run_fix())
    sys.exit(run(offline=offline))


if __name__ == "__main__":
    main()
