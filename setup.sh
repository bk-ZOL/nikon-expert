#!/bin/bash
# =============================================================================
# Nikon Expert — 一键环境搭建脚本
# 适用：macOS + Apple Silicon (M1/M2/M3/M4)
# 用法：bash setup.sh
# =============================================================================

set -e  # 任何命令失败立即退出

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; exit 1; }

echo ""
echo -e "${BLUE}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      NIKON EXPERT — 环境初始化脚本           ║${NC}"
echo -e "${BLUE}║      MacBook Air M4 · 本地 RAG 专家系统      ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════╝${NC}"
echo ""

# 获取脚本所在目录（项目根目录）
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "项目目录：$PROJECT_DIR"
cd "$PROJECT_DIR"

# ─────────────────────────────────────────────────────────────
# STEP 0: 检测内存，推荐模型
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 0 · 检测系统配置${NC}"

MEM_GB=$(system_profiler SPHardwareDataType 2>/dev/null | grep "Memory:" | awk '{print $2}')
info "检测到内存：${MEM_GB} GB"

if [ "$MEM_GB" -ge 32 ] 2>/dev/null; then
    RECOMMENDED_MODEL="qwen2.5:32b-instruct-q4_K_M"
    info "推荐模型：Qwen2.5-32B (Q4_K_M，~20GB)"
elif [ "$MEM_GB" -ge 24 ] 2>/dev/null; then
    RECOMMENDED_MODEL="qwen2.5:14b-instruct-q6_K"
    info "推荐模型：Qwen2.5-14B (Q6_K，~12GB)"
else
    RECOMMENDED_MODEL="qwen2.5:7b-instruct-q8_0"
    info "推荐模型：Qwen2.5-7B (Q8，~7GB)"
fi

# ─────────────────────────────────────────────────────────────
# STEP 1: Homebrew
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 1 · 检查 Homebrew${NC}"
if command -v brew &>/dev/null; then
    ok "Homebrew 已安装 ($(brew --version | head -1))"
else
    warn "未检测到 Homebrew，开始安装..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon 路径
    if [ -f /opt/homebrew/bin/brew ]; then
        echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc
        export PATH="/opt/homebrew/bin:$PATH"
    fi
    ok "Homebrew 安装完成"
fi

# ─────────────────────────────────────────────────────────────
# STEP 2: Ollama
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 2 · 检查 Ollama${NC}"
if command -v ollama &>/dev/null; then
    ok "Ollama 已安装"
else
    warn "未检测到 Ollama，通过 Homebrew 安装..."
    brew install ollama
    ok "Ollama 安装完成"
fi

# 启动 Ollama 服务（如果未运行）
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    info "启动 Ollama 后台服务..."
    ollama serve &>/tmp/ollama.log &
    sleep 3
fi
ok "Ollama 服务正在运行"

# ─────────────────────────────────────────────────────────────
# STEP 3: 下载 LLM 模型
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 3 · 下载 LLM 模型${NC}"
echo ""
echo "推荐模型：$RECOMMENDED_MODEL"
echo "（也可手动指定，直接回车使用推荐）"
read -p "请输入模型名 [回车使用推荐]: " USER_MODEL
MODEL="${USER_MODEL:-$RECOMMENDED_MODEL}"

# 更新 .env 中的模型配置
if [ -f .env ]; then
    sed -i '' "s|LLM_MODEL=.*|LLM_MODEL=$MODEL|" .env
fi

if ollama list 2>/dev/null | grep -q "$(echo $MODEL | cut -d: -f1)"; then
    ok "模型 $MODEL 已存在，跳过下载"
else
    info "开始下载 $MODEL（首次下载，请耐心等待）..."
    ollama pull "$MODEL"
    ok "模型下载完成"
fi

# ─────────────────────────────────────────────────────────────
# STEP 4: Miniforge / conda
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 4 · 配置 Python 环境${NC}"

if command -v conda &>/dev/null; then
    ok "conda 已安装 ($(conda --version))"
else
    warn "未检测到 conda，安装 Miniforge..."
    brew install miniforge
    # 初始化 conda
    conda init zsh 2>/dev/null || true
    source ~/.zshrc 2>/dev/null || true
    ok "Miniforge 安装完成"
fi

# 创建/激活环境
ENV_NAME="nikon_expert"
if conda env list | grep -q "^$ENV_NAME "; then
    ok "conda 环境 '$ENV_NAME' 已存在"
else
    info "创建 conda 环境 '$ENV_NAME' (Python 3.11)..."
    conda create -n "$ENV_NAME" python=3.11 -y
    ok "conda 环境创建完成"
fi

