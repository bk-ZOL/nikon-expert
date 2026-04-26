#!/bin/bash
# =============================================================================
# Nikon Expert — 构建分发包
# 用法：bash build.sh
# 产出：dist/Nikon-Expert-<date>.tar.gz（含代码 + 模型 + 启动器）
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE=$(date +%Y%m%d)
DIST_NAME="Nikon-Expert-${DATE}"
DIST_DIR="${PROJECT_DIR}/dist/${DIST_NAME}"

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     Nikon Expert — 构建分发包            ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. 清理旧构建 ──────────────────────────────────────────────
info "清理旧构建..."
rm -rf "${PROJECT_DIR}/dist"
mkdir -p "$DIST_DIR"

# ── 2. 复制项目文件 ────────────────────────────────────────────
info "复制项目代码..."
cp -r "${PROJECT_DIR}/src" "$DIST_DIR/"
cp -r "${PROJECT_DIR}/ui" "$DIST_DIR/"
cp -r "${PROJECT_DIR}/scripts" "$DIST_DIR/"
cp "${PROJECT_DIR}/setup.sh" "$DIST_DIR/"
cp "${PROJECT_DIR}/ingest.sh" "$DIST_DIR/"
cp "${PROJECT_DIR}/start.sh" "$DIST_DIR/"
cp "${PROJECT_DIR}/query.sh" "$DIST_DIR/"
cp "${PROJECT_DIR}/requirements.txt" "$DIST_DIR/"
cp "${PROJECT_DIR}/.env" "$DIST_DIR/"

# 创建必要目录
mkdir -p "$DIST_DIR/models"
mkdir -p "$DIST_DIR/data/raw/manuals"
mkdir -p "$DIST_DIR/data/raw/checksheets"
mkdir -p "$DIST_DIR/data/raw/fault_history"

# ── 3. 复制模型文件（解引用符号链接）────────────────────────────
info "复制 Embedding 模型..."

BGE_M3_SRC="${PROJECT_DIR}/models/bge-m3"
if [ -L "$BGE_M3_SRC" ]; then
    # 解引用软链接，复制真实文件
    real_path=$(readlink "$BGE_M3_SRC")
    if [ -d "$real_path" ] && [ -f "$real_path/pytorch_model.bin" ]; then
        info "  BGE-M3 源：$real_path"
        cp -r "$real_path" "$DIST_DIR/models/bge-m3/"
        ok "  BGE-M3 复制完成"
    else
        warn "  BGE-M3 软链接指向无效路径：$real_path"
    fi
elif [ -d "$BGE_M3_SRC" ] && [ -f "$BGE_M3_SRC/pytorch_model.bin" ]; then
    cp -r "$BGE_M3_SRC" "$DIST_DIR/models/bge-m3/"
    ok "  BGE-M3 复制完成"
else
    warn "  BGE-M3 模型未找到，跳过"
fi

BGE_RERANKER_SRC="${PROJECT_DIR}/models/bge-reranker-v2-m3"
if [ -L "$BGE_RERANKER_SRC" ]; then
    real_path=$(readlink "$BGE_RERANKER_SRC")
    if [ -d "$real_path" ]; then
        cp -r "$real_path" "$DIST_DIR/models/bge-reranker-v2-m3/"
        ok "  BGE-Reranker 复制完成"
    else
        warn "  BGE-Reranker 软链接指向无效路径：$real_path"
    fi
elif [ -d "$BGE_RERANKER_SRC" ]; then
    cp -r "$BGE_RERANKER_SRC" "$DIST_DIR/models/bge-reranker-v2-m3/"
    ok "  BGE-Reranker 复制完成"
fi

# ── 4. 生成 macOS 双击启动器 ───────────────────────────────────
info "生成启动器..."
cat > "$DIST_DIR/Nikon Expert.command" << 'LAUNCHER'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查 conda
if ! command -v conda &>/dev/null; then
    echo "❌ 未检测到 conda，请先运行 setup.sh 安装环境"
    echo "   cd \"$SCRIPT_DIR\" && bash setup.sh"
    read -p "按回车退出..."
    exit 1
fi

# 激活环境（不存在则提示安装）
if conda env list | grep -q "^nikon_expert "; then
    eval "$(conda shell.bash hook)"
    conda activate nikon_expert
else
    echo "❌ conda 环境 'nikon_expert' 不存在"
    echo "   请先运行: cd \"$SCRIPT_DIR\" && bash setup.sh"
    read -p "按回车退出..."
    exit 1
fi

# 启动应用
python3 -u ui/app.py
LAUNCHER
chmod +x "$DIST_DIR/Nikon Expert.command"
ok "  启动器已生成"

# ── 5. 导出 conda 环境配置 ──────────────────────────────────────
info "导出 conda 环境配置..."
conda env export -n nikon_expert --no-builds > "$DIST_DIR/environment.yml" 2>/dev/null || true
ok "  environment.yml 已生成"

# ── 6. 生成安装说明 ────────────────────────────────────────────
info "生成安装说明..."
cat > "$DIST_DIR/INSTALL.md" << 'INSTALLDOC'
# Nikon Expert 安装指南

## 前置要求（仅需一次）

### 1. 安装 Homebrew（如已有跳过）
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. 安装 Miniforge（conda 环境）
```bash
brew install miniforge
conda init zsh
# 重启终端使生效
```

### 3. 安装 Ollama（本地 LLM）
- 方式 A：`brew install ollama`，安装后打开 Ollama 应用
- 方式 B：从 https://ollama.com/download 下载

### 4. 下载 LLM 模型
打开终端执行：
```bash
ollama pull qwen2.5:7b-instruct-q8_0
```

## 安装

### 5. 运行安装脚本
```bash
cd Nikon-Expert
bash setup.sh
```
脚本会自动：创建 conda 环境 → 安装 Python 依赖 → 配置路径

（注意：模型文件已包含在包内，无需重新下载。）

## 启动

### 方式 A：双击启动
双击 `Nikon Expert.command` 即可。

### 方式 B：命令行启动
```bash
cd Nikon-Expert
conda activate nikon_expert
python ui/app.py
```

浏览器打开 http://localhost:7860

## 日常使用

1. **全文搜索索引**（推荐先做）：在「知识库」页面点击「选择目录」，指向你的笔记目录，索引秒完成
2. **上传文档**：PDF 手册或 Markdown 文件上传后自动向量化
3. **直接提问**：输入问题即可，系统自动识别问答/故障排查模式
INSTALLDOC

# ── 7. 打包 ──────────────────────────────────────────────────────
info "打包压缩..."
cd "${PROJECT_DIR}/dist"
tar -czf "${DIST_NAME}.tar.gz" "$DIST_NAME"
SIZE=$(du -sh "${DIST_NAME}.tar.gz" | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            构建完成！                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  📦 分发包：dist/${DIST_NAME}.tar.gz"
echo -e "  📏 大小：${SIZE}"
echo ""
echo "  分发给同事："
echo "  1. 传输 tar.gz 文件（U盘/网盘/内网）"
echo "  2. 同事解压后阅读 INSTALL.md"
echo "  3. 双击 Nikon Expert.command 启动"
echo ""
