# Nikon Expert — 本地化光刻机专家系统

> 纯本地运行 · 零数据外传 · 中英日三语 · 带溯源引用

---

## 架构

三层融合检索：Karpathy 式全文搜索 + Gbrain 式语义索引 + RAG 生成

```
用户提问 → 智能路由（自动分类）
               ├── 全文搜索层 (SQLite FTS5)  — 关键词精确匹配，Error Code 秒查
               └── 语义索引层 (Qdrant + BGE-M3) — 理解语义，概念性问答
          → 结果融合 → 去重排序 → LLM 生成回答（带引用）
```

**自动识别查询类型：**
- 含 Error Code 或故障关键词 → 故障排查模式
- 概念/原理类问题 → 知识问答模式
- 无需手动切换

---

## 快速开始（3 步完成）

### 第 1 步：环境初始化（仅需一次）
```bash
cd nikon-expert
bash setup.sh
```

### 第 2 步：安装 Ollama 并下载模型
```bash
brew install ollama        # 或从 ollama.com 下载
ollama pull qwen2.5:7b-instruct-q8_0
```

### 第 3 步：启动
```bash
# 方式 A：双击启动器（macOS）
open "Nikon Expert.command"

# 方式 B：命令行
conda activate nikon_expert
python ui/app.py
```

启动后自动弹出原生窗口（无浏览器地址栏），或访问 http://localhost:7860

---

## 数据摄入

### 全文搜索索引（推荐先做）
在「知识库」页面点击「选择目录」，指向你的 Obsidian Vault 或笔记目录。
- 直接索引原始文件，不向量化，秒级完成
- 支持 `.md`、`.txt`、`.pdf`
- 适合 Error Code 精确查找、关键词搜索

### 向量化摄入（RAG）
上传 PDF 手册或 Markdown 文件，系统自动：
- 按章节结构分块 → Embedding 向量化 → 存入向量库
- 同时写入全文索引
- 摄入后立即可被检索

### 命令行摄入
```bash
# 摄入 Obsidian Vault
bash ingest.sh /path/to/your/vault

# 命令行提问
bash query.sh "E-5301 报警如何排查？"
```

---

## 项目结构

```
nikon-expert/
├── src/
│   ├── engine.py      # 核心引擎（路由融合检索 + LLM 生成）
│   ├── fulltext.py    # 全文搜索层（SQLite FTS5）
│   ├── router.py      # 智能路由（查询分类 + 双路召回融合）
│   ├── ingestor.py    # 数据摄入器（结构感知分块 + 去重 + FTS 同步）
│   └── prompts.py     # Prompt 模板
├── ui/
│   └── app.py         # 原生窗口界面（Gradio + pywebview）
├── scripts/           # 命令行工具
├── models/            # Embedding 模型（BGE-M3 + Reranker）
├── data/
│   ├── qdrant_db/     # 向量数据库
│   └── fts.db         # 全文搜索数据库
├── build.sh           # 一键打包分发包
├── setup.sh           # 环境初始化
├── .env               # 配置文件
└── requirements.txt   # Python 依赖
```

---

## 配置说明（.env）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `LLM_MODEL` | Ollama 模型名 | `qwen2.5:7b-instruct-q8_0` |
| `LLM_BASE_URL` | Ollama 地址 | `http://localhost:11434` |
| `EMBED_MODEL_PATH` | BGE-M3 模型路径 | `./models/bge-m3` |
| `QDRANT_PATH` | 向量数据库路径 | `./data/qdrant_db` |
| `FTS_DB_PATH` | 全文搜索数据库路径 | `./data/fts.db` |
| `PARENT_CHUNK_SIZE` | PDF 父块大小 | `1024` |
| `CHILD_CHUNK_SIZE` | PDF 子块大小 | `256` |
| `CONFIDENCE_THRESHOLD` | 检索置信度阈值 | `0.30` |
| `RETRIEVAL_TOP_K` | 初始检索数量 | `8` |
| `RERANK_TOP_N` | 重排序保留数量 | `4` |

---

## 分发给同事

```bash
bash build.sh
# 产出 dist/Nikon-Expert-<日期>.tar.gz，含模型 + 启动器 + 安装说明
```

同事解压后执行 `bash setup.sh`，双击 `Nikon Expert.command` 即可使用。

---

## 常见问题

**Q: 模型下载很慢？**
A: 配置镜像：`export HF_ENDPOINT=https://hf-mirror.com`，然后 `python scripts/download_models.py`

**Q: 查询无结果？**
A: 先在知识库页面做全文索引或上传文档。如果已有数据仍无结果，可降低 `.env` 中 `CONFIDENCE_THRESHOLD` 到 `0.20`

**Q: 响应慢？**
A: 换更小的模型：编辑 `.env` 中 `LLM_MODEL` 为 `qwen2.5:7b-instruct-q8_0`

**Q: 如何更新知识库？**
A: 重新运行全文索引或上传新文件，数据自动追加

**Q: 想用 GPU 服务器？**
A: 在服务器装 Ollama/vLLM，将 `.env` 中 `LLM_BASE_URL` 改为服务器地址
