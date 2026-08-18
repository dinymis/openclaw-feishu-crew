# config/ —— 业务配置层

代码（`scripts/pipeline.py`）不含任何业务敏感值，全部由本目录注入。

## 文件说明

| 文件 | 内容 |
|---|---|
| `team.json` | accounts 表（工程师昵称/open_id）、默认账号、max_attempts、model_hint、看板卡片标题模板 |
| `accounts/<account_id>.json` | 每个 bot 账号的飞书 app_id/app_secret |

## 第三方如何填写

最快方式：`python3 scripts/setup.py` —— 自动从 `*.example` 生成下面的配置文件骨架，交互问答收集昵称 / app_id / app_secret / open_id，幂等（已存在的配置保留不覆盖）。以下为手动方式，字段含义相同：

1. 复制 `team.json.example` 为 `team.json`：
   - `accounts` 段的 key（如 `bot01`）是你的 bot 账号 id，一个 key 对应一位「工程师」；
   - `engineer` 填工程师昵称（任意名字，如 Alice）；
   - `open_id` 填**你本人在该 bot 飞书应用内**的 open_id（open_id 是 per-app 的；可从飞书消息事件的 sender 元数据或飞书开放平台 API 调试台获取）；不需要某账号时填 `null`。
2. 为每个账号复制 `accounts/bot01.json.example` 为 `accounts/<account_id>.json`，填入该飞书自建应用的 `app_id` / `app_secret`。
3. `agents` 段的 `model_hint` 仅用于 agent-detail 卡片展示与一致性校验，填你期望的模型名即可，可留空。

## 配置优先级

- 飞书凭证：`FEISHU_APP_ID`/`FEISHU_APP_SECRET` 环境变量 > `accounts/<account_id>.json` > OpenClaw `openclaw.json`（兜底）
- open_id：`BOARD_OPEN_ID` 环境变量 > `team.json` accounts 表
- openclaw.json 路径：`OPENCLAW_CONFIG_PATH` 环境变量 > team.json `openclaw_config_path` > `~/.openclaw/openclaw.json`
- 状态文件目录：team.json `state_dir` > 工作区根目录（默认 `task-state-<account_id>.json`）
- 配置目录本身：`PIPELINE_CONFIG_DIR` 环境变量 > `<workspace>/config`

配置文件缺失时退化到代码默认值，命令接口行为不变。

## 安全纪律

`team.json` 与 `accounts/*.json` 含真实凭证与用户标识，**绝不可提交到任何公开仓库**（`.gitignore` 已排除，仅 `*.example` 入库）。
