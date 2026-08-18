# Contributing to openclaw-feishu-crew

感谢你的关注！以下是参与贡献的方式与约定。

## 可以贡献什么

- 通用机制改进：`scripts/setup.py`、`scripts/pipeline.py` 状态机/命令、`scripts/resolve-project.py`、`scripts/doctor.py`、`scripts/add-engineer.py`
- 配置模板与文档：`config/*.example`、README、INDEX.md、SETUP_WIZARD.md、`docs/feishu-app-setup.md`
- 测试：`tests/smoke_test.py`、`tests/doctor_test.py`、`tests/setup_test.py` 覆盖新场景
- Bug 报告与使用问题：直接开 issue

## 开发约定

1. **零第三方依赖**：脚本只用 Python ≥3.8 标准库，不要引入 pip 依赖。
2. **配置分层**：一切可变项走 `config/`（team.json / accounts/*.json），代码默认值兜底；新参数化点保持「配置缺失退化默认值、旧调用方式零改动」的兼容原则。
3. **示例脱敏**：example 文件只用虚构账号/昵称（如 Alice/Bot01），占位 open_id 用 `ou_xxx` 风格。
4. **不写死绝对路径**；不提交真实凭证、token、open_id、内部域名等任何敏感信息。
5. 状态机与命令的破坏性变更需在 PR 描述中说明迁移方式，并更新 README 命令速查。

## 提交流程

1. Fork 本仓库并创建特性分支：`git checkout -b feat/xxx`
2. 本地跑一遍测试：`python3 tests/smoke_test.py`（应输出 `SMOKE PASS`）、`python3 tests/doctor_test.py`（应输出 `DOCTOR TEST PASS`）、`python3 tests/setup_test.py`（应输出 `SETUP TEST PASS`）
3. 提交前自查敏感信息：`grep -rInE 'ou_[0-9a-f]{10,}|[g]ithub_pat_|ghp_[A-Za-z0-9]{30,}|app_secret' . --exclude-dir=.git` 应为 0 命中（`[g]` 写法避免示例命令自身命中）
4. 提交 PR，说明动机、改动点与验证方式

## 提交信息风格

`<type>: <简述>`，type ∈ `feat / fix / docs / test / refactor / chore`，中英文皆可，说清楚即可。
