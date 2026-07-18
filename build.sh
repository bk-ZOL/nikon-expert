#!/bin/bash
# =============================================================================
# Nikon Expert — 构建分发包（venv 版，开箱即用）
# 用法：bash build.sh
# 产出：dist/Nikon-Expert-<date>.tar.gz
#   含：代码 + Embedding 模型 + 知识库(向量库/FTS) + venv 一键脚本 + 启动器
#   不含：真实 .env（含 API key）、conda、__pycache__、.git
# =============================================================================
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE=$(date +%Y%m%d)
DIST_NAME="Nikon-Expert-${DATE}"
DIST_DIR="${PROJECT_DIR}/dist/${DIST_NAME}"

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Nikon Expert — 构建分发包（venv）      ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. 清理并建目录 ────────────────────────────────────────────
info "清理旧构建..."
rm -rf "${PROJECT_DIR}/dist"
mkdir -p "$DIST_DIR"

# ── 2. 复制代码（排除缓存/密钥/大目录）─────────────────────────
info "复制项目代码..."
COPY_EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store'
               --exclude='.git' --exclude='.venv' --exclude='dist')
rsync -a "${COPY_EXCLUDES[@]}" "${PROJECT_DIR}/src"     "$DIST_DIR/"
rsync -a "${COPY_EXCLUDES[@]}" "${PROJECT_DIR}/ui"      "$DIST_DIR/"
rsync -a "${COPY_EXCLUDES[@]}" "${PROJECT_DIR}/scripts" "$DIST_DIR/"
cp "${PROJECT_DIR}/requirements.txt" "$DIST_DIR/"
cp "${PROJECT_DIR}/setup_venv.sh"    "$DIST_DIR/"
cp "${PROJECT_DIR}/ingest.sh"        "$DIST_DIR/" 2>/dev/null || true
cp "${PROJECT_DIR}/start.sh"         "$DIST_DIR/" 2>/dev/null || true
cp "${PROJECT_DIR}/query.sh"         "$DIST_DIR/" 2>/dev/null || true

# ── 3. 安全：只发脱敏模板，绝不发真实 .env ─────────────────────
info "生成脱敏配置模板（不含 API key）..."
cp "${PROJECT_DIR}/.env.example" "$DIST_DIR/.env.example"
# 兜底：确保 .env.example 里没有残留 key
if grep -qE '^[A-Z_]*API_KEY=.+' "$DIST_DIR/.env.example"; then
    warn "  .env.example 里检测到疑似 key，已清空其值"
    sed -i '' -E 's/^([A-Z_]*API_KEY=).+/\1/' "$DIST_DIR/.env.example"
fi
ok "  仅打包 .env.example（同事 setup 时自动生成 .env）"

# ── 4. 空知识库骨架（同事各自导入自己的知识，不带本机的库）─────
info "创建空知识库目录（同事各自导入自己的知识）..."
mkdir -p "$DIST_DIR/data/raw/manuals" "$DIST_DIR/data/raw/checksheets" "$DIST_DIR/data/raw/fault_history"
ok "  data/ 骨架已建（首次导入时自动生成向量库/FTS）"

# ── 5. 复制 Embedding 模型（瘦身：去 onnx/图片/缓存，只留 torch 权重）──
info "复制 Embedding 模型（已瘦身，请稍候）..."
# 排除项：onnx 导出(2.1G，我们用 torch 不用它)、README 图片、HF 缓存
MODEL_EXCLUDES=(--exclude='onnx' --exclude='onnx/***' --exclude='imgs'
                --exclude='.cache' --exclude='*.jpg' --exclude='*.jpeg'
                --exclude='*.png' --exclude='*.gif')
