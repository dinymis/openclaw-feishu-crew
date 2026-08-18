# pipeline → Workboard 单向桥接设计（1 档，可选镜像层）

pipeline.py 任务台账的**单向只读镜像**层：把任务看板镜像到管理端 Workboard 插件（OpenClaw 内置），供 Control UI 观察。**未启用 workboard 插件/gateway 不可达时整层静默降级，不影响 pipeline 任何功能。**

配套代码：`scripts/workboard-mirror.py`（镜像层）+ `scripts/pipeline.py` 内 `_trigger_workboard_mirror()`（触发器）。

## 0. 定位与铁律

1. **pipeline 台账（`task-state-<account>.json`）是唯一事实源**；Workboard 只是只读镜像。
2. **绝不反向写**：任何 Workboard 侧变化（人工拖卡、dispatch、lifecycle sync）都不回写 pipeline。
3. **最终一致**：镜像动作失败不阻塞 pipeline 主流程，降级进待补同步队列（`pending_ops`），后续事件/对账补齐。

## 1. 红线（镜像卡绝不越界，代码内强制断言 `assert_red_lines`）

| 红线 | 内容 | 原因 |
| --- | --- | --- |
| R1 | 绝不把镜像卡置为 `ready` | ready + 无 claim 是 dispatch 拉起 worker 的唯一入口，镜像卡被拉起会造成反向执行 |
| R2 | 绝不写 `sessionKey`/`execution`/`runId` 字段 | 会触发 Workboard lifecycle sync 自动移卡，与 pipeline 事实源打架；`run_id`/`child_session_key` 只写进 notes 纯文本 |
| R3 | 永不调用 `cards.dispatch` | 见 R1 |
| R4 | 只用 `todo/running/review/blocked/done` 五态 | 不用 `triage/scheduled/backlog`（各有非 pipeline 语义），不用 `ready`（见 R1） |

## 2. 状态映射表

pipeline 6 任务态 → Workboard 五态：

| pipeline 状态 | workboard 状态 | 说明 |
| --- | --- | --- |
| `pending` | `todo` | 排队中 |
| `running` | `running` | 同名映射 |
| `review` | `review` | 同名映射 |
| `done` | `done` | 附 comment 写结果摘要 |
| `stopped` | `blocked` | workboard 无「已停止」终态；blocked + comment「人工停止」；label `pipe:stopped` 区分 |
| `error` | `blocked` | blocked + comment 写错误与 attempts/max；label `pipe:error` |

**边界情况：**

| 边界 | 处理 |
| --- | --- |
| fail 重试期（pending 是瞬态，实际落回 running） | 卡片保持 `running`，追加 comment「第 N/M 次重试：<error 摘要>」，不做状态迁移（避免抖动） |
| fail 重试耗尽 / error（不可重试） | `blocked` + label `pipe:error` + comment |
| `clear` 归档（终态任务从台账移除） | 镜像层检测到台账无此任务：`cards.archive` 归档孤儿卡并从 mirror-state 移除 |
| 存量 done/stopped 历史任务 | 首次全量同步（reconcile）时**跳过**（避免看板被历史淹没），只镜像活跃 + error 态 |

## 3. board 划分

每账号一块 board：`pipeline-<account>`（如 `pipeline-bot01`、`pipeline-bot02`），与 per-account 隔离原则对齐；board 是 Workboard 侧唯一隔离边界。首次同步时用 `boards.upsert` 幂等建板。卡片 `agentId` 填 pipeline 的 agent id，仅作展示与过滤，不用于 dispatch（R1-R3 保证不会被拉起）。

## 4. 卡片规范

| 字段 | 模板 |
| --- | --- |
| `title` | `[<task_id>] <task.title 截断60字>` |
| `labels` | `acct:<account>`、`agent:<agent_id>`、`src:pipeline`、`stage:<阶段名>`、（异常时）`pipe:error` / `pipe:stopped` |
| `notes` | 任务描述 + attempts/max + summary（截 200 字）+ last_error（截 120 字）+ run_id/child_session_key（纯文本，R2） |
| `priority` | 默认 `normal` |
| `idempotencyKey` | `task_id`（服务端原生支持创建幂等） |

