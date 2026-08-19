# INDEX.md —— AI 导航入口

> **给 AI（以及想快速定位的你）读的仓库地图。** 一句话：本项目把 OpenClaw 单 agent 变成「飞书多 bot 账号 = 一支工程师团队」，按任务看板协作干活。

## 一句话定位

每个飞书 bot 账号是一位「工程师」，五只角色猴（需求/架构/编码/质检/测试）按流水线接单；编排脚本（`scripts/pipeline.py`）管看板状态机，配置全程外置、零第三方依赖。

## 带你部署一支团队（最快路径）

**对 AI 说：「读 SETUP_WIZARD.md，带我部署一支三人团队」，然后逐条执行 SETUP_WIZARD.md 的 checklist。** 最快路径（建好飞书应用后）：

```bash
git clone https://github.com/dinymis/openclaw-feishu-crew
cd openclaw-feishu-crew && bash scripts/deploy.sh --smoke   # 一条命令：前置检查 → setup.py → restart 提示 → 自动冒烟自验证
openclaw gateway restart                                    # （若未带 --restart）然后对 bot 说「看板」
```

## 文件地图（每份文件干什么 / AI 在什么场景读）

| 文件 | 干什么 | AI 在什么场景读 |
|---|---|---|
| `README.md` | 项目总览：价值、依赖、三步上手、命令速查、FAQ | 一开始想了解全局、或遇到 FAQ 里的症状时 |
| `INDEX.md`（本文件） | 仓库导航与文件地图 | 每次会话开始、需要定位该读哪份文件时 |
| `SETUP_WIZARD.md` | 写给 AI 执行的逐步部署引导（checklist 化） | 用户要求「部署/搭建一支团队」时 |
| `openclaw.example.json` | OpenClaw 多 agent + 多 bot 账号配置模板（JSON5） | 配 OpenClaw 的 channels/agents/bindings/白名单时 |
| `config/README.md` | 业务配置层说明、优先级、字段含义 | 填 team.json / accounts/ 或排查配置优先级时 |
| `config/team.json.example` | 团队配置模板（accounts 表 / 默认账号 / agents） | 新建 team.json 时参考 |
| `config/accounts/bot01.json.example` | per-account 飞书凭证模板 | 新建某账号凭证文件时参考 |
| `scripts/deploy.sh` | 一键部署脚本（前置检查 python3≥3.8/仓库完整性 → setup.py → restart 提示），`--smoke` 部署后自动端到端冒烟自验证 | 刚 clone 仓库、要一条命令走完部署时 |
| `scripts/setup.py` | 一键初始化：生成配置骨架 + doctor 自检（幂等，已存在不覆盖）；`--apply` 并入 openclaw.json（先备份 .bak） | 刚 clone 仓库、从零初始化配置时 |
| `scripts/pipeline.py` | 核心：看板状态机 + 命令层 | 理解/改动看板逻辑、扩展命令时 |
| `scripts/doctor.py` | 一键自检（配置完整性/对齐/凭证连通性）；`--fix` 交互补全 | 部署收尾复查、诊断「为什么没生效」时 |
| `scripts/add-engineer.py` | 一键新增工程师（三处同步 + doctor 复查） | 用户要「加一位工程师/bot」时 |
| `scripts/feishu_card.py` | 通用飞书卡片发送器（渲染 Agent 管理控制台卡片 + 发送/更新） | 需要独立发送/更新看板卡片、或改造卡片样式时 |
| `scripts/resolve-project.py` | 项目别名 → 任务前导语（含必读 AGENTS.md 注入） | 派活涉及多项目路由时 |
| `docs/architecture.md` | 整体架构图与状态机说明 | 理解账号→工程师→看板→流水线全链路时 |
| `docs/coordinator-agents-template.md` | 协调猴 AGENTS.md 通用模板（带 `<占位符>`） | 新建/改造协调猴工作区 AGENTS.md 时 |
| `docs/command-reference.md` | pipeline.py 全量命令手册 + 典型工作流 | 查命令用法、环境变量、编排示例时 |
| `docs/feishu-app-setup.md` | 飞书自建应用搭建 SOP（含踩坑） | 建飞书应用、排查 99991672 / HTTP 400 / open_id 时 |
| `tests/smoke_test.py` | 离线冒烟：状态机闭环验收 | 改完 pipeline 后跑通验收 |
| `tests/deploy_smoke_test.py` | 一键部署端到端冒烟（deploy.sh 闭环 + revision/waiting_retry/retry-due 新机制 + 卡片渲染 mock） | 改完 deploy.sh 或验证部署闭环后跑通 |
| `tests/doctor_test.py` | doctor / add-engineer / --fix 自测 | 改完 doctor 或 add-engineer 后跑通 |
| `tests/setup_test.py` | setup.py 一键初始化自测（骨架/幂等/--apply 备份） | 改完 setup.py 后跑通 |
| `tests/mirror_test.py` | 镜像层自测（八态映射 + 红线断言 + mock RPC 主链路/降级） | 改完 workboard-mirror.py 后跑通 |
| `tests/decision_sweep_test.py` | 批次 3 验收：waiting_decision 决策卡 + approve/reject/defer 流转 + sweep 四类 findings + 幂等去重 | 改完决策/巡检机制后跑通 |
| `CONTRIBUTING.md` | 贡献约定（零依赖/脱敏/提交流程） | 准备提交改动时 |
| `CODE_OF_CONDUCT.md` / `LICENSE` | 社区准则 / MIT 许可 | 几乎不需要读 |

