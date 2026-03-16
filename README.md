# 🔬 Nikon Expert — 本地化光刻机专家系统

> 纯本地运行 · 零数据外传 · 中英日三语 · 带溯源引用

---

## ⚡ 快速开始（3 步完成）

### 第 1 步：环境初始化（仅需一次，约 1-3 小时）
```bash
cd nikon_expert
bash setup.sh
```
脚本会自动：安装 Homebrew → Ollama → 下载 LLM 模型 → 配置 Python 环境 → 下载 Embedding 模型

### 第 2 步：摄入 Obsidian 知识库
```bash
bash ingest.sh /path/to/your/ObsidianVault
```

### 第 3 步：启动界面
```bash
bash start.sh
```
浏览器打开 http://localhost:7860

---

## 📋 日常使用

| 操作 | 命令 |
|------|------|
| 启动图形界面 | `bash start.sh` |
| 命令行提问 | `bash query.sh "你的问题"` |
| 故障排查模式 | `bash query.sh "E-5301 报警" --troubleshoot` |
| 交互模式 | `bash query.sh` |
| 摄入 Obsidian | `bash ingest.sh` |
| 摄入 PDF 手册 | 将 PDF 放入 `data/raw/manuals/`，然后 `bash ingest.sh` 选择选项 2 |
| 摄入 Checksheet | 将 Excel 放入 `data/raw/checksheets/`，然后 `bash ingest.sh` 选择选项 2 |

---

## 📁 项目结构

```
nikon_expert/
├── setup.sh          # 一键环境搭建
├── ingest.sh         # 数据摄入
├── start.sh          # 启动图形界面
├── query.sh          # 命令行查询
├── .env              # 配置文件（模型、路径、参数）
├── requirements.txt  # Python 依赖
│
├── src/
│   ├── engine.py     # 核心查询引擎
│   ├── ingestor.py   # 数据摄入器（Obsidian/PDF/Excel/故障履历）
│   └── prompts.py    # Prompt 模板（零幻觉约束）
│
├── scripts/
│   ├── ingest_obsidian.py   # Obsidian 摄入
│   ├── ingest_all.py        # 全量摄入
│   ├── query_cli.py         # CLI 查询工具
│   └── download_models.py   # 手动下载模型
│
├── ui/
│   └── app.py        # Gradio 图形界面
│
└── data/
    ├── raw/
    │   ├── manuals/       # 放 PDF 手册
    │   ├── checksheets/   # 放 Excel Checksheet
    │   └── fault_history/ # 放故障履历 JSON/CSV
    └── qdrant_db/         # 向量数据库（自动生成）
```

---

## ⚙️ 配置说明（.env）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `LLM_MODEL` | Ollama 模型名 | `qwen2.5:14b-instruct-q6_K` |
| `OBSIDIAN_VAULT_PATH` | Obsidian Vault 路径 | 空（需手动填写） |
| `CONFIDENCE_THRESHOLD` | 检索置信度阈值，低于此值不返回结果 | `0.30` |
| `RETRIEVAL_TOP_K` | 初始检索数量 | `8` |
| `RERANK_TOP_N` | 重排序后保留数量 | `4` |

---

## 🔧 常见问题

**Q: 模型下载很慢怎么办？**
A: 配置国内镜像：`export HF_ENDPOINT=https://hf-mirror.com`，然后重新运行 `python scripts/download_models.py`

**Q: 查询无结果 / 置信度太低？**
A: 在 `.env` 中降低 `CONFIDENCE_THRESHOLD` 到 `0.20` 试试

**Q: 响应速度慢？**
A: 换更小的模型：编辑 `.env` 中的 `LLM_MODEL` 为 `qwen2.5:7b-instruct-q8_0`

**Q: 如何更新知识库（新增笔记后）？**
A: 直接再次运行 `bash ingest.sh`，新内容会追加到现有向量库

**Q: 想用 GPU 服务器扩展？**
A: 在服务器上安装 vLLM / Ollama，将 `.env` 中的 `LLM_BASE_URL` 改为服务器地址，其他代码无需修改
