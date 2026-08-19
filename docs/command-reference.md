# 命令手册（Command Reference）

> `scripts/pipeline.py` 全量命令、环境变量与典型工作流。所有命令输出均为 JSON，可直接被编排方（协调猴）解析。
>
> **账号隔离是前提**：每条 `pipeline.py` 命令都必须带 `BOARD_ACCOUNT=<account_id>`（当前会话的飞书 bot 账号 id），否则落到默认账号看板上。

## 环境变量

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `BOARD_ACCOUNT` | ✅ 必填 | 当前工程师的 bot 账号 id（对应 `config/team.json` accounts 表的 key，如 `bot01`）。决定读写哪个看板状态文件 `task-state-<account_id>.json` |
| `BOARD_OPEN_ID` | 首张卡片必填 | 发送者（工程师）在该 bot 应用下的 open_id。team.json 未登记 open_id 时必须传入；open_id 是 **per-app** 的，不能跨 bot 复用 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选 | 飞书凭据最高优先级覆盖（其余优先级见 `config/README.md`） |
| `OPENCLAW_CONFIG_PATH` | 可选 | openclaw.json 路径覆盖，默认 `~/.openclaw/openclaw.json` |
| `PIPELINE_CONFIG_DIR` | 可选 | 配置目录覆盖，默认 `<仓库根>/config` |

## 命令全量清单

### 看板展示

| 命令 | 说明 |
|---|---|
| `board` | 渲染/刷新当前工程师的多 Agent 任务看板卡片。已有看板卡片时原地更新（PATCH）而非新发一张；发送成功后把 `message_id` 记入状态，供后续原地刷新 |
| `agent-detail <agent_id>` | 某只猴的详情卡片（实时模型、正在处理的任务、最近已处理），原地覆盖看板卡片，含「← 返回看板」按钮。`agent_id` 支持中文名/别名解析 |
| `detail <task_id>` | 输出单个任务的完整 JSON（title/status/agent/result/summary/attempts/run_id 等） |
| `history` | 输出历史任务（done/stopped 状态任务 + `clear` 归档的快照），按完成时间倒序 |

### 任务创建与派活

| 命令 | 说明 |
|---|---|
| `start <需求标题>` | 创建五阶段流水线（需求分析 → 技术评审 → 编码开发 → 代码评审 → 测试），五个阶段任务共享同一 `parent_id`，阶段 1 立即启动，返回 `parent_id` |
| `assign <agent_id> <标题>` | 单点派活：登记一个任务并立即分配给指定猴，返回 `task_id`。`agent_id` 取值：`requirement-analyst` / `architect` / `code-reviewer` / `coder` / `tester` / `ops`（运维猴，单点派活角色，不在五阶段流水线内） |
| `dispatch <自然语言>` | 自然语言派活：从文本中识别角色关键词（如「技术评审」「编码」「测试」）自动选猴，去掉关键词后作为任务标题 |

### 任务推进

| 命令 | 说明 |
|---|---|
| `record-run <task_id> [run_id] [child_session_key]` | 把协调猴 `sessions_spawn` 得到的子会话运行标识挂到任务上（run_id / child_session_key 可选，传哪个记哪个） |
| `set-result <task_id> <结果全文> [摘要]` | 保存任务结果；若任务处于 `running` 则自动转 `review`。摘要缺省时取结果前 200 字 |
| `complete <task_id>` | 完成任务（`running`/`review` 可完成）→ `done`。**流水线联动**：若任务属于某阶段，自动创建并启动下一阶段任务 |
| `review <task_id>` | 仅 `running` 可提交审核 → `review`（`set-result` 已隐含该转换，此命令用于不带结果的手动提交） |
| `stop <task_id>` | 人为停止任务（`done` 不可停止）→ `stopped`，释放占用的猴 |
| `release <agent_id>` | 释放某只猴：其名下所有 `running` 任务置 `stopped` |
| `clear <task_id>` | 清除终态任务（仅 `done`/`error`/`stopped`），从看板移除并归档进 `history`；非终态任务会被拒绝（提示先 stop/complete） |

### 失败与重试

| 命令 | 说明 |
|---|---|
| `fail <task_id> <错误摘要>` | 失败上报，自动分类：**瞬时错误**（配额/限流/429/超时/5xx 等关键词命中）→ 任务转 `waiting_retry` 状态并记 `next_retry_at`（默认退避 1800s，team.json `retry_backoff_seconds` 可配），**attempts 不增**，返回 `waiting: true`；**非瞬时错误**（权限/配置类）→ 直接转 `error` 终态不重试；瞬时错误且 attempts 已达 `max_attempts` → 转 `error` |
| `error <task_id> <错误摘要>` | **不可重试错误**（权限/配置类），任务直接转 `error` 终态，返回 `retry: false` |
| `retry-due` | **退避到期扫描（sweep，幂等）**：把 `next_retry_at` 已到点的 `waiting_retry` 任务转回并真启动（`running`），**attempts 此时才 +1**（账实相符：账上 +1 = 真启动一次）。返回 `started` 列表（含 task_id/agent_id），由协调猴对每个条目 `sessions_spawn(agentId=<agent_id>)` 拉起并 `record-run`。建议由 cron 每分钟驱动（见下方「退避重试驱动」） |

> ⚠️ 重试完全由看板状态机驱动：`fail` 瞬时失败不再立即重试（不烧 attempts），而是进 `waiting_retry` 等退避到期；禁止绕过看板直接重试，否则次数与状态失真。

#### 退避重试驱动（cron）

