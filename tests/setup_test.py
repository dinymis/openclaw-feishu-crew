#!/usr/bin/env python3
"""setup.py 自测：一键初始化的四条关键路径。

覆盖：
    1. 首次运行：从 *.example 生成 config/team.json 与 accounts/<id>.json 骨架
    2. 重复运行（幂等）：已存在的 team.json / accounts/*.json 保留不覆盖
    3. 非交互参数路径：--account-id/--name/--app-id/--app-secret/--open-id 直接生成
    4. --apply：合并写入 openclaw.json 前先备份 .bak，且原文件内容保留在 .bak

不发真实飞书请求：doctor 收尾自检全部以 --offline 跑。

用法：
    python3 tests/setup_test.py
"""

import io
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# setup.py 文件名带连字符风格但实为合法模块名，直接按路径加载
_spec = importlib.util.spec_from_file_location("setup", os.path.join(ROOT, "scripts", "setup.py"))
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name} {extra}")


def set_env(config_dir, openclaw_path=None):
    os.environ["PIPELINE_CONFIG_DIR"] = config_dir
    if openclaw_path:
        os.environ["OPENCLAW_CONFIG_PATH"] = openclaw_path
    else:
        os.environ.pop("OPENCLAW_CONFIG_PATH", None)


def run_setup(argv, config_dir, openclaw_path=None):
    set_env(config_dir, openclaw_path)
    buf = io.StringIO()
    code = setup.run(argv, out=buf)
    return code, buf.getvalue()


def clear_env():
    os.environ.pop("PIPELINE_CONFIG_DIR", None)
    os.environ.pop("OPENCLAW_CONFIG_PATH", None)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


print("== 1) 首次运行：生成配置骨架（交互问答全跳过 → 占位骨架 + ❌ 待补清单） ==")
d1 = tempfile.mkdtemp(prefix="setup-first-")
code, out = run_setup(["--offline"], d1, openclaw_path="/nonexistent/openclaw.json")
check("未填凭证 exit=1（doctor 有待补项）", code == 1)
check("交互问答提示在列", "进入交互问答" in out)
check("team.json 骨架已生成", os.path.isfile(os.path.join(d1, "team.json")))
check("accounts/bot01.json 骨架已生成", os.path.isfile(os.path.join(d1, "accounts", "bot01.json")))
team = load_json(os.path.join(d1, "team.json"))
check("骨架含 default_account=bot01", team.get("default_account") == "bot01")
check("骨架含 agents 段（来自 example 模板）", isinstance(team.get("agents"), dict))
acct = load_json(os.path.join(d1, "accounts", "bot01.json"))
check("凭证留占位符", acct["app_id"].startswith("cli_xxx"))
check("❌ 待补清单逐项列出", "自检还有" in out and "app_id" in out)
check("openclaw 片段输出在列", "channels.feishu.accounts 追加" in out)

print("== 2) 重复运行幂等：已存在配置保留不覆盖 ==")
d2 = tempfile.mkdtemp(prefix="setup-idem-")
os.makedirs(os.path.join(d2, "accounts"), exist_ok=True)
with open(os.path.join(d2, "team.json"), "w", encoding="utf-8") as f:
    json.dump({"default_account": "bot01",
               "accounts": {"bot01": {"engineer": "OldName",
                                       "open_id": "ou_fakefakefakefakefakefakefake01"}}}, f)
with open(os.path.join(d2, "accounts", "bot01.json"), "w", encoding="utf-8") as f:
    json.dump({"app_id": "cli_fakebot01000000", "app_secret": "keep-secret"}, f)
code, out = run_setup(["--account-id", "bot01", "--name", "NewName",
                       "--app-id", "cli_fake000000000001", "--app-secret", "new-secret",
                       "--open-id", "ou_fakefakefakefakefakefakefake01", "--offline"],
                      d2, openclaw_path="/nonexistent/openclaw.json")
team2 = load_json(os.path.join(d2, "team.json"))
acct2 = load_json(os.path.join(d2, "accounts", "bot01.json"))
check("幂等 exit=0", code == 0)
check("team.json 保留（昵称不被覆盖）", team2["accounts"]["bot01"]["engineer"] == "OldName")
check("accounts 保留（app_secret 不被覆盖）", acct2["app_secret"] == "keep-secret")
check("提示保留不覆盖", "保留不覆盖" in out)

