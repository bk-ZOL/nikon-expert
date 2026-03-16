#!/bin/bash
# ════════════════════════════════════════════════════════
# Nikon Expert — 摄入数据到知识库
# 用法：bash ingest.sh [vault路径]
# ════════════════════════════════════════════════════════
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; NC='\033[0m'
echo -e "${BLUE}📥 Nikon Expert — 知识库摄入${NC}"

# 检查 conda 环境
if ! conda env list | grep -q "^nikon_expert "; then
    echo "❌ 未找到 conda 环境 'nikon_expert'，请先运行 bash setup.sh"
    exit 1
fi

# 覆盖 Vault 路径（可选参数）
if [ -n "$1" ]; then
    export OBSIDIAN_VAULT_PATH="$1"
    echo "📂 使用 Vault 路径：$1"
fi

# 启动 Ollama（摄入阶段实际不需要，但保持一致）
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "启动 Ollama..."
    ollama serve &>/tmp/ollama.log &
    sleep 2
fi

echo ""
echo "选择摄入方式："
echo "  1. 仅摄入 Obsidian Vault"
echo "  2. 摄入所有数据源（Obsidian + PDF + Checksheet）"
read -p "请输入选项 [1/2，默认1]: " OPT
OPT="${OPT:-1}"

if [ "$OPT" = "2" ]; then
    conda run -n nikon_expert python scripts/ingest_all.py
else
    conda run -n nikon_expert python scripts/ingest_obsidian.py
fi

echo ""
echo -e "${GREEN}✅ 摄入完成！运行 bash start.sh 启动界面${NC}"
