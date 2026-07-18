#!/bin/bash
# =============================================================================
# Nikon Expert — venv 一键环境搭建（轻量，无需 conda）
# 适用：macOS Apple Silicon (M1/M2/M3/M4) · Python 3.11+
# 用法：bash setup_venv.sh
# =============================================================================
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo ""
echo -e "${BLUE}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Nikon Expert — venv 环境搭建（轻量版）     ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════╝${NC}"

# ── STEP 1 · Python ──────────────────────────────────────────
echo ""; echo -e "${BLUE}▶ STEP 1 · 检查 Python${NC}"
PY=""
for c in python3.12 python3.11 python3; do
    if command -v "$c" &>/dev/null; then
        ver=$("$c" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        maj=${ver%%.*}; min=${ver##*.}
        if [ "$maj" -eq 3 ] && [ "$min" -ge 10 ]; then PY="$c"; break; fi
    fi
done
[ -n "$PY" ] || err "需要 Python 3.10+。可执行：brew install python@3.11"
ok "Python：$($PY --version) ($PY)"

# ── STEP 2 · 创建 venv + 装依赖 ──────────────────────────────
echo ""; echo -e "${BLUE}▶ STEP 2 · 创建虚拟环境 .venv${NC}"
if [ -d .venv ]; then
    ok ".venv 已存在，跳过创建"
else
    "$PY" -m venv .venv
    ok ".venv 创建完成"
fi
info "升级 pip 并安装依赖（首次约几分钟）..."
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
ok "Python 依赖安装完成"

# ── STEP 3 · .env ────────────────────────────────────────────
echo ""; echo -e "${BLUE}▶ STEP 3 · 配置文件${NC}"
if [ ! -f .env ]; then
    cp .env.example .env 2>/dev/null && ok "已生成 .env（如需外接大脑，填入对应 API key）" \
        || warn "缺少 .env.example，请手动创建 .env"
else
    ok ".env 已存在"
fi

# ── STEP 4 · Ollama（本地大脑）──────────────────────────────
echo ""; echo -e "${BLUE}▶ STEP 4 · 检查 Ollama（本地 LLM）${NC}"
if command -v ollama &>/dev/null; then
    ok "Ollama 已安装"
    curl -s http://localhost:11434/api/tags &>/dev/null || { info "启动 Ollama..."; ollama serve &>/tmp/ollama.log & sleep 3; }
    MODEL="${1:-qwen2.5:7b-instruct-q8_0}"
    if ollama list 2>/dev/null | grep -q "$(echo "$MODEL" | cut -d: -f1)"; then
        ok "模型 $MODEL 已存在"
    else
        info "下载模型 $MODEL（首次较慢，或稍后手动 ollama pull）..."
        ollama pull "$MODEL" || warn "模型下载失败，可稍后手动执行：ollama pull $MODEL"
    fi
else
    warn "未检测到 Ollama。本地大脑需要它：brew install ollama （或 https://ollama.com/download）"
    warn "装好后执行：ollama pull qwen2.5:7b-instruct-q8_0"
fi

# ── STEP 5 · Embedding 模型 ──────────────────────────────────
echo ""; echo -e "${BLUE}▶ STEP 5 · 检查 Embedding 模型${NC}"
if [ -d models/bge-m3 ] && [ -n "$(ls -A models/bge-m3 2>/dev/null)" ]; then
    ok "BGE-M3 已就位（随包分发）"
else
    warn "未找到 models/bge-m3，尝试下载（需网络）..."
    ./.venv/bin/python scripts/download_models.py 2>/dev/null || \
        warn "下载失败，请联系分发者获取 models/ 目录，或设 HF_ENDPOINT=https://hf-mirror.com 后重试"
fi

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              🎉 搭建完成！                    ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo "启动：双击 \"Nikon Expert.command\"，或命令行："
echo "  ./.venv/bin/python ui/app.py"
echo "然后浏览器打开 http://localhost:7860"
echo ""