copy_model() {
    local src="$1" dst="$2" name="$3"
    if [ -L "$src" ]; then src="$(readlink "$src")"; fi
    if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
        rsync -a "${MODEL_EXCLUDES[@]}" "$src/" "$dst/"
        ok "  $name 已打包（瘦身）"
    else
        warn "  $name 未找到，跳过（同事需自行下载）"
    fi
}
mkdir -p "$DIST_DIR/models/bge-m3" "$DIST_DIR/models/bge-reranker-v2-m3"
copy_model "${PROJECT_DIR}/models/bge-m3"            "$DIST_DIR/models/bge-m3"            "BGE-M3"
copy_model "${PROJECT_DIR}/models/bge-reranker-v2-m3" "$DIST_DIR/models/bge-reranker-v2-m3" "BGE-Reranker"

# ── 6. macOS 双击启动器（venv，自动首次安装）──────────────────
info "生成启动器..."
cat > "$DIST_DIR/Nikon Expert.command" << 'LAUNCHER'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 首次运行：自动搭建 venv 环境
if [ ! -x ".venv/bin/python" ]; then
    echo "首次启动，正在搭建环境（几分钟）..."
    bash setup_venv.sh || { echo "❌ 环境搭建失败，见上方日志"; read -p "按回车退出..."; exit 1; }
fi

# 确保 Ollama 在跑（本地大脑）
if command -v ollama &>/dev/null && ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    ollama serve &>/tmp/ollama.log &
    sleep 2
fi

echo "🌐 启动中… 浏览器将打开 http://localhost:7860"
exec ./.venv/bin/python ui/app.py
LAUNCHER
chmod +x "$DIST_DIR/Nikon Expert.command"
ok "  启动器已生成（venv，首次自动安装）"

# ── 7. 安装说明 ────────────────────────────────────────────────
info "生成安装说明..."
cat > "$DIST_DIR/INSTALL.md" << 'INSTALLDOC'
# Nikon Expert 安装指南（venv 轻量版）

## 一次性前置（每台电脑一次）

1. **Python 3.11**（macOS 通常已带，或 `brew install python@3.11`）
2. **Ollama**（本地大脑）：`brew install ollama` 或 https://ollama.com/download
3. **拉一个模型**：`ollama pull qwen2.5:7b-instruct-q8_0`

> Embedding 模型和知识库已随包附带，无需下载。

## 安装 & 启动（推荐）

**双击 `Nikon Expert.command`** —— 首次会自动创建 `.venv`、装依赖，然后启动。
浏览器打开 http://localhost:7860。

## 命令行方式

```bash
cd Nikon-Expert
bash setup_venv.sh        # 首次：建 .venv + 装依赖
./.venv/bin/python ui/app.py
```

## 用外接大脑（可选，Claude / DeepSeek / 千问 …）

默认用本地 Ollama（数据零外传）。想用云端大脑：
编辑 `.env`，填入对应 `API_KEY`，界面「⚙️ 设置」里切换即可。
（⚠️ 云端会把"检索片段+问题"发往该 API；embedding/检索始终本地。）

## 导入你自己的知识（首次是空库）

本包**不含任何知识库内容**——每个人导入自己的资料即可：
- 界面「📂 知识库」→ 上传 PDF/Markdown，或「导入 OKF」填 OKF 目录路径
- 命令行：`./.venv/bin/python scripts/ingest_okf.py /path/to/okf`

导入前提问会提示"知识库为空"，属正常。
INSTALLDOC
ok "  INSTALL.md 已生成"

# ── 8. 打包 ────────────────────────────────────────────────────
info "压缩打包..."
cd "${PROJECT_DIR}/dist"
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' -czf "${DIST_NAME}.tar.gz" "$DIST_NAME"
SIZE=$(du -sh "${DIST_NAME}.tar.gz" | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            构建完成！                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  📦 dist/${DIST_NAME}.tar.gz  (${SIZE})"
echo -e "  🔒 已排除真实 .env / API key / __pycache__ / .git"
echo -e "  🧠 内置 Embedding 模型；知识库留空（同事各自导入自己的知识）"
echo ""
echo "  同事：解压 → 双击 \"Nikon Expert.command\"（首次自动装环境）"
echo ""
