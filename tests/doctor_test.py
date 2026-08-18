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

print(f"\nDOCTOR TEST PASS: {PASS} checks")
