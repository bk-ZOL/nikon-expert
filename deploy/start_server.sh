#!/bin/bash
# =============================================================================
# Nikon Expert — Ubuntu 服务器一键启动
# 用法：bash deploy/start_server.sh
# =============================================================================
set -e

GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'

# ── 检测环境 ─────────────────────────────────────────────────────
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Nikon Expert — 服务器部署启动          ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

# GPU 检测
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    echo -e "${GREEN}✅ GPU 检测到：${GPU_NAME}${NC}"
    export EMBED_DEVICE=cuda
else
    echo "⚠️  未检测到 GPU，使用 CPU 模式"
    export EMBED_DEVICE=cpu
fi

# Conda 环境检测
if ! command -v conda &>/dev/null; then
    echo "❌ 未检测到 conda，请先安装 Miniforge/Miniconda"
    exit 1
fi

CONDA_ENV="nikon_expert"
if conda env list | grep -q "^${CONDA_ENV} "; then
    eval "$(conda shell.bash hook)"
    conda activate "$CONDA_ENV"
    echo -e "${GREEN}✅ Conda 环境：${CONDA_ENV}${NC}"
else
    echo "❌ Conda 环境 '${CONDA_ENV}' 不存在，请先运行 setup.sh"
    exit 1
fi

export BACKEND_MODE=remote
export API_BASE_URL=http://localhost:8000
export API_PORT=8000
export UI_PORT=7860

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ── 启动 FastAPI 后端 ────────────────────────────────────────────
echo ""
echo "⚙️  启动 API 服务 (端口 ${API_PORT})..."
uvicorn api.server:app --host 0.0.0.0 --port "$API_PORT" &
BACKEND_PID=$!

# 等待后端就绪
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${API_PORT}/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ API 服务已启动 (PID: ${BACKEND_PID})${NC}"
        break
    fi
    sleep 1
done

if ! curl -sf "http://localhost:${API_PORT}/health" > /dev/null 2>&1; then
    echo "❌ API 服务启动失败，请检查日志"
    kill "$BACKEND_PID" 2>/dev/null
    exit 1
fi

# ── 启动 Gradio UI ──────────────────────────────────────────────
echo "⚙️  启动 UI 服务 (端口 ${UI_PORT})..."
BACKEND_MODE=remote API_BASE_URL="http://localhost:${API_PORT}" \
    python ui/app.py &
UI_PID=$!
sleep 3

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            启动完成！                     ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  📡 API 服务：http://0.0.0.0:${API_PORT}"
echo "  🖥️  UI 界面：http://0.0.0.0:${UI_PORT}"
echo "  📋 API PID: ${BACKEND_PID} | UI PID: ${UI_PID}"
echo ""
echo "  按 Ctrl+C 停止所有服务"
echo ""

trap "echo '正在停止...'; kill $BACKEND_PID $UI_PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait
