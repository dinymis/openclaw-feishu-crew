#!/usr/bin/env bash
# deploy.sh —— openclaw-feishu-crew 一键部署脚本。
#
# clone 本仓库后的一条命令：前置检查 → setup.py 初始化 → restart 提示/尝试 → 可选 --smoke 自验证。
#
# 用法：
#   bash scripts/deploy.sh                 # 交互式（setup.py 问答流，回车可跳过）
#   bash scripts/deploy.sh --smoke         # 部署后自动跑端到端冒烟自验证
#   bash scripts/deploy.sh --restart       # setup 成功后自动执行 openclaw gateway restart
#   bash scripts/deploy.sh --offline --account-id bot01 --name Alice \
#       --app-id cli_xxx --app-secret *** --open-id ou_xxx   # 非交互/CI
#
# 除 --smoke / --restart / -h / --help 外，其余参数全部透传给 scripts/setup.py
# （含 --apply / --offline 与全部非交互参数）。
#
# 纯标准依赖（bash + python3 ≥3.8 标准库），无需 pip 安装任何包。
# set -euo pipefail：任一步失败立即报错退出；唯一例外是 openclaw 命令不存在时
# 只提示不报错（未安装 OpenClaw 不影响配置初始化本身）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

info() { echo "▶ $*"; }
err()  { echo "❌ $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
deploy.sh —— openclaw-feishu-crew 一键部署

用法：
  bash scripts/deploy.sh [选项] [setup.py 参数...]

deploy.sh 自身选项：
  --smoke     部署完成后自动跑端到端冒烟（tests/deploy_smoke_test.py）自验证
  --restart   setup 成功后自动执行 openclaw gateway restart（默认只提示）
  -h, --help  显示本帮助

其余参数原样透传给 scripts/setup.py，例如：
  --account-id bot01 --name Alice --app-id cli_xxx --app-secret *** --open-id ou_xxx
  --apply（并入 openclaw.json，先备份 .bak）  --offline（跳过 doctor 联网探测）

示例：
  bash scripts/deploy.sh                                   # 交互式问答
  bash scripts/deploy.sh --smoke                           # 部署 + 自验证
  bash scripts/deploy.sh --offline --account-id bot01 \
      --name Alice --app-id cli_xxx --app-secret ***      # 非交互/CI
EOF
}

SMOKE=0
RESTART=0
SETUP_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --smoke)   SMOKE=1 ;;
        --restart) RESTART=1 ;;
        -h|--help) usage; exit 0 ;;
        *)         SETUP_ARGS+=("$1") ;;
    esac
    shift
done

echo "openclaw-feishu-crew deploy 一键部署"
echo "仓库目录：$ROOT"
echo "------------------------------------------------------------"

# --- 前置检查 1/2：python3 ≥ 3.8 ------------------------------------------
info "前置检查 1/2：python3 ≥ 3.8"
command -v python3 >/dev/null 2>&1 \
    || err "未找到 python3 命令：请先安装 Python ≥ 3.8"
PYVER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || err "Python 版本过低：$PYVER（需要 ≥ 3.8）"
echo "  ✅ python3 $PYVER"

# --- 前置检查 2/2：git 仓库完整性 ------------------------------------------
info "前置检查 2/2：git 仓库完整性"
[ -d "$ROOT/.git" ] \
    || err "$ROOT 不是完整的 git 仓库：请先 git clone 本仓库再部署"
for f in scripts/setup.py scripts/pipeline.py scripts/doctor.py \
         scripts/feishu_card.py tests/deploy_smoke_test.py \
         config/team.json.example openclaw.example.json; do
    [ -f "$ROOT/$f" ] || err "仓库缺文件：$f（clone 可能不完整，请重新 clone）"
done
echo "  ✅ 仓库文件完整"

# --- setup.py 一键初始化（透传参数） ---------------------------------------
info "执行 setup.py 一键初始化（透传参数：${SETUP_ARGS[*]:-无}）"
python3 "$SCRIPT_DIR/setup.py" ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"} \
    || err "setup.py 未通过：请按上方 ❌ 待补项修复后重跑（已有配置保留，只补缺的）"

# --- openclaw gateway restart：检测命令存在性，不存在只提示不报错 ----------
echo ""
if command -v openclaw >/dev/null 2>&1; then
    if [ "$RESTART" = 1 ]; then
        info "检测到 openclaw 命令：尝试 openclaw gateway restart"
        if openclaw gateway restart; then
            echo "  ✅ gateway restart 命令已执行"
        else
            echo "⚠️ openclaw gateway restart 执行失败，请手动排查（配置初始化本身已完成）"
        fi
    else
        echo "ℹ️ 检测到 openclaw 命令：请自行执行 \`openclaw gateway restart\` 让配置生效"
        echo "   （也可带 --restart 重跑本脚本自动执行）"
    fi
else
    echo "ℹ️ 未检测到 openclaw 命令，跳过 restart。"
    echo "   安装 OpenClaw 后请执行 \`openclaw gateway restart\` 让配置生效。"
fi

# --- 可选：端到端冒烟自验证 ------------------------------------------------
if [ "$SMOKE" = 1 ]; then
    echo ""
    info "运行端到端冒烟自验证：tests/deploy_smoke_test.py"
    python3 "$ROOT/tests/deploy_smoke_test.py" \
        || err "端到端冒烟未通过：请按上方 FAIL 项排查"
    echo "  ✅ 端到端冒烟全绿"
fi

echo ""
echo "🎉 部署完成。下一步：对任一 bot 私聊说「看板」，验证第一张飞书卡片。"
echo "   配置类问题排查：python3 scripts/doctor.py"
