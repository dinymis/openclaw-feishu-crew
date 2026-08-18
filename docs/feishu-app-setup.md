# 飞书自建应用搭建 SOP

> 目标：为本仓库的每一位「工程师」（bot）创建可用的飞书自建应用。
> 每一步都对应 SETUP_WIZARD.md 阶段 2 的一条 checklist。所有配置均为虚构说明，不含任何真实凭证。

---

## 0. 为什么每个 bot 一个应用

- 每个 bot 账号 = 一位工程师，独立 app_id / app_secret / 独立消息收发。
- **open_id 是 per-app 的**：同一个用户在不同应用下的 open_id 不同。你在 bot01 应用里取到的 open_id，拿去让 bot02 发消息会 100% 报 `99992361`（接收者不存在/无权限）。**每个 bot 各自取各自的 open_id。**

---

## 1. 创建自建应用

1. 登录飞书开放平台后台（需要具备创建企业自建应用的权限）。
2. 「创建企业自建应用」→ 填名称（如 `feishu-crew-bot01`）、图标。
3. 进入应用详情页，在「凭证与基础信息」页拿到 **App ID**（`cli_` 开头）与 **App Secret**（点「重置」可见，仅显示一次，抄下来）。

> 这两个值填到 `config/accounts/<account_id>.json` 与 `openclaw.json` 的 `channels.feishu.accounts.<id>`。

---

## 2. 开通机器人能力

1. 「添加应用能力」→ 添加 **机器人**。
2. 机器人能力开启后，应用才能以 bot 身份收发消息。

---

## 3. 权限清单（scope）

进入「权限管理」，搜索并按需开通以下 scope（机器人消息 + 卡片交互）：

| scope（示意） | 用途 |
|---|---|
| `im:message` | 接收/发送消息（收发私聊/群聊消息的基础） |
| `im:message:send_as_bot` | 以机器人身份发消息 |
| `im:message.p2p_msg:readonly` 等读取类 | 读取会话与消息（如通过事件 sender 取 open_id） |
| 卡片（消息卡片）发送相关 scope | 发送/更新交互式消息卡片 |

> ⚠️ **开通权限 ≠ 生效**：新增/变更 scope 后必须**创建新版本并发布**，否则接口仍报权限不足。

---

## 4. 事件订阅（WebSocket）

1. 「事件与回调」→ 订阅方式选择 **WebSocket（长连接）**（无需公网回调地址，最省事）。
2. 添加事件订阅：勾选 **`im.message.receive_v1`（接收消息）**。
3. 保存后同样需要**创建版本并发布**。

> WebSocket 模式下，OpenClaw 的飞书 channel 会以长连接接收消息事件；`im.message.receive_v1` 没订阅时，bot 会「收消息但不回」。

---

## 5. 发布版本

1. 「版本管理与发布」→ 创建新版本，把上面所有改动（机器人能力/权限/事件订阅）打进去。
2. 提交审核 → 发布（企业自建应用通常即时生效，或需管理员审批）。
3. 发布后回到权限/事件页确认「已发布」状态。

---

## 6. 验证

```bash
python3 scripts/doctor.py        # 应输出 [bot0x] 飞书凭证有效
# 再对应用内机器人发「看板」，能收到卡片即成功
```

---

## 常见踩坑（真实沉淀）

| 症状 | 根因 | 解法 |
|---|---|---|
| 接口报 `99991672` / `Access denied scope` | 权限没开齐，或开了没发布新版本 | 开通对应 scope 后**创建版本并发布**，再重试 |
| 卡片发送 HTTP 400 | 应用缺卡片（交互式消息卡片）相关权限 | 开通卡片发送 scope 并发布；先用 doctor 确认凭证本身有效 |
| `99992361` 接收者不存在/无权限 | open_id 是 per-app 的，串用了别的 bot 的 open_id | 每个 bot 各自获取 open_id，team.json 按账号分别填 |
| bot 收到消息但不回 | 事件订阅没开，或 OpenClaw 白名单漏配 | 确认订阅 `im.message.receive_v1` 且已发布；白名单见 openclaw.example.json 坑 1/坑 2 |

---

## open_id 怎么拿（per-app）

在对应 bot 应用内，通过以下任一方式获取**你本人对该应用**的 open_id：

1. 给该 bot 发一条消息，从消息事件的 `sender` 字段取 `open_id`（调试台可回放事件）；
2. 飞书开放平台「调试台 / API 调试」用 `im/v1/messages` 等接口交互时，响应里的 `open_id`。

拿到后填进 `config/team.json` 的 `accounts.<id>.open_id`（或用 `BOARD_OPEN_ID` 运行时传入）。
