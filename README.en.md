# Nikon Expert — On-Premise Lithography Equipment Expert System

[简体中文](README.md) | **English**

> Runs fully locally · Zero data exfiltration · Chinese / English / Japanese · Answers with source citations

---

## Architecture

Three-layer hybrid retrieval: Karpathy-style full-text search + Gbrain-style semantic index + RAG generation

```
User question → smart router (auto-classify)
               ├── Full-text layer (SQLite FTS5)   — exact keyword match, instant error-code lookup
               └── Semantic layer (Qdrant + BGE-M3) — meaning-aware, conceptual Q&A
          → fuse results → dedup & rank → LLM answer (with citations)
```

**Query type is auto-detected:**
- Contains an error code or fault keyword → troubleshooting mode
- Conceptual / principle question → knowledge Q&A mode
- No manual switching needed

---

## Cloud Deployment + Claude Integration (MCP)

Besides the local Gradio UI, the project can be deployed to the cloud and exposed as an **MCP service** for Claude (both Claude Code and claude.ai web), letting Claude query the knowledge base and read circuit diagrams directly.

### Two integration forms
| | Claude Code (local CLI) | claude.ai (web) |
|---|---|---|
| Transport | `mcp_server.py` (stdio) | `mcp_http_server.py` (streamable-http) |
| How to add | `claude mcp add` | Settings → Connectors → remote URL |
| Prerequisite | none | public HTTPS (e.g. Cloudflare Tunnel) |

`mcp_http_server.py` reuses the tools from `mcp_server.py` (transport differs only); `mcp_tools_extra.py` registers additional tools.

### MCP tools (11 total)
- `nikon_query` / `nikon_search` / `nikon_search_error_code` — search the local library
- `nikon_view_diagram` — **circuit-diagram vision / trace**: locate a page by wire / board / page number → return a high-res render + full text layer
- `nikon_ingest` / `nikon_list_documents` / `nikon_delete_document` / `nikon_get_status` / `nikon_reindex_diagrams`
- `nikon_ask_wps` — ask the WPS / Kingsoft knowledge base directly (covers content that only lives in WPS online docs and cannot be ingested locally)
- `nikon_query_worklog` — **read the Feishu worklog live**: fetch the latest raw entries from the Feishu doc (last N days / date range); ideal for "what did the recent report say", while semantic search over historical logs goes through `nikon_query`

### Circuit-diagram "trace" (`nikon_view_diagram` + identifier inverted index)
- Config-driven (`src/diagram_config.py`), no hard-coded model / page / naming rules; a new model only adds rules
- On ingest, wire / board / connector IDs are extracted into an **inverted index** (`src/diagram_index.py`, stored in `data/fts.db`), supports incremental updates
- Locate a page precisely by **component / signal / wire / page / figure number** → `figures.render_pdf_page` renders it and hands the image to Claude's vision
- Rebuild index: `python scripts/build_diagram_index.py` (incremental / `--full`)

### Qdrant server mode (multi-process concurrency)
The local file-based Qdrant is single-writer; when the web UI + MCP must access it simultaneously in the cloud, switch to a Qdrant server:
```bash
docker run -d --name nikon-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -v <data>/qdrant_srv:/qdrant/storage qdrant/qdrant
```
Set `QDRANT_URL=http://127.0.0.1:6333` in `.env`; components switch to server mode automatically (falls back to local file mode when unset — behavior unchanged).

### WPS / Kingsoft knowledge base auto incremental sync
One-way sync from a WPS kwiki knowledge base into the local library (Qdrant + FTS) for unified retrieval / diagram use:
- `scripts/sync_kwiki.py` — mirror + incremental detection + type-dispatched ingest (originally via kwiki-cli)
- `scripts/wps_api.py` — pure-Python direct WPS REST API client (`X-Kwiki-Auth`), **no kwiki-cli binary needed**, suitable for headless servers
- `scripts/sync_wps.py` — cloud entry point (reuses sync_kwiki logic + HTTP transport)
- Config: `.kwiki_env` (chmod 600) holds `X_KWIKI_AUTH` / `KWIKI_KB_KUID` / `MACHINE_MODEL`
- Schedule: systemd timer, incremental sync every 30 min
- Limitation: WPS "online docs" (doc_type=f) / OTL smart docs cannot be downloaded via API — use `nikon_ask_wps` to query the WPS built-in RAG instead

### Feishu worklog integration (A ingest + B live, complementary)
Bring the field **worklog** from a Feishu doc into the system — both semantic search over history and a live view of recent activity:
- **Path A · historical ingest** (into the vector store, retrievable via `nikon_query`)
  - `scripts/feishu_client.py` — wiki/docx link → `document_id`, read `raw_content`, **chunk by date** (one entry per day, tolerant of mixed formats like `2026/08/5`, `08/03`)
  - `src/ingest_worklog.py` — reuses `ingestor`'s storage / dedup / FTS helpers; metadata tags `doc_type=feishu_worklog` + `source` + `feishu_doc_id` + `date` + `last_edited`
  - `scripts/sync_feishu.py` — **content-hash incremental** (re-ingest only when changed: delete-by-doc_name then full re-ingest, avoiding duplicate vectors); state in `data/feishu_sync_state.json`
