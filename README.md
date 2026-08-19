# openclaw-feishu-crew

> **在飞书里用多个 bot 账号模拟一支工程师团队，按任务看板协作干活。**

> 🪄 **试试对你的 OpenClaw 说：「读 INDEX.md，带我部署一支三人团队」。**
> 它会照着 [INDEX.md](INDEX.md) 导航 + [SETUP_WIZARD.md](SETUP_WIZARD.md) 的 checklist 一步步带你建应用、填配置、跑绿 doctor、验证看板。

每个飞书 bot 账号是一位「工程师」，各带一块独立的任务看板；需求猴 / 架构猴 / 编码猴 / 质检猴 / 测试猴五只角色猴按流水线接单干活。这是 [OpenClaw](https://github.com/openclaw/openclaw) 生态内的「多 Agent 团队协作」工作区模板 + 编排脚本包。

## 它解决什么问题

OpenClaw 单 agent 会话是「一个全能助理」；本项目把它变成「一个工程团队」：

- **账号即工程师**：每个飞书 bot 账号对应一位工程师，独立看板、独立状态文件、独立凭证，互不可见；
- **看板即状态机**：任务状态 `pending → running → review → done`（另有 `stopped` / `error`），全部落盘在 `task-state-<account_id>.json`；
- **派活闭环**：`assign` 登记看板 → 协调猴用 `sessions_spawn(agentId=...)` 拉起子猴 → `record-run` 记录子会话 → 成功后 `set-result` / `complete`；
- **重试纪律**：`fail`（瞬时错误，任务级退避重试，默认 max_attempts=3）vs `error`（权限/配置类，不重试）。
- **通用卡片发送器**：`scripts/feishu_card.py` 独立渲染「Agent 管理控制台」看板卡片（已注册 Agent + 流水线阶段 + 操作按钮），支持发送新卡片与更新已有卡片，凭据全部走环境变量/config 分层；
- **开箱即用的协调猴模板**：`docs/coordinator-agents-template.md` 把看板规则、派活闭环、重试纪律、路径解析、心跳巡检等机制抽象成带 `<占位符>` 的通用 AGENTS.md 模板；`docs/command-reference.md` 是 pipeline.py 全量命令手册（含典型工作流示例）。

**镜像层（可选）**：pipeline 台账还可单向只读镜像到 OpenClaw Workboard 插件（Control UI 看板），每个账号一块 `pipeline-<account>` board，供管理端观察任务流转。镜像层完全独立、单向（台账是唯一事实源）、失败静默降级不阻塞主流程——**未启用 workboard 插件时触发即静默跳过，零影响**；设计见 [docs/workboard-bridge.md](docs/workboard-bridge.md)。

## 依赖清单

| 依赖 | 说明 |
|---|---|
| Python 3.x 标准库 | 脚本只用标准库（json/os/sys/urllib），无需 pip 安装任何包 |
| [OpenClaw](https://github.com/openclaw/openclaw) | 提供多 agent 配置（`agents.list`）、`sessions_spawn(agentId)` 子会话、飞书 channel 多账号（`channels.feishu.accounts`） |
| 飞书自建应用 × N | 每个工程师一个 bot 应用，需要开通机器人消息收发与卡片交互权限 |

## 快速上手（三步）

1. **建飞书应用**：在飞书开放平台为每位工程师创建一个自建应用（bot），记下每个应用的 `app_id` / `app_secret`（图文 SOP 见 [docs/feishu-app-setup.md](docs/feishu-app-setup.md)）；
2. **一键初始化**：

```bash
git clone https://github.com/dinymis/openclaw-feishu-crew
cd openclaw-feishu-crew && python3 scripts/setup.py
openclaw gateway restart   # 然后对 bot 说「看板」
```

`setup.py` 会自动从 `*.example` 生成配置骨架（`config/team.json`、`config/accounts/<account_id>.json`，已存在的保留不覆盖），交互问答收集昵称 / `app_id` / `app_secret` / `open_id`，并收尾跑 doctor 自检、列出待补项。

也可以用一键部署脚本把上面三步串成一条命令（前置检查 python3 ≥3.8 / 仓库完整性 → setup.py → 提示/执行 `openclaw gateway restart`），`--smoke` 还能自动跑端到端冒烟自验证：

```bash
bash scripts/deploy.sh --smoke          # 部署 + 自动冒烟（不发真实飞书消息）
bash scripts/deploy.sh --restart        # setup 成功后自动 openclaw gateway restart
bash scripts/deploy.sh --offline --account-id bot01 --name Alice \
  --app-id cli_xxx --app-secret ***   # 非交互/CI（参数透传给 setup.py）
```

<details>
<summary><b>高级：分步手动配置（setup.py 做的事拆开看）</b></summary>

1. **配 OpenClaw 多账号**：复制 `openclaw.example.json` 为你的 OpenClaw 配置（或并入现有配置），在 `channels.feishu.accounts.<account_id>` 填入各应用凭证——模板里已标注 `agents.list`、两处白名单（`subagents.allowAgents` / `tools.agentToAgent.allow`，最易踩的坑）与 `bindings` 的填法；
2. **部署本仓库**：把本仓库拷入 coordinator 工作区（`scripts/` + `config/` + `projects.json`）；
3. **填配置**：按 `config/README.md` 复制 `*.example` 为真实配置文件并填入凭证 / open_id / 工程师昵称；
4. **自检**：`python3 scripts/doctor.py`——全绿后 `openclaw gateway restart`，对任一 bot 说「看板」即可看到第一张卡片。

非交互/CI 场景也可一条命令直接生成完整配置：

```bash
python3 scripts/setup.py --account-id bot01 --name Alice \
  --app-id cli_xxx --app-secret secret-xxx --open-id ou_xxx
python3 scripts/setup.py --apply      # 可选：把账号自动并入 openclaw.json（合并前先备份 .bak）
python3 scripts/setup.py --offline    # 可选：跳过 doctor 联网探测
```

</details>

## 配置说明

- 配置层结构与优先级、占位符填写方法见 **`config/README.md`**；OpenClaw 侧的多 agent / 多账号配置模板见 **`openclaw.example.json`**；
- 整体架构（账号→工程师→看板→五猴流水线与状态机）见 **`docs/architecture.md`**；
- 协调猴工作区怎么落地（看板规则/派活闭环/重试纪律/路径解析/心跳巡检）见 **`docs/coordinator-agents-template.md`**；命令全量清单与典型工作流见 **`docs/command-reference.md`**；
- 代码不含业务敏感值，配置缺失时自动退化到代码默认值；
- 环境变量：`BOARD_ACCOUNT`（当前工程师账号 id）、`BOARD_OPEN_ID`（发送者 open_id）由协调猴在每次调用时传入。

## 命令速查

```bash
# 一键部署（clone 后一条命令：前置检查 → setup.py → restart 提示；--smoke 自动冒烟自验证）
bash scripts/deploy.sh --smoke
bash scripts/deploy.sh --restart        # setup 成功后自动执行 openclaw gateway restart

# 一键初始化（clone 后第一条命令；幂等，已存在的配置保留不覆盖）
python3 scripts/setup.py              # 交互问答：昵称/appId/appSecret/open_id
python3 scripts/setup.py --apply      # 同上，并把账号自动并入 openclaw.json（先备份 .bak）
python3 scripts/setup.py --offline    # 同上，跳过 doctor 联网探测

# 一键自检（填完两份配置后先跑这个，全绿再 restart）
python3 scripts/doctor.py            # 全量检查（含飞书凭证连通性探测）
python3 scripts/doctor.py --offline  # 无网环境：跳过连通性探测

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
python3 scripts/pipeline.py clear <task_id>            # 清除终态任务（仅 done/error/stopped；默认保留 history 记录）

# Workboard 镜像层（可选，需启用 OpenClaw Workboard 插件；未启用时静默跳过）
python3 scripts/workboard-mirror.py reconcile <account_id>   # 全量对账：以台账为准补齐差异（存量任务首次同步也用它）
python3 scripts/workboard-mirror.py event <account_id> <task_id>   # 单任务事件同步（pipeline.py 各命令成功后自动触发，一般无需手动）
python3 scripts/workboard-mirror.py cleanup <account_id> <task_id> ...  # 清理指定任务的镜像卡

# 项目路径解析（别名 → 任务前导语，含「必读项目 AGENTS.md」指令注入）
python3 scripts/resolve-project.py <项目别名> [服务别名]

# 通用飞书卡片发送器（渲染 Agent 管理控制台卡片；发送新卡片或 --update 更新已有卡片）
BOARD_ACCOUNT=bot01 python3 scripts/feishu_card.py <open_id> ['{"stages": [...]}']
BOARD_ACCOUNT=bot01 python3 scripts/feishu_card.py --update <message_id> ['{"stages": [...]}']
```

### fail/retry 行为说明

- `fail <task_id> <错误>` 用于瞬时错误（限流/超时/5xx/配额等）。任务**保持 running**（内部经 pending 重新分配），看板 summary 标注「上次失败，准备自动重试 N/M」，`attempts` 递增；
- 不存在独立的 `waiting_retry` 中间态：`fail` 返回 `retry=true` 后，由**编排方（协调猴）**负责用 `sessions_spawn(agentId=...)` 重新拉起子会话继续任务；
- 重试耗尽（默认 `max_attempts=3`，可在 team.json 配置）后任务转 **error** 终态，需人工介入；
- 权限/配置类错误请用 `error`（不可自动重试），避免无意义的反复重试。

## 冒烟验收边界

- 本仓库 `tests/smoke_test.py` 用虚构 team.json + mock 卡片发送，可离线跑通 `board / assign / set-result / complete` 状态机闭环（不发真实飞书消息）；`tests/doctor_test.py` 用虚构配置验证 doctor 全绿 / 缺配置两条路径；`tests/setup_test.py` 验证 setup.py 骨架生成 / 幂等 / `--apply` 备份（均为临时目录虚构配置，不碰仓库 config/）；`tests/deploy_smoke_test.py` 端到端验证一键部署闭环（deploy.sh 真实执行 + 状态机全闭环 + revision 乐观锁 / waiting_retry 退避 / retry-due 幂等新机制 + feishu_card.py 卡片渲染与发送路径 mock）；`tests/mirror_test.py` 用 mock RPC 验证镜像层状态映射 / 红线断言 / 主链路 / 失败降级（不依赖真实 gateway）；
- 真实飞书卡片收发需要有效应用凭证与 open_id：未配置凭证时 `board` 等涉及卡片的命令会在发送阶段报错，属预期行为——请先按「快速上手」配置。

## 常见问题 FAQ

| 症状 | 原因 | 解法 |
|---|---|---|
| 飞书接口报错 `99991672` / `Access denied scope` | 自建应用的 API 权限没开齐，或开了权限但没发布新版本（权限未生效） | 在开放平台「权限管理」开通机器人消息收发、卡片相关 scope，然后**创建新版本并发布**，再重试 |
| bot 收到消息但不回 | 白名单漏配（`subagents.allowAgents` 或 `tools.agentToAgent.allow`），或开放平台「事件订阅」没开/没订阅消息事件 | 两处白名单补齐（见 `openclaw.example.json` 注释里的坑 1/坑 2）；开放平台开启事件订阅并订阅 `im.message.receive_v1`，发布新版本 |
| 卡片发送失败，HTTP 400 | 应用缺少卡片（interactive message card）相关权限 | 在开放平台开通卡片发送相关权限并发布新版本；先用 doctor 确认凭证本身有效 |
| 跨 bot 发消息报错 `99992361`（接收者不存在/无权限） | open_id 是 **per-app** 的：同一个人在不同 bot 应用下的 open_id 不同，拿 A bot 里取到的 open_id 去让 B bot 发消息就会报这个错 | 每个 bot 各自获取对方的 open_id（如从该 bot 的消息事件 sender 元数据），team.json 里按账号分别填；不确定时用 `BOARD_OPEN_ID` 显式传入 |
| 看板不出卡片 | `BOARD_ACCOUNT` / `BOARD_OPEN_ID` 环境变量没传对（账号 id 拼错、open_id 为空） | `BOARD_ACCOUNT` 必须是 team.json accounts 段的 key（如 bot01）；`BOARD_OPEN_ID` 填该 bot 下的真实 open_id；先跑 `python3 scripts/doctor.py` 排查 |
| doctor 报 account_id 不对齐 | team.json accounts 表、config/accounts/ 凭证文件、openclaw.json `channels.feishu.accounts` 三处账号清单不一致 | 按 doctor 的 ❌ 提示补齐缺失的一侧；新增 bot 时三处同步加 |

> FAQ 覆盖的症状均为真实踩坑沉淀。配置类问题先跑 `python3 scripts/doctor.py` 定位，能省一半排查时间。

## 目录结构

```
openclaw-feishu-crew/
├── README.md
├── INDEX.md                   # AI 导航：文件地图 + 部署/排错/加人入口
├── SETUP_WIZARD.md            # 写给 AI 执行的逐步部署 checklist
├── LICENSE                    # MIT
├── .gitignore                 # 真实凭证/看板状态不入库
├── openclaw.example.json      # OpenClaw 多 agent + 多账号配置模板
├── scripts/
│   ├── deploy.sh              # 一键部署（前置检查 → setup.py → restart 提示，--smoke 自验证）
│   ├── setup.py               # 一键初始化（生成配置骨架 + doctor 自检，幂等）
│   ├── pipeline.py            # 核心：看板状态机 + 命令层（配置分层版）
│   ├── workboard-mirror.py    # 可选：pipeline → Workboard 单向镜像层
│   ├── doctor.py              # 一键自检（含 --fix 交互补全）
│   ├── add-engineer.py        # 一键新增工程师（三处同步 + doctor 复查）
│   ├── feishu_card.py         # 通用飞书卡片发送器（渲染看板卡片 + 发送/更新卡片）
│   └── resolve-project.py     # 项目别名 → 任务前导语
├── config/
│   ├── README.md              # 配置层说明与优先级
│   ├── team.json.example      # 团队配置模板（虚构示例）
│   └── accounts/
│       └── bot01.json.example # per-account 飞书凭证模板
├── docs/
│   ├── architecture.md        # 整体架构与状态机说明
│   ├── coordinator-agents-template.md  # 协调猴 AGENTS.md 通用模板（带 <占位符>）
│   ├── command-reference.md   # pipeline.py 全量命令手册 + 典型工作流
│   ├── workboard-bridge.md    # Workboard 镜像层设计（单向只读，可选）
│   └── feishu-app-setup.md    # 飞书自建应用搭建 SOP
├── projects.example.json      # 项目登记模板（虚构 demo-shop）
└── tests/
    ├── smoke_test.py          # 离线冒烟：状态机闭环验收
    ├── deploy_smoke_test.py   # 一键部署端到端冒烟（deploy.sh 闭环 + 新机制 + 卡片渲染）
    ├── doctor_test.py         # doctor / add-engineer / --fix 自测
    ├── setup_test.py          # setup.py 一键初始化自测（骨架/幂等/--apply 备份）
    └── mirror_test.py         # 镜像层自测（状态映射 + mock RPC，不依赖真实 gateway）
```

## 安全与边界

- 本仓库不含任何真实项目代码、真实凭证或真实用户标识；
- `config/team.json` 与 `config/accounts/*.json` 已被 `.gitignore` 排除，真实凭证只存在于你本机；
- 任务看板状态留在本地工作区（`task-state-<account_id>.json`，gitignore 排除）。

## License

MIT，见 [LICENSE](LICENSE)。
