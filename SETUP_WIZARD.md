# SETUP_WIZARD.md —— AI 部署引导手册

> **写给 AI 执行的逐步部署 checklist。** 目标：让第三方从 clone 到「看板出卡片」一路可验证。
> 每步都附**确定性验证命令**，AI 照做即可；做完一条勾一条。
>
> 💡 阶段 1/3 的手动拷模板与填配置可用 `python3 scripts/setup.py` 一条命令代替
> （自动从 `*.example` 生成配置骨架 + 交互问答收集昵称/appId/appSecret/open_id + doctor 自检）；
> 非交互/CI：`python3 scripts/setup.py --account-id bot01 --name Alice --app-id cli_xxx --app-secret *** --open-id ou_xxx`。
>
> 🚀 也可用一键部署脚本 `bash scripts/deploy.sh` 串联「前置检查 → setup.py → restart 提示」；
> 部署后用 `bash scripts/deploy.sh --smoke`（或单独跑 `python3 tests/deploy_smoke_test.py`）自验证，不发真实飞书消息。

---

## 前置：你（AI）手头要有

- 飞书开放平台的账号（能建自建应用，且有权发布版本）
- 本仓库已 clone 到某工作区（假设记为 `<repo>`）
- 目标：一支三人团队（3 个 bot 账号，如 `bot01/bot02/bot03`）

> 建飞书应用的图文细节见 **`docs/feishu-app-setup.md`**，这里只给 AI 可执行的 checklist。

---

## Checklist

### 阶段 0：跑一次自检，摸清起点

- [ ] 执行 `python3 scripts/doctor.py --offline`
- 预期：报 `config/team.json 不存在`、`channels.feishu.accounts` 缺失等 ❌。这是正常的起点快照，记下缺了什么。

### 阶段 1：准备配置文件

> 快捷路径：`python3 scripts/setup.py`（交互问答收集一位工程师的昵称/appId/appSecret/open_id，
> 自动生成 team.json 与 accounts/<account_id>.json 骨架，幂等不覆盖已有配置）。
> 以下为手动等价步骤，多人团队逐个加人时也可改用阶段 7 的 `add-engineer.py`。

- [ ] 复制团队模板：`cp config/team.json.example config/team.json`
- [ ] 复制凭证模板（先给 bot01）：
      `cp config/accounts/bot01.json.example config/accounts/bot01.json`
- [ ] 验证：`python3 scripts/doctor.py --offline` 应只剩「占位符」类 ❌，不再有「文件不存在」。

### 阶段 2：建飞书自建应用（每个 bot 一个）

> 参照 `docs/feishu-app-setup.md` 逐项做，核心 5 步：

- [ ] 在飞书开放平台为 `bot01` 创建一个**自建应用**，拿到 `app_id` / `app_secret`
- [ ] 开通**机器人**能力
- [ ] 按 SOP 权限清单开通权限（机器人消息收发、卡片交互相关 scope）
- [ ] 事件订阅：订阅 `im.message.receive_v1`（WebSocket 长连接推送）
- [ ] 创建版本并**发布**（权限/事件未发布不生效）
- 对 `bot02`、`bot03` 重复上述 5 步。

### 阶段 3：填配置（三处对齐）

- [ ] 把 `app_id`/`app_secret` 填入 `config/accounts/<id>.json`（三个账号各一份）
- [ ] 在 `config/team.json` 的 `accounts` 段登记三位工程师昵称与 open_id
      （open_id 是 **per-app** 的，从各 bot 的消息事件 sender 元数据获取；暂未拿到就先留占位，运行时用 `BOARD_OPEN_ID` 传）
- [ ] 配 OpenClaw：参照 `openclaw.example.json` 在 `~/.openclaw/openclaw.json` 补上
      `channels.feishu.accounts`（三个账号）、`agents.list`（coordinator + 五猴）、
      两处白名单（`subagents.allowAgents` / `tools.agentToAgent.allow`）、`bindings` 三条路由
- 验证：`python3 scripts/doctor.py --offline` 应「account_id 对齐」「openclaw.json 对齐」全 ✅。

### 阶段 4：doctor 全绿

- [ ] 执行 `python3 scripts/doctor.py`（联网，会探测飞书凭证连通性）
- 预期：每个账号 `[bot0x] 飞书凭证有效：成功换取 tenant_access_token`。
- 若报 `code=99991672`/`Access denied scope` → 权限没开齐或没发布新版本，回到阶段 2。
- 若报 HTTP 400（卡片）→ 卡片权限缺失，见 SOP。
- 若 `open_id` 提醒 per-app → 确认每个 open_id 取自对应 bot 应用内。
- [ ] 直到输出「全绿」与「下一步 openclaw gateway restart」。

### 阶段 5：生效并验证看板

> 快捷路径：部署时带 `--restart` 可自动执行 `openclaw gateway restart`（未检测到 openclaw 命令时只提示不报错）。

- [ ] 执行 `openclaw gateway restart`
- [ ] 对任一 bot（如 bot01）私聊说「看板」
- [ ] 预期：收到第一张看板卡片（含三位工程师的看板入口 + 任务列表）
- 验证失败时：回 README FAQ「看板不出卡片」逐项排查。

### 阶段 6：跑测试，确认仓库状态机闭环

- [ ] `python3 tests/deploy_smoke_test.py` → 输出 `DEPLOY SMOKE PASS`（端到端一键部署闭环）
- [ ] `python3 tests/smoke_test.py` → 输出 `SMOKE PASS`
- [ ] `python3 tests/doctor_test.py` → 输出 `DOCTOR TEST PASS`
- [ ] `python3 tests/setup_test.py` → 输出 `SETUP TEST PASS`

### 阶段 7：加人（可选，用脚本而非手改）

- [ ] `python3 scripts/add-engineer.py bot04 Dana`（一键三处同步 + doctor 复查）
- [ ] 按脚本打印的 JSON5 片段，把 `bot04` 补进 `openclaw.json` 的 accounts 与 bindings。

---

## 完成判定

- [ ] `doctor.py`（联网）全绿
- [ ] `openclaw gateway restart` 无报错
- [ ] 对 bot 说「看板」能收到卡片
- [ ] 四组测试全绿（deploy smoke / smoke / doctor / setup）

以上全勾 = 团队可用。若中途卡住，把 `python3 scripts/doctor.py` 的输出发给用户，按 ❌ 与 FAQ 逐条解。