info "激活环境并安装依赖..."
# 使用 conda run 在指定环境中执行
conda run -n "$ENV_NAME" pip install -q --upgrade pip

info "安装 PyTorch (MPS 加速)..."
conda run -n "$ENV_NAME" pip install -q torch --index-url https://download.pytorch.org/whl/cpu

info "安装 LlamaIndex..."
conda run -n "$ENV_NAME" pip install -q \
    "llama-index==0.10.67" \
    llama-index-llms-ollama \
    llama-index-embeddings-huggingface \
    llama-index-vector-stores-qdrant \
    llama-index-postprocessor-flag-embedding-reranker

info "安装向量数据库和文档解析..."
conda run -n "$ENV_NAME" pip install -q \
    qdrant-client \
    pymupdf4llm \
    pdfplumber \
    openpyxl \
    python-docx

info "安装 Embedding 模型依赖..."
conda run -n "$ENV_NAME" pip install -q \
    FlagEmbedding \
    sentence-transformers \
    "transformers>=4.40" \
    huggingface_hub

info "安装 API 和 UI 框架..."
conda run -n "$ENV_NAME" pip install -q \
    fastapi \
    "uvicorn[standard]" \
    "gradio>=4.36" \
    "pydantic>=2.7" \
    python-dotenv \
    pyyaml \
    loguru \
    tqdm \
    pandas

ok "所有 Python 依赖安装完成"

# ─────────────────────────────────────────────────────────────
# STEP 5: 下载 Embedding 模型（BGE-M3 + Reranker）
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 5 · 下载 Embedding 模型（BGE-M3 约 2.5GB）${NC}"

mkdir -p models

conda run -n "$ENV_NAME" python3 - << 'PYEOF'
import os, sys
from pathlib import Path

models_dir = Path("models")

# BGE-M3
bge_path = models_dir / "bge-m3"
if bge_path.exists() and any(bge_path.iterdir()):
    print("✅ BGE-M3 已存在，跳过")
else:
    print("⏳ 下载 BGE-M3（约 2.5GB）...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id="BAAI/bge-m3",
            local_dir=str(bge_path),
            ignore_patterns=["*.pt", "flax_*", "tf_*", "*.msgpack"],
        )
        print("✅ BGE-M3 下载完成")
    except Exception as e:
        print(f"⚠️  BGE-M3 下载失败：{e}")
        print("   请稍后手动运行：python scripts/download_models.py")

# BGE-Reranker
reranker_path = models_dir / "bge-reranker-v2-m3"
if reranker_path.exists() and any(reranker_path.iterdir()):
    print("✅ BGE-Reranker 已存在，跳过")
else:
    print("⏳ 下载 BGE-Reranker-v2-m3（约 1.1GB）...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id="BAAI/bge-reranker-v2-m3",
            local_dir=str(reranker_path),
            ignore_patterns=["*.pt", "flax_*", "tf_*", "*.msgpack"],
        )
        print("✅ BGE-Reranker 下载完成")
    except Exception as e:
        print(f"⚠️  Reranker 下载失败：{e}")
PYEOF

# ─────────────────────────────────────────────────────────────
# STEP 6: 配置 Obsidian Vault 路径
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}▶ STEP 6 · 配置 Obsidian Vault 路径${NC}"

CURRENT_VAULT=$(grep "^OBSIDIAN_VAULT_PATH=" .env 2>/dev/null | cut -d= -f2)
echo ""
echo "请输入你的 Obsidian Vault 完整路径"
echo "（例如：/Users/你的用户名/Documents/MyVault）"
if [ -n "$CURRENT_VAULT" ]; then
    echo "当前配置：$CURRENT_VAULT"
fi
read -p "Vault 路径 [回车跳过]: " VAULT_PATH

if [ -n "$VAULT_PATH" ]; then
    if [ -d "$VAULT_PATH" ]; then
        sed -i '' "s|OBSIDIAN_VAULT_PATH=.*|OBSIDIAN_VAULT_PATH=$VAULT_PATH|" .env
        ok "Vault 路径已保存：$VAULT_PATH"
    else
        warn "路径不存在，请稍后在 .env 中手动填写 OBSIDIAN_VAULT_PATH"
    fi
fi

# ─────────────────────────────────────────────────────────────
# 完成
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            🎉 环境搭建完成！                 ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo "下一步操作："
echo ""
echo -e "  ${YELLOW}1. 摄入 Obsidian 知识库：${NC}"
echo -e "     bash ingest.sh"
echo ""
echo -e "  ${YELLOW}2. 启动交互界面：${NC}"
echo -e "     bash start.sh"
echo ""
echo -e "  ${YELLOW}3. 命令行快速测试：${NC}"
echo -e "     bash query.sh \"你的问题\""
echo ""