print("== 3) 非交互参数路径：一次生成完整配置并自检全绿 ==")
d3 = tempfile.mkdtemp(prefix="setup-noninteractive-")
code, out = run_setup(["--account-id", "bot01", "--name", "Alice",
                       "--app-id", "cli_fakebot01000000", "--app-secret", "fake-secret-01",
                       "--open-id", "ou_fakefakefakefakefakefakefake01", "--offline"],
                      d3, openclaw_path="/nonexistent/openclaw.json")
team3 = load_json(os.path.join(d3, "team.json"))
acct3 = load_json(os.path.join(d3, "accounts", "bot01.json"))
check("非交互 exit=0（全绿）", code == 0, out)
check("昵称写入 team.json", team3["accounts"]["bot01"]["engineer"] == "Alice")
check("open_id 写入 team.json", team3["accounts"]["bot01"]["open_id"] == "ou_fakefakefakefakefakefakefake01")
check("凭证写入 accounts", acct3["app_id"] == "cli_fakebot01000000"
      and acct3["app_secret"] == "fake-secret-01")
check("doctor 自检全绿在列", "doctor 自检全绿" in out)
check("下一步指引在列", "openclaw gateway restart" in out and "看板" in out)

print("== 4) --apply：合并 openclaw.json 前先备份 .bak ==")
d4 = tempfile.mkdtemp(prefix="setup-apply-")
oc_path = os.path.join(d4, "openclaw.json")
with open(oc_path, "w", encoding="utf-8") as f:
    f.write('{\n  // JSON5 注释：重写后会丢，原件保留在 .bak\n'
            '  "channels": {"feishu": {"enabled": true}},\n'
            '  "agents": {"list": [{"id": "coordinator"}]}\n}\n')
code, out = run_setup(["--account-id", "bot01", "--name", "Alice",
                       "--app-id", "cli_fakebot01000000", "--app-secret", "fake-secret-01",
                       "--open-id", "ou_fakefakefakefakefakefakefake01",
                       "--apply", "--offline"], d4, openclaw_path=oc_path)
check("--apply exit=0", code == 0, out)
check("备份 .bak 已生成", os.path.isfile(oc_path + ".bak"))
check("备份先于合并（提示顺序）", "已备份 openclaw.json" in out)
oc = load_json(oc_path)
check("accounts 已并入", oc["channels"]["feishu"]["accounts"]["bot01"]["appId"]
      == "cli_fakebot01000000")
check("bindings 已追加", any(b.get("match", {}).get("accountId") == "bot01"
                              for b in oc.get("bindings", [])))
check("原 agents 段未动", oc["agents"]["list"][0]["id"] == "coordinator")

print("== 5) --apply 幂等：重跑不重复追加、不覆盖已有账号 ==")
code, out = run_setup(["--account-id", "bot01", "--name", "Alice",
                       "--app-id", "cli_fakebot01000000", "--app-secret", "fake-secret-01",
                       "--open-id", "ou_fakefakefakefakefakefakefake01",
                       "--apply", "--offline"], d4, openclaw_path=oc_path)
oc2 = load_json(oc_path)
check("重跑 exit=0", code == 0)
check("bindings 未重复追加", sum(1 for b in oc2.get("bindings", [])
                                  if b.get("match", {}).get("accountId") == "bot01") == 1)
check("提示保留不覆盖", "保留不覆盖" in out)

print("== 6) 无 openclaw.json 时 --apply 仍只输出片段（不误建） ==")
d5 = tempfile.mkdtemp(prefix="setup-nooc-")
code, out = run_setup(["--account-id", "bot01", "--name", "Alice",
                       "--app-id", "cli_fakebot01000000", "--app-secret", "fake-secret-01",
                       "--open-id", "ou_fakefakefakefakefakefakefake01",
                       "--apply", "--offline"],
                      d5, openclaw_path=os.path.join(d5, "openclaw.json"))
check("exit=0", code == 0)
check("提示参照 example 创建", "参照 openclaw.example.json 创建" in out)
check("未凭空创建 openclaw.json", not os.path.isfile(os.path.join(d5, "openclaw.json")))

clear_env()
print(f"\nSETUP TEST PASS: {PASS} checks")
