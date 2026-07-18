#!/bin/bash
# ════════════════════════════════════════════════════════
# Nikon Expert — 启动图形界面
# 用法：bash start.sh
# ════════════════════════════════════════════════════════
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
echo -e "${BLUE}🚀 Nikon Expert — 启动界面${NC}"

# 选择 Python 运行器：优先 venv，其次 conda
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYRUN="$PROJECT_DIR/.venv/bin/python"
    echo -e "${GREEN}使用 venv：.venv/bin/python${NC}"
elif conda env list 2>/dev/null | grep -q "^nikon_expert "; then
    PYRUN="conda run -n nikon_expert python3"
    echo -e "${GREEN}使用 conda 环境：nikon_expert${NC}"
else
    echo -e "${RED}❌ 未找到 .venv 或 conda 环境，请先运行：bash setup_venv.sh${NC}"
    exit 1
fi

# 检查向量库是否有数据
HAS_DATA=$($PYRUN -c "
import os; os.chdir('$PROJECT_DIR')
from dotenv import load_dotenv; load_dotenv()
from src.ingestor import db_status
s = db_status()
print(s['points'])
" 2>/dev/null || echo "0")

if [ "$HAS_DATA" = "0" ]; then
    echo ""
    echo -e "${RED}⚠️  知识库为空！请先摄入数据：${NC}"
    echo "   bash ingest.sh"
    echo ""
    read -p "是否现在摄入 Obsidian Vault？[y/N]: " DO_INGEST
    if [[ "$DO_INGEST" =~ ^[Yy]$ ]]; then
        bash "$PROJECT_DIR/ingest.sh"
    else
        echo "请摄入数据后再启动界面。"
        exit 0
    fi
fi

# 启动 Ollama
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "启动 Ollama 后台服务..."
    ollama serve &>/tmp/ollama.log &
    sleep 3
fi

MODEL=$(grep "^LLM_MODEL=" .env | cut -d= -f2)
echo "📡 使用模型：$MODEL"
echo "🌐 界面地址：http://localhost:7860"
echo "   局域网：  http://$(ipconfig getifaddr en0 2>/dev/null || echo 'YOUR_IP'):7860"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

exec $PYRUN ui/app.py