`waiting_retry` 任务的到期启动由外部 cron 定期调用 `retry-due` 驱动（脚本自身不内置定时器），建议每分钟一次、逐账号扫（命令幂等，无到点任务时不写状态、不发卡片）：

```cron
* * * * * for a in bot01 bot02; do BOARD_ACCOUNT=$a python3 /path/to/scripts/pipeline.py retry-due >/dev/null 2>&1; done
```

> `started` 非空时，协调猴需在心跳/下次巡检时对列出的任务补 `sessions_spawn` + `record-run`（看板已把任务置为 `running` 并 attempts+1）。

## 状态机速览

```
assign/start → pending → running → review → done
                           │  ↓ fail（瞬时错误，attempts 不增）
                           │  waiting_retry（带 next_retry_at 退避）
                           │  ↓ retry-due 到点真启动（attempts 才 +1）
                           ├─→ running（回到运行）…… attempts 耗尽再 fail → error
                           ├─→ stopped（stop）
                           ├─→ error（error / fail 非瞬时错误 / 重试耗尽）
review → running（审核打回重派）
done/error/stopped → clear（归档进 history）
```

**并发写保护（revision 乐观锁）**：state.json 带 `revision` 字段，每次写入 +1；命令保存时若磁盘 revision 已超前（并发写抢先），本次写入被拒并自动重读重试（最多 10 次），不会互相覆盖。

## 典型工作流

### 工作流一：单点派活闭环（一个任务从创建到完成）

```bash
# 0. 每条命令都带 BOARD_ACCOUNT；首张卡片需要 BOARD_OPEN_ID
export BOARD_ACCOUNT=bot01
export BOARD_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 1. 登记任务（pending → running），返回 task_id
python3 scripts/pipeline.py assign coder "修复登录接口超时问题"
# → {"ok": true, "task_id": "task-1787100000-a1b2c3", ...}

# 2. 协调猴用 sessions_spawn(agentId="coder", label="编码猴-修复登录接口", taskName=...) 拉起子猴

# 3. 记录子会话运行标识（sessions_spawn 返回的 run_id / child_session_key）
python3 scripts/pipeline.py record-run task-1787100000-a1b2c3 <run_id> <child_session_key>

# 4. 子猴成功交付：保存结果（自动转 review）
python3 scripts/pipeline.py set-result task-1787100000-a1b2c3 "完整结果全文..." "一句话摘要"

# 5a. 用户审核通过 → 完成
python3 scripts/pipeline.py complete task-1787100000-a1b2c3

# 5b.（可选）查看任务详情 / 清理看板
python3 scripts/pipeline.py detail task-1787100000-a1b2c3
python3 scripts/pipeline.py clear task-1787100000-a1b2c3
```

失败分支（替换步骤 4）：

```bash
# 瞬时错误（如超时/配额/429）：fail 自动进 waiting_retry 退避，attempts 不增
python3 scripts/pipeline.py fail task-1787100000-a1b2c3 "子会话超时"
# → {"ok": true, "retry": false, "waiting": true,
#    "next_retry_at": "2026-08-19 15:30:00", ...}
# 协调猴此时不 spawn；cron 驱动的 retry-due 到点拉起（见「退避重试驱动」）：
# → {"ok": true, "started": [{"task_id": ..., "agent_id": "coder", "attempts": 2, ...}]}
# 协调猴对 started 每项：sessions_spawn(agentId="coder", ...) → record-run

# 权限/配置类错误：不重试，直接终态
python3 scripts/pipeline.py error task-1787100000-a1b2c3 "飞书应用缺少卡片权限"
```

### 工作流二：五阶段流水线

```bash
export BOARD_ACCOUNT=bot01

# 1. 创建流水线，阶段 1（需求分析）立即启动
python3 scripts/pipeline.py start "实现用户积分系统"
# → {"ok": true, "parent_id": "task-...-parent", ...}
# 五个阶段任务已建好：task-...-parent-1（running）～ -5（pending）

# 2. 阶段 1：spawn requirement-analyst → record-run → set-result → 用户审核 → complete
python3 scripts/pipeline.py set-result <阶段1 task_id> "需求文档全文..." "需求摘要"
python3 scripts/pipeline.py complete <阶段1 task_id>
# → complete 自动启动阶段 2（技术评审）任务

# 3. 重复 2 的模式推进阶段 2~5（architect → coder → code-reviewer → tester）
#    每阶段产出完整结果发给用户，等用户确认后再 complete 推进

# 4. 阶段 5 complete 后流水线完成；history 查看全部记录
python3 scripts/pipeline.py history
```

### 工作流三：看板日常管理

```bash
export BOARD_ACCOUNT=bot01

python3 scripts/pipeline.py board                    # 查看/刷新看板卡片（原地更新）
python3 scripts/pipeline.py agent-detail architect   # 看架构猴详情（也接受别名如「架构」）
python3 scripts/pipeline.py dispatch "帮我测试一下支付模块"   # 自然语言派活 → 自动识别为测试猴
python3 scripts/pipeline.py stop <task_id>           # 人为停止
python3 scripts/pipeline.py release coder            # 释放编码猴（名下任务全部 stopped）
python3 scripts/pipeline.py clear <task_id>          # 清理终态任务（保留 history）
```

## 相关文档

- 协调猴如何把这些命令编排成派活闭环：[`coordinator-agents-template.md`](coordinator-agents-template.md)
- 状态机与架构全景：[`architecture.md`](architecture.md)
- Workboard 镜像层（可选）：[`workboard-bridge.md`](workboard-bridge.md) 与 `scripts/workboard-mirror.py`
- 项目路径解析：`scripts/resolve-project.py`（别名 → 任务前导语，含「必读项目 AGENTS.md」指令注入）
