# openclaw-feishu-crew

> **在飞书里用多个 bot 账号模拟一支工程师团队，按任务看板协作干活。**

每个飞书 bot 账号是一位「工程师」，各带一块独立的任务看板；需求猴 / 架构猴 / 编码猴 / 质检猴 / 测试猴五只角色猴按流水线接单干活。这是 [OpenClaw](https://github.com/openclaw/openclaw) 生态内的「多 Agent 团队协作」工作区模板 + 编排脚本包。

## 它解决什么问题

OpenClaw 单 agent 会话是「一个全能助理」；本项目把它变成「一个工程团队」：

- **账号即工程师**：每个飞书 bot 账号对应一位工程师，独立看板、独立状态文件、独立凭证，互不可见；
- **看板即状态机**：任务状态 `pending → running → review → done`（另有 `stopped` / `error`），全部落盘在 `task-state-<account_id>.json`；
- **派活闭环**：`assign` 登记看板 → 协调猴用 `sessions_spawn(agentId=...)` 拉起子猴 → `record-run` 记录子会话 → 成功后 `set-result` / `complete`；
- **重试纪律**：`fail`（瞬时错误，任务级退避重试，默认 max_attempts=3）vs `error`（权限/配置类，不重试）。

## 依赖清单

| 依赖 | 说明 |
|---|---|
| Python 3.x 标准库 | 脚本只用标准库（json/os/sys/urllib），无需 pip 安装任何包 |
| [OpenClaw](https://github.com/openclaw/openclaw) | 提供多 agent 配置（`agents.list`）、`sessions_spawn(agentId)` 子会话、飞书 channel 多账号（`channels.feishu.accounts`） |
| 飞书自建应用 × N | 每个工程师一个 bot 应用，需要开通机器人消息收发与卡片交互权限 |

## 快速上手（五步）

1. **建飞书应用**：在飞书开放平台为每位工程师创建一个自建应用（bot），记下每个应用的 `app_id` / `app_secret`；
2. **配 OpenClaw 多账号**：复制 `openclaw.example.json` 为你的 OpenClaw 配置（或并入现有配置），在 `channels.feishu.accounts.<account_id>` 填入各应用凭证——模板里已标注 `agents.list`、两处白名单（`subagents.allowAgents` / `tools.agentToAgent.allow`，最易踩的坑）与 `bindings` 的填法；
3. **部署本仓库**：把本仓库拷入 coordinator 工作区（`scripts/` + `config/` + `projects.json`）；
4. **填配置**：按 `config/README.md` 复制 `*.example` 为真实配置文件并填入凭证 / open_id / 工程师昵称；
5. **看第一张看板**：对任一 bot 说「看板」（协调猴执行 `pipeline.py board`），一张带刷新按钮的飞书卡片即出现在对话里。

## 配置说明

- 配置层结构与优先级、占位符填写方法见 **`config/README.md`**；OpenClaw 侧的多 agent / 多账号配置模板见 **`openclaw.example.json`**；
- 整体架构（账号→工程师→看板→五猴流水线与状态机）见 **`docs/architecture.md`**；
- 代码不含业务敏感值，配置缺失时自动退化到代码默认值；
- 环境变量：`BOARD_ACCOUNT`（当前工程师账号 id）、`BOARD_OPEN_ID`（发送者 open_id）由协调猴在每次调用时传入。

## 命令速查

```bash
# 环境变量：BOARD_ACCOUNT=<bot01|bot02|...> [BOARD_OPEN_ID=<open_id>]
python3 scripts/pipeline.py board                      # 查看/刷新看板卡片
python3 scripts/pipeline.py agent-detail <agent_id>    # 查看某只猴的详情卡片
python3 scripts/pipeline.py start <需求>               # 创建 5 阶段流水线并启动
python3 scripts/pipeline.py assign <agent_id> <标题>   # 单点派活
python3 scripts/pipeline.py dispatch <自然语言>        # 自然语言派活（含角色关键词）
python3 scripts/pipeline.py record-run <task_id> [run_id] [child_session_key]  # 记录子会话
python3 scripts/pipeline.py set-result <task_id> <结果> [摘要]  # 保存结果并转审核
python3 scripts/pipeline.py complete <task_id>         # 完成任务（自动推进流水线下一阶段）
python3 scripts/pipeline.py review <task_id>           # 提交审核
python3 scripts/pipeline.py stop <task_id>             # 停止任务
python3 scripts/pipeline.py fail <task_id> <错误>      # 失败（可重试）
python3 scripts/pipeline.py error <task_id> <错误>     # 失败（不可重试）
python3 scripts/pipeline.py release <agent_id>         # 释放猴
python3 scripts/pipeline.py detail <task_id>           # 任务详情
python3 scripts/pipeline.py history                    # 历史任务

# 项目路径解析（别名 → 任务前导语，含「必读项目 AGENTS.md」指令注入）
python3 scripts/resolve-project.py <项目别名> [服务别名]
```

## 冒烟验收边界

- 本仓库 `tests/smoke_test.py` 用虚构 team.json + mock 卡片发送，可离线跑通 `board / assign / set-result / complete` 状态机闭环（不发真实飞书消息）；
- 真实飞书卡片收发需要有效应用凭证与 open_id：未配置凭证时 `board` 等涉及卡片的命令会在发送阶段报错，属预期行为——请先按「快速上手」配置。

## 目录结构

```
openclaw-feishu-crew/
├── README.md
├── LICENSE                    # MIT
├── .gitignore                 # 真实凭证/看板状态不入库
├── scripts/
│   ├── pipeline.py            # 核心：看板状态机 + 命令层（配置分层版）
│   └── resolve-project.py     # 项目别名 → 任务前导语
├── config/
│   ├── README.md              # 配置层说明与优先级
│   ├── team.json.example      # 团队配置模板（虚构示例）
│   └── accounts/
│       └── bot01.json.example # per-account 飞书凭证模板
├── projects.example.json      # 项目登记模板（虚构 demo-shop）
└── tests/
    └── smoke_test.py          # 离线冒烟：状态机闭环验收
```

## 安全与边界

- 本仓库不含任何真实项目代码、真实凭证或真实用户标识；
- `config/team.json` 与 `config/accounts/*.json` 已被 `.gitignore` 排除，真实凭证只存在于你本机；
- 任务看板状态留在本地工作区（`task-state-<account_id>.json`，gitignore 排除）。

## License

MIT，见 [LICENSE](LICENSE)。