- **Path B · live tool** `nikon_query_worklog` — fetches raw entries from Feishu on demand, not stored, always current
- **Auth** `scripts/feishu_auth.py` — private personal docs require a **user_access_token** (OAuth 2.0, browser consent once, cached to `.feishu_token.json`); supports `refresh_token` auto-renewal (requires the Feishu app to grant `offline_access`)
- Config: `.feishu_env` (chmod 600) holds `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_WORKLOG_URL`
- First authorization: `python scripts/feishu_auth.py login` (must run on a machine with a browser); sync: `python scripts/sync_feishu.py`

---

## Quick Start (3 steps)

### Step 1: Initialize the environment (once)
```bash
cd nikon-expert
bash setup.sh
```

### Step 2: Install Ollama and pull a model
```bash
brew install ollama        # or download from ollama.com
ollama pull qwen2.5:7b-instruct-q8_0
```

### Step 3: Launch
```bash
# Option A: double-click launcher (macOS)
open "Nikon Expert.command"

# Option B: command line
conda activate nikon_expert
python ui/app.py
```

A native window opens automatically (no browser address bar), or visit http://localhost:7860

---

## Data Ingestion

### Full-text index (recommended first)
On the "Knowledge Base" page click "Select folder" and point to your Obsidian vault or notes directory.
- Indexes raw files directly, no vectorization, completes in seconds
- Supports `.md`, `.txt`, `.pdf`
- Ideal for exact error-code lookup and keyword search

### Vectorized ingest (RAG)
Upload PDF manuals or Markdown files; the system automatically:
- Chunks by section structure → embeds → stores in the vector DB
- Writes to the full-text index at the same time
- Retrievable immediately after ingest

### Command-line ingest
```bash
# Ingest an Obsidian vault
bash ingest.sh /path/to/your/vault

# Ask from the command line
bash query.sh "How do I troubleshoot the E-5301 alarm?"
```

---

## Project Structure

```
nikon-expert/
├── src/
│   ├── engine.py      # core engine (router + hybrid retrieval + LLM generation)
│   ├── fulltext.py    # full-text layer (SQLite FTS5)
│   ├── router.py      # smart router (query classification + dual-recall fusion)
│   ├── ingestor.py    # ingesters (structure-aware chunking + dedup + FTS sync)
│   └── prompts.py     # prompt templates
├── ui/
│   └── app.py         # native window UI (Gradio + pywebview)
├── scripts/           # command-line tools
├── models/            # embedding models (BGE-M3 + reranker)
├── data/
│   ├── qdrant_db/     # vector database
│   └── fts.db         # full-text search database
├── build.sh           # one-click distributable packaging
├── setup.sh           # environment initialization
├── .env               # configuration
└── requirements.txt   # Python dependencies
```

---

## Configuration (.env)

| Key | Description | Default |
|--------|------|--------|
| `LLM_MODEL` | Ollama model name | `qwen2.5:7b-instruct-q8_0` |
| `LLM_BASE_URL` | Ollama address | `http://localhost:11434` |
| `EMBED_MODEL_PATH` | BGE-M3 model path | `./models/bge-m3` |
| `QDRANT_PATH` | vector DB path (local file mode) | `./data/qdrant_db` |
| `QDRANT_URL` | Qdrant server address (set = server mode, multi-process concurrency) | empty (= file mode) |
| `FTS_DB_PATH` | full-text DB path | `./data/fts.db` |
| `LLM_PROVIDER` | cloud brain provider (claude/deepseek/…, empty = local Ollama) | empty |
| `MCP_HOST` / `MCP_PORT` | MCP streamable-http bind address | `127.0.0.1` / `8000` |
| `X_KWIKI_AUTH` | WPS kwiki token (put in `.kwiki_env`, never commit) | — |
| `KWIKI_KB_KUID` | WPS knowledge base kuid to sync (starts with `0s`) | — |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | Feishu custom-app credentials (put in `.feishu_env`, never commit) | — |
| `FEISHU_WORKLOG_URL` | Feishu worklog doc link (wiki/docx) or document_id | — |
| `FEISHU_SCOPES` | OAuth scopes (reading docs needs `docx`/`drive`/`wiki` readonly; add `offline_access` for auto-renewal) | see `.feishu_env` |
| `PARENT_CHUNK_SIZE` | PDF parent-chunk size | `1024` |
| `CHILD_CHUNK_SIZE` | PDF child-chunk size | `256` |
| `CONFIDENCE_THRESHOLD` | retrieval confidence threshold | `0.30` |
| `RETRIEVAL_TOP_K` | initial retrieval count | `8` |
| `RERANK_TOP_N` | reranked keep count | `4` |

---

## Distributing to Colleagues

```bash
bash build.sh
# produces dist/Nikon-Expert-<date>.tar.gz with models + launcher + install guide
```

A colleague unpacks it, runs `bash setup.sh`, and double-clicks `Nikon Expert.command`.

---

## FAQ

**Q: Model download is slow?**
A: Configure a mirror: `export HF_ENDPOINT=https://hf-mirror.com`, then `python scripts/download_models.py`

**Q: Query returns nothing?**
A: First build the full-text index or upload documents on the Knowledge Base page. If you already have data but still get nothing, lower `CONFIDENCE_THRESHOLD` in `.env` to `0.20`

**Q: Slow responses?**
A: Use a smaller model: set `LLM_MODEL` in `.env` to `qwen2.5:7b-instruct-q8_0`

**Q: How to update the knowledge base?**
A: Re-run the full-text index or upload new files; data is appended automatically

**Q: Want to use a GPU server?**
A: Install Ollama/vLLM on the server and point `LLM_BASE_URL` in `.env` to it