## 排错入口

- 症状对表 → `README.md`「常见问题 FAQ」
- 配置不生效 → `python3 scripts/doctor.py`（缺配置类问题先跑它）
- 飞书应用本身 → `docs/feishu-app-setup.md`

## 加人命令

```bash
python3 scripts/add-engineer.py bot03 Carole
# 三处同步（accounts 模板 / team.json 登记 / 打印 openclaw.json 片段）+ doctor 复查
```

## 命令速查

```bash
bash scripts/deploy.sh --smoke          # 一键部署 + 自动冒烟自验证
bash scripts/deploy.sh --restart        # 部署成功后自动执行 openclaw gateway restart
python3 scripts/setup.py              # 一键初始化（生成配置骨架 + doctor 自检）
python3 scripts/setup.py --apply      # 同上，并把账号并入 openclaw.json（先备份 .bak）
python3 scripts/doctor.py            # 全量自检（含凭证连通性探测）
python3 scripts/doctor.py --offline  # 自检（跳过联网探测）
python3 scripts/doctor.py --fix      # 交互引导：缺什么补填什么
python3 scripts/add-engineer.py <account_id> <昵称>   # 一键加工程师

python3 scripts/pipeline.py board                       # 看板
python3 scripts/pipeline.py start <需求>                # 建 5 阶段流水线
python3 scripts/pipeline.py assign <agent_id> <标题>    # 派活
python3 scripts/pipeline.py complete <task_id>          # 完成
python3 scripts/pipeline.py detail <task_id>            # 详情
# 完整命令见 README「命令速查」
python3 tests/deploy_smoke_test.py && python3 tests/smoke_test.py && python3 tests/doctor_test.py && python3 tests/setup_test.py && python3 tests/mirror_test.py && python3 tests/decision_sweep_test.py
```

## 部署就绪判定

四条全满足即「可用」：

1. `python3 scripts/doctor.py`（联网）全绿；
2. `openclaw gateway restart` 成功；
3. 对任一 bot 说「看板」能收到第一张卡片；
4. `python3 tests/deploy_smoke_test.py`、`tests/smoke_test.py`、`tests/doctor_test.py`、`tests/setup_test.py`、`tests/mirror_test.py`、`tests/decision_sweep_test.py` 全绿。
