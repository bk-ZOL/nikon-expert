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

## 云端部署 + Claude 接入（MCP）

除本地 Gradio 界面外，本项目可部署到云端并作为 **MCP 服务**接入 Claude（Claude Code 与 claude.ai 网页版），让 Claude 直接调用知识库、识读电路图。

### 两种接入形态
| | Claude Code（本地 CLI） | claude.ai 网页版 |
|---|---|---|
| 传输 | `mcp_server.py`（stdio） | `mcp_http_server.py`（streamable-http） |
| 接法 | `claude mcp add` | 设置 → Connectors → 远程 URL |
| 前置 | 无 | 公网 HTTPS（Cloudflare Tunnel 等） |

`mcp_http_server.py` 复用 `mcp_server.py` 的工具，仅传输不同；`mcp_tools_extra.py` 注册额外工具。

### MCP 工具（共 11 个）
- `nikon_query` / `nikon_search` / `nikon_search_error_code` — 检索本地库
- `nikon_view_diagram` — **电路图识图/追线**：按线号/板号/页号精确取页 → 高清渲染回传 + 完整文字层
- `nikon_ingest` / `nikon_list_documents` / `nikon_delete_document` / `nikon_get_status` / `nikon_reindex_diagrams`
- `nikon_ask_wps` — 直接问 WPS/金山知识库（覆盖只存在于 WPS 在线文档、无法本地摄入的内容）
- `nikon_query_worklog` — **实时读飞书工作日志**：现查飞书云文档返回最新原文（近 N 天/日期范围），适合"最近日报写了啥"；历史日志的语义检索走 `nikon_query`

### 电路图"追线路"（`nikon_view_diagram` + 标号倒排）
- 配置驱动（`src/diagram_config.py`），不硬编码机型/页号/命名规律；新机型只加规则
- ingest 时抽取线号/板号/连接器号建**倒排索引**（`src/diagram_index.py`，存于 `data/fts.db`），支持增量
- 按**元件/信号/线号/页号/图号**精确定位页 → `figures.render_pdf_page` 渲染成图交给 Claude 视觉识读
- 重建索引：`python scripts/build_diagram_index.py`（增量 / `--full`）

### Qdrant 服务器模式（多进程并发）
本地文件版 Qdrant 单写入；云端需网页版 + MCP 同时访问时，改用 Qdrant server：
```bash
docker run -d --name nikon-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -v <data>/qdrant_srv:/qdrant/storage qdrant/qdrant
```
在 `.env` 设 `QDRANT_URL=http://127.0.0.1:6333`，各组件自动改走服务器（未设则回退本地文件模式，行为不变）。

### WPS/金山知识库自动增量同步
将 WPS kwiki 知识库单向同步进本地库（Qdrant + FTS），供检索/识图统一使用：
- `scripts/sync_kwiki.py` — 镜像 + 增量检测 + 按类型分发摄入（原用 kwiki-cli）
- `scripts/wps_api.py` — 纯 Python 直连 WPS REST API（`X-Kwiki-Auth`），**无需 kwiki-cli 二进制**，适合无桌面的服务器
- `scripts/sync_wps.py` — 云端入口（复用 sync_kwiki 逻辑 + HTTP 传输）
- 配置：`.kwiki_env`（chmod 600）放 `X_KWIKI_AUTH` / `KWIKI_KB_KUID` / `MACHINE_MODEL`
- 定时：systemd timer 每 30 分钟增量同步
- 限制：WPS "在线文档"（doc_type=f）/ OTL 智能文档 API 无法下载，改用 `nikon_ask_wps` 直接问 WPS 自带 RAG

### 飞书工作日志接入（A 灌库 + B 实时，两路互补）
把飞书云文档里的**现场工作日志**接进系统，既能语义检索历史、又能实时看最近动态：
- **A 路 · 历史灌库**（进向量库，`nikon_query` 可检索）
  - `scripts/feishu_client.py` — wiki/docx 链接 → `document_id`，读 `raw_content`，**按日期切块**（每天一条日报，容错 `2026/08/5`、`08/03` 混合格式）
  - `src/ingest_worklog.py` — 复用 `ingestor` 的存储/去重/FTS 助手；metadata 打 `doc_type=feishu_worklog` + `source` + `feishu_doc_id` + `date` + `last_edited`
  - `scripts/sync_feishu.py` — **内容哈希增量**（变了才"按 doc_name 删旧 + 全量重灌"，不产生重复向量）；state 存 `data/feishu_sync_state.json`
- **B 路 · 实时工具** `nikon_query_worklog` — 现查飞书返回原文，不入库，永远最新
- **认证** `scripts/feishu_auth.py` — 私有个人文档须 **user_access_token**（OAuth 2.0，浏览器授权一次缓存到 `.feishu_token.json`）；含 `refresh_token` 自动续期能力（需飞书应用开通 `offline_access`）
- 配置：`.feishu_env`（chmod 600）放 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_WORKLOG_URL`
- 首次授权：`python scripts/feishu_auth.py login`（须在有浏览器的机器上跑）；同步：`python scripts/sync_feishu.py`

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
| `QDRANT_PATH` | 向量数据库路径（本地文件模式） | `./data/qdrant_db` |
| `QDRANT_URL` | Qdrant 服务器地址（设了则用 server 模式，多进程并发） | 空（=文件模式） |
| `FTS_DB_PATH` | 全文搜索数据库路径 | `./data/fts.db` |
| `LLM_PROVIDER` | 云端大脑 provider（claude/deepseek/…，空=本地 Ollama） | 空 |
| `MCP_HOST` / `MCP_PORT` | MCP streamable-http 绑定地址 | `127.0.0.1` / `8000` |
| `X_KWIKI_AUTH` | WPS kwiki token（放 `.kwiki_env`，勿入库） | — |
| `KWIKI_KB_KUID` | 要同步的 WPS 知识库 kuid（`0s` 开头） | — |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据（放 `.feishu_env`，勿入库） | — |
| `FEISHU_WORKLOG_URL` | 飞书工作日志文档链接（wiki/docx）或 document_id | — |
| `FEISHU_SCOPES` | OAuth scope（读文档需 `docx`/`drive`/`wiki` 只读；自动续期加 `offline_access`） | 见 `.feishu_env` |
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
