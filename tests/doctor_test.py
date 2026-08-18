#!/usr/bin/env python3
"""doctor.py 自测：虚构配置跑通「全绿路径」+「缺配置路径」。

不发真实飞书请求：连通性探测的接口地址通过 DOCTOR_FEISHU_TOKEN_URL
指向本进程内临时 HTTP 服务（返回伪造成功响应）。

用法：
    python3 tests/doctor_test.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import doctor  # noqa: E402

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name} {extra}")


# --- 本地 mock 飞书 token 接口 ---------------------------------------------
class _TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if body.get("app_id", "").startswith("cli_") and body.get("app_secret"):
            resp = {"code": 0, "msg": "ok", "tenant_access_token": "mock-token"}
        else:
            resp = {"code": 10003, "msg": "invalid app_id"}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


_server = HTTPServer(("127.0.0.1", 0), _TokenHandler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
TOKEN_URL = f"http://127.0.0.1:{_server.server_address[1]}/token"


def run_doctor(config_dir, offline=False):
    os.environ["PIPELINE_CONFIG_DIR"] = config_dir
    os.environ["DOCTOR_FEISHU_TOKEN_URL"] = TOKEN_URL
    os.environ["OPENCLAW_CONFIG_PATH"] = os.path.join(config_dir, "openclaw.json")
    buf = io.StringIO()
    code = doctor.run(offline=offline, out=buf)
    return code, buf.getvalue()


def write_full_config(base):
    """虚构但完整的两份配置 + 对齐的 openclaw.json（全为假值）。"""
    accounts_dir = os.path.join(base, "accounts")
    os.makedirs(accounts_dir, exist_ok=True)
    team = {
        "default_account": "bot01",
        "accounts": {
            "bot01": {"engineer": "Alice", "open_id": "ou_fakefakefakefakefakefakefake01"},
            "bot02": {"engineer": "Bob", "open_id": "ou_fakefakefakefakefakefakefake02"},
        },
        "max_attempts": 3,
        "agents": {
            "coder": {"model_hint": "your-provider/your-model"},
            "tester": {"model_hint": "your-provider/your-model"},
        },
    }
    with open(os.path.join(base, "team.json"), "w", encoding="utf-8") as f:
        json.dump(team, f, ensure_ascii=False)
    for aid, secret in (("bot01", "fake-secret-01"), ("bot02", "fake-secret-02")):
        with open(os.path.join(accounts_dir, f"{aid}.json"), "w", encoding="utf-8") as f:
            json.dump({"app_id": f"cli_fake{aid}000000", "app_secret": secret}, f)
    openclaw = {
        "channels": {"feishu": {"enabled": True, "defaultAccount": "bot01",
                                 "accounts": {"bot01": {"appId": "cli_fakebot01000000"},
                                              "bot02": {"appId": "cli_fakebot02000000"}}}},
        "agents": {"list": [{"id": "coordinator"}]},
    }
    with open(os.path.join(base, "openclaw.json"), "w", encoding="utf-8") as f:
        json.dump(openclaw, f)


print("== 1) 全绿路径（offline，跳过连通性） ==")
d1 = tempfile.mkdtemp(prefix="doctor-green-")
write_full_config(d1)
code, out = run_doctor(d1, offline=True)
check("offline 全绿 exit=0", code == 0, out)
check("team.json ✅", "config/team.json 存在且可解析" in out)
check("对齐 ✅", "account_id 对齐" in out and "openclaw.json 对齐" in out)
check("open_id 提醒在列", "per-app" in out)

print("== 2) 全绿路径（含连通性探测，mock token 接口） ==")
code, out = run_doctor(d1, offline=False)
check("online 全绿 exit=0", code == 0, out)
check("bot01 凭证有效", "[bot01] 飞书凭证有效" in out)
check("bot02 凭证有效", "[bot02] 飞书凭证有效" in out)
check("下一步指引", "openclaw gateway restart" in out)

print("== 3) 缺配置路径（team.json 不存在） ==")
d2 = tempfile.mkdtemp(prefix="doctor-missing-")
code, out = run_doctor(d2, offline=True)
check("缺 team.json exit!=0", code != 0)
check("报 ❌", "❌ config/team.json 不存在" in out)

print("== 4) 占位符路径（example 原样复制，未填值） ==")
d3 = tempfile.mkdtemp(prefix="doctor-placeholder-")
shutil.copy(os.path.join(ROOT, "config", "team.json.example"), os.path.join(d3, "team.json"))
os.makedirs(os.path.join(d3, "accounts"), exist_ok=True)
shutil.copy(os.path.join(ROOT, "config", "accounts", "bot01.json.example"),
            os.path.join(d3, "accounts", "bot01.json"))
code, out = run_doctor(d3, offline=True)
check("占位符 exit!=0", code != 0)
check("open_id 占位报 ❌", "open_id 仍是占位符" in out)
check("app_id 占位报 ❌", "app_id 未填写或仍是占位符" in out)

print("== 5) 对齐缺口路径（openclaw.json 少一个账号） ==")
d4 = tempfile.mkdtemp(prefix="doctor-misalign-")
write_full_config(d4)
with open(os.path.join(d4, "openclaw.json"), encoding="utf-8") as f:
    oc = json.load(f)
del oc["channels"]["feishu"]["accounts"]["bot02"]
with open(os.path.join(d4, "openclaw.json"), "w", encoding="utf-8") as f:
    json.dump(oc, f)
code, out = run_doctor(d4, offline=True)
check("对齐失败 exit!=0", code != 0)
check("报缺账号", "channels.feishu.accounts 缺" in out and "bot02" in out)

print("== 6) doctor --fix 交互路径（mock input，先缺后补全→绿） ==")
d5 = tempfile.mkdtemp(prefix="doctor-fix-")
accounts_dir = os.path.join(d5, "accounts")
os.makedirs(accounts_dir, exist_ok=True)
# 初始：team.json 两账号 + 占位 open_id（ou_xxx），凭证占位
with open(os.path.join(d5, "team.json"), "w", encoding="utf-8") as f:
    json.dump({"default_account": "bot01",
               "accounts": {
                   "bot01": {"engineer": "Alice",
                             "open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
                   "bot02": {"engineer": "Bob",
                             "open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
               },
               "agents": {"coder": {"model_hint": "your-provider/your-model"}}}, f)
for aid in ("bot01", "bot02"):
    with open(os.path.join(accounts_dir, f"{aid}.json"), "w", encoding="utf-8") as f:
        json.dump({"app_id": "cli_xxxxxxxxxxxxxxxx", "app_secret": "your-app-secret-here"}, f)

# 模拟用户交互：依次回答 bot01/bot02 的 open_id，再 bot01/bot02 的 app_id/app_secret
import builtins  # noqa: E402
_fix_inputs = iter([
    "ou_fakebot01openid000000000000",  # bot01 open_id
    "ou_fakebot02openid000000000000",  # bot02 open_id
    "cli_fakebot01000000", "fake-secret-01",  # bot01 app_id/app_secret
    "cli_fakebot02000000", "fake-secret-02",  # bot02 app_id/app_secret
])
_orig_input = builtins.input
builtins.input = lambda *a, **k: next(_fix_inputs)
try:
    os.environ["PIPELINE_CONFIG_DIR"] = d5
    os.environ["DOCTOR_FEISHU_TOKEN_URL"] = TOKEN_URL
    os.environ["OPENCLAW_CONFIG_PATH"] = os.path.join(d5, "openclaw.json")
    buf = io.StringIO()
    code = doctor.run_fix(out=buf)
    fix_out = buf.getvalue()
finally:
    builtins.input = _orig_input
    os.environ.pop("PIPELINE_CONFIG_DIR", None)
    os.environ.pop("DOCTOR_FEISHU_TOKEN_URL", None)
    os.environ.pop("OPENCLAW_CONFIG_PATH", None)
check("--fix 补全后 offline 复查 exit=0", code == 0, fix_out)
check("--fix 输出引导标题", "交互引导" in fix_out)
check("--fix 补全后对齐绿", "account_id 对齐" in fix_out)

print("== 7) add-engineer.py（虚拟 team.json：新增+幂等+缺参报错） ==")
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "add_engineer", os.path.join(ROOT, "scripts", "add-engineer.py"))
add_engineer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_engineer)

d6 = tempfile.mkdtemp(prefix="add-eng-")
with open(os.path.join(d6, "team.json"), "w", encoding="utf-8") as f:
    json.dump({"default_account": "bot01",
               "accounts": {"bot01": {"engineer": "Alice",
                                       "open_id": "ou_xxx1"}}}, f)
os.makedirs(os.path.join(d6, "accounts"), exist_ok=True)
with open(os.path.join(d6, "accounts", "bot01.json"), "w", encoding="utf-8") as f:
    json.dump({"app_id": "cli_fakebot01000000", "app_secret": "s1"}, f)

# 新增 bot03
os.environ["PIPELINE_CONFIG_DIR"] = d6
try:
    argv = ["add-engineer.py", "bot03", "Carole"]
    code = add_engineer.main(argv)
    team_after = json.load(open(os.path.join(d6, "team.json"), encoding="utf-8"))
    check("add-engineer 新增 exit=0", code == 0)
    check("bot03 已登记", "bot03" in team_after["accounts"]
          and team_after["accounts"]["bot03"]["engineer"] == "Carole")
    check("bot03 凭证模板已建",
          os.path.isfile(os.path.join(d6, "accounts", "bot03.json")))

    # 幂等：重复添加同 id，open_id 保留、昵称覆盖
    argv2 = ["add-engineer.py", "bot03", "Carole2"]
    code2 = add_engineer.main(argv2)
    team_after2 = json.load(open(os.path.join(d6, "team.json"), encoding="utf-8"))
    check("add-engineer 幂等 exit=0", code2 == 0)
    check("幂等覆盖昵称", team_after2["accounts"]["bot03"]["engineer"] == "Carole2")

    # 缺参报错（exit=2）
    code3 = add_engineer.main(["add-engineer.py"])
    check("缺参 exit=2", code3 == 2)
finally:
    os.environ.pop("PIPELINE_CONFIG_DIR", None)

print(f"\nDOCTOR TEST PASS: {PASS} checks")