## 5. 同步通道：Gateway RPC

主通道为 `openclaw gateway call workboard.*` RPC 子进程调用：

```
openclaw gateway call workboard.cards.move --params '{"id":"<card>","status":"review"}' --json --timeout 5000
```

零新增依赖（纯标准库 subprocess）、零鉴权配置（CLI 自动用本地 gateway 鉴权）、状态迁移用 `cards.move` 任意两态直达。CLI `openclaw workboard` 子命令只覆盖 create/list/show/dispatch，无法完成状态迁移主链路，仅作人工排查手段。

各动作落点：

| 镜像动作 | RPC |
| --- | --- |
| 建板 | `workboard.boards.upsert` |
| 建卡 | `workboard.cards.create`（带 idempotencyKey/boardId/labels） |
| 状态迁移 | `workboard.cards.move {id, status}` |
| 结果/错误附注 | `workboard.cards.comment {id, body}` |
| notes/labels 刷新 | `workboard.cards.update {id, patch:{...}}` |
| 归档（clear） | `workboard.cards.archive` |

## 6. 触发点设计

### 6.1 触发器（pipeline.py 内）

`_trigger_workboard_mirror(task_ids, comment=None)`：命令成功后异步拉起 `workboard-mirror.py event <account> <task_id>`（不等待、不读返回、触发器自身静默失败）。挂钩位置（grep `_trigger_workboard_mirror` 即得全部）：

| pipeline 命令 | 镜像动作 |
| --- | --- |
| `assign`（ad-hoc） | 建卡（`running` 或 `todo`） |
| `start`（流水线） | 批量建各阶段卡 |
| `complete` | 本卡 move `done` + comment 摘要；下一阶段卡同步 |
| `review` / `set-result` / `record-run` | move/refresh 对应状态与 notes |
| `stop` | move `blocked` + label `pipe:stopped` + comment |
| `fail`（重试期） | 保持 running + comment「重试 N/M」 |
| `fail`（耗尽）/ `error` | move `blocked` + label `pipe:error` |
| `release` | 该 agent 被停止的任务逐卡按 stop 行 |
| `clear` | 归档孤儿镜像卡 |

### 6.2 存量/对账：reconcile

`workboard-mirror.py reconcile <account>` 以 pipeline 台账为准全量对账补同步（存量任务首次同步也用它）：活跃态建卡、done/stopped 历史跳过、先清 pending_ops、孤儿卡仅报告不自动删。

### 6.3 mirror-state 存储

独立文件 `workboard-mirror-<account>.json`（与 task-state 同目录，不复用 `sync-state.json`）：

```json
{
  "version": 1,
  "account": "bot01",
  "board_id": "pipeline-bot01",
  "cards": {
    "<task_id>": {
      "card_id": "uuid",
      "last_synced_status": "running",
      "last_sync_at": "2026-01-01 00:00:00"
    }
  },
  "pending_ops": []
}
```

写盘用「临时文件 + `os.replace` 原子替换」。

## 7. 幂等与失败策略

- **创建幂等**：`idempotencyKey = task_id`；兜底：mirror-state 已有 card_id 则跳过 create。
- **迁移幂等**：move 同态幂等无害，且能覆盖人工拖动；comment 只在状态迁移成功那次写入。
- **失败降级**：所有镜像调用 try/except（timeout 5000ms），任何异常不 raise、不改变 pipeline 命令返回与退出码；失败动作压入 `pending_ops`，下一次任意 pipeline 事件先清队列再执行本次动作。
- **gateway 不可达期间** pending_ops 累积；恢复后第一次事件批量补齐；也可主动跑 `reconcile` 对账。

## 8. 回滚与停用

镜像层完全独立：停用只需删除 `scripts/pipeline.py` 中各命令尾部的 `_trigger_workboard_mirror(...)` 调用；`scripts/workboard-mirror.py` 与 `workboard-mirror-<account>.json` 可保留不删，不影响 pipeline 任何功能。未安装/未启用 workboard 插件时无需任何操作——触发器与镜像层自动静默降级。
