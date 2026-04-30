"""Nikon Expert — FastAPI 后端服务
提供 REST/SSE API，支持多团队知识库隔离。
启动: uvicorn api.server:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import requests as http_req
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, PlainTextResponse, HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel

from src.device import get_device
from src.engine_factory import ensure_shared_resources
from src.tenant import get_engine, list_team_collections, remove_team
from api.models import QueryRequest, IngestResponse, DocumentItem, StatusResponse


# ── 生命周期 ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⚙️  正在加载共享模型...")
    ensure_shared_resources()
    print("✅ API 服务就绪")
    yield


app = FastAPI(title="Nikon Expert API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 团队识别中间件 ─────────────────────────────────────────────
@app.middleware("http")
async def team_middleware(request, call_next):
    team_id = request.headers.get("X-Team-ID", "default")
    if not re.match(r'^[a-zA-Z0-9_]+$', team_id):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": "Invalid team_id"})
    request.state.team_id = team_id
    response = await call_next(request)
    response.headers["X-Team-ID"] = team_id
    return response


# ── 健康检查 ─────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "device": get_device()}


# ── 查询（SSE 流式） ───────────────────────────────────────────
@app.post("/api/query")
async def api_query(request: QueryRequest):
    """SSE 流式查询"""
    from src.router import auto_mode, classify_query, get_route_weights, merge_results, extract_error_codes
    from src.engine import _build_context
    from src.prompts import KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    from llama_index.core import Settings

    eng = get_engine(request.team_id)
    fts_conn = eng.get("fts_conn")
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    mode = auto_mode(question)
    history = request.history or []

    def event_generator():
        try:
            # ── 检索（使用团队的 retriever 和 FTS）──
            threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.30"))
            top_k = int(os.getenv("RETRIEVAL_TOP_K", "8"))

            qtype = classify_query(question)
            fts_w, sem_w = get_route_weights(qtype)

            # FTS 检索（使用团队专属连接）
            fts_results = []
            if fts_conn:
                try:
                    prefix = "" if request.team_id == "default" else f"{request.team_id}_"
                    codes = extract_error_codes(question)
                    if codes:
                        fts_results = _search_fts_with_conn(fts_conn, prefix, codes[0], limit=top_k, exact=True)
                    else:
                        fts_query = question.replace("？", "").replace("?", "").strip()
                        if fts_query:
                            fts_results = _search_fts_with_conn(fts_conn, prefix, fts_query, limit=top_k)
                except Exception:
                    pass

            # 语义检索
            semantic_results = []
            try:
                nodes = eng["retriever"].retrieve(question)
                semantic_results = [n for n in nodes if (n.score or 0.0) >= threshold]
            except Exception:
                pass

            merged = merge_results(fts_results, semantic_results, fts_w, sem_w, top_n=top_k)
            context, citations, citations_data, has_result = _build_context(merged, mode)

            if not has_result:
                data = json.dumps({"delta": NO_RESULT_RESPONSE, "is_final": True, "has_result": False}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                return

            # 对话历史
            history_prefix = ""
            if history:
                recent = history[-6:]
                turns = []
                for msg in recent:
                    role = "工程师" if msg["role"] == "user" else "助手"
                    turns.append(f"{role}：{msg['content'][:400]}")
                history_prefix = "【对话历史（最近几轮）】\n" + "\n".join(turns) + "\n\n"

            prompt_tmpl = TROUBLESHOOTING_PROMPT if mode == "troubleshoot" else KNOWLEDGE_QA_PROMPT
            final_prompt = prompt_tmpl.format(context=context, query=history_prefix + question)

            # 流式输出
            for token in Settings.llm.stream_complete(final_prompt):
                data = json.dumps({
                    "delta": token.delta, "is_final": False, "has_result": True,
                }, ensure_ascii=False)
                yield f"data: {data}\n\n"

            # 最终：带 citations
            data = json.dumps({
                "delta": "", "is_final": True, "has_result": True,
                "citations": citations, "citations_data": citations_data,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        except Exception as e:
            data = json.dumps({"delta": f"服务错误：{e}", "is_final": True, "has_result": False}, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _search_fts_with_conn(conn, table_prefix, query, limit=8, exact=False):
    """使用特定团队连接执行 FTS 搜索"""
    fts_table = f"[{table_prefix}doc_fts]"
    meta_table = f"[{table_prefix}doc_meta]"

    if exact:
        quoted = f'"{query}"'
        where = f"{fts_table} MATCH ?"
        params = [quoted]
    else:
        where = f"{fts_table} MATCH ?"
        params = [query]

    sql = f"""
        SELECT {fts_table}.doc_id, {fts_table}.doc_name, {fts_table}.doc_type,
               COALESCE({meta_table}.machine_model, '') as machine_model,
               {fts_table}.section_title, {fts_table}.text, rank as score,
               COALESCE({meta_table}.file_path, '') as file_path
        FROM {fts_table}
        LEFT JOIN {meta_table} ON {fts_table}.doc_id = {meta_table}.doc_id
        WHERE {where}
        ORDER BY score
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        results.append({
            "doc_id": row[0], "doc_name": row[1], "doc_type": row[2],
            "machine_model": row[3], "section_title": row[4],
            "text": row[5], "score": -row[6], "file_path": row[7],
        })
    return results


# ── 文件摄入 ─────────────────────────────────────────────────────
@app.post("/api/ingest", response_model=IngestResponse)
async def api_ingest(file: UploadFile = File(...), team_id: str = Form("default")):
    """上传文件并向量化"""
    from src.engine import _ingest_md_file, _ingest_pdf_file

    eng = get_engine(team_id)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".md", ".pdf"):
        return IngestResponse(success=False, message=f"不支持的格式：{suffix}")

    # 保存到临时文件
    tmp_path = Path(tempfile.mkdtemp()) / file.filename
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        # 需要注入团队的 engine 和 fts_conn
        from src.engine import _init_engine
        import hashlib
        from src.ingestor import _split_by_headings, _chunk_hash
        from llama_index.core import VectorStoreIndex, Document, StorageContext
        from llama_index.vector_stores.qdrant import QdrantVectorStore

        rid = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
        doc_name = file.filename

        # 删除旧数据
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        collection = eng["collection"]
        eng["client"].delete(
            collection_name=collection,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))])
            ),
        )

        if suffix == ".md":
            result = _ingest_md_file(eng, tmp_path, rid, doc_name, team_id)
        else:
            result = _ingest_pdf_file(eng, tmp_path, rid, doc_name, team_id)

        return IngestResponse(
            success=result.get("success", False),
            message=result.get("message", ""),
            chunks=result.get("chunks"),
        )
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


# ── 文档列表 ─────────────────────────────────────────────────────
@app.get("/api/documents", response_model=list[DocumentItem])
async def api_documents(team_id: str = "default"):
    from collections import Counter
    eng = get_engine(team_id)
    doc_counts, doc_meta = Counter(), {}
    offset = None
    while True:
        try:
            points, next_offset = eng["client"].scroll(
                collection_name=eng["collection"],
                limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
        except Exception:
            break
        if not points:
            break
        for p in points:
            name = p.payload.get("doc_name", "未知")
            doc_counts[name] += 1
            if name not in doc_meta:
                fp = p.payload.get("file_path", "")
                dt = p.payload.get("doc_type", "")
                doc_meta[name] = {"doc_type": dt, "location": fp or "—"}
        offset = next_offset
        if next_offset is None:
            break
    return [
        DocumentItem(doc_name=n, doc_type=doc_meta[n]["doc_type"],
                     location=doc_meta[n]["location"], chunks=c)
        for n, c in sorted(doc_counts.items())
    ]


# ── 删除文档 ─────────────────────────────────────────────────────
@app.delete("/api/documents/{doc_name}")
async def api_delete_document(doc_name: str, team_id: str = "default"):
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    eng = get_engine(team_id)
    eng["client"].delete(
        collection_name=eng["collection"],
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))])
        ),
    )
    return {"message": f"已删除：{doc_name}"}


# ── FTS 索引 ─────────────────────────────────────────────────────
@app.post("/api/fts/index")
async def api_fts_index(dir_path: str, pattern: str = "*.md", team_id: str = "default"):
    from src.fulltext import _split_sections, _extract_pdf_text
    eng = get_engine(team_id)
    fts_conn = eng.get("fts_conn")
    if not fts_conn:
        raise HTTPException(500, "FTS 未初始化")

    dir_path = dir_path.strip()
    if not os.path.isdir(dir_path):
        raise HTTPException(400, f"目录不存在：{dir_path}")

    indexed, records, errors = 0, 0, []
    import hashlib
    prefix = "" if team_id == "default" else f"{team_id}_"

    for root, dirs, files in os.walk(dir_path):
        for fname in files:
            if not _match_pattern(fname, pattern):
                continue
            fpath = os.path.join(root, fname)
            try:
                text = Path(fpath).read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not text:
                continue

            rid = hashlib.sha256(fpath.encode()).hexdigest()[:12]
            sections = _split_sections(text)
            for title, content in sections:
                if not content.strip():
                    continue
                try:
                    fts_conn.execute(
                        f"INSERT OR REPLACE INTO [{prefix}doc_fts] VALUES (?,?,?,?,?,?)",
                        (rid, fname, "grep_source", "", title, content),
                    )
                    records += 1
                except Exception as e:
                    errors.append(str(e))
            fts_conn.execute(
                f"INSERT OR REPLACE INTO [{prefix}doc_meta] VALUES (?,?,?,?,?,datetime('now'))",
                (rid, fname, "grep_source", fpath, ""),
            )
            fts_conn.commit()
            indexed += 1

    return {"indexed": indexed, "records": records, "errors": errors}


def _match_pattern(filename: str, pattern: str) -> bool:
    import fnmatch
    patterns = [p.strip() for p in pattern.split(";")]
    return any(fnmatch.fnmatch(filename.lower(), p.lower()) for p in patterns)


@app.post("/api/fts/clear")
async def api_fts_clear(team_id: str = "default"):
    eng = get_engine(team_id)
    fts_conn = eng.get("fts_conn")
    if not fts_conn:
        return {"cleared": 0}
    prefix = "" if team_id == "default" else f"{team_id}_"
    try:
        count = fts_conn.execute(f"SELECT count() FROM [{prefix}doc_fts]").fetchone()[0]
        fts_conn.execute(f"DELETE FROM [{prefix}doc_fts]")
        fts_conn.execute(f"DELETE FROM [{prefix}doc_meta]")
        fts_conn.commit()
        return {"cleared": count}
    except Exception:
        return {"cleared": 0}


# ── 团队管理 ─────────────────────────────────────────────────────
@app.get("/api/teams")
async def api_list_teams():
    return {"teams": list_team_collections()}


@app.post("/api/teams")
async def api_create_team(team_id: str, description: str = ""):
    if not re.match(r'^[a-zA-Z0-9_]+$', team_id):
        raise HTTPException(400, "team_id 只允许字母、数字和下划线")
    # 创建 engine 即自动创建 collection + FTS tables
    get_engine(team_id)
    return {"message": f"团队 '{team_id}' 已创建"}


# ── 知识库状态 ───────────────────────────────────────────────────
@app.get("/api/status", response_model=StatusResponse)
async def api_status(team_id: str = "default"):
    eng = get_engine(team_id)
    try:
        info = eng["client"].get_collection(eng["collection"])
        points = info.points_count
    except Exception:
        points = 0

    fts_conn = eng.get("fts_conn")
    fts_records = 0
    if fts_conn:
        try:
            prefix = "" if team_id == "default" else f"{team_id}_"
            fts_records = fts_conn.execute(f"SELECT count() FROM [{prefix}doc_fts]").fetchone()[0]
        except Exception:
            pass

    return StatusResponse(
        collection=eng["collection"],
        points=points,
        fts_records=fts_records,
        status="ok",
        device=get_device(),
    )


# ── LLM 模型管理 ───────────────────────────────────────────────
@app.get("/api/models")
async def api_list_models():
    url = os.getenv("LLM_BASE_URL", "http://localhost:11434")
    try:
        r = http_req.get(f"{url}/api/tags", timeout=3)
        return {"models": [m["name"] for m in r.json().get("models", [])]}
    except Exception:
        return {"models": [], "error": "无法连接 Ollama"}


@app.post("/api/models/switch")
async def api_switch_model(model_name: str):
    from llama_index.core import Settings
    from llama_index.llms.ollama import Ollama
    Settings.llm = Ollama(
        model=model_name,
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434"),
        request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
    )
    return {"message": f"已切换至 {model_name}"}


# ── PDF 文件服务（供 UI 引用跳转使用） ──────────────────────────
@app.get("/serve-pdf")
async def serve_pdf(path: str):
    try:
        resolved = Path(path).resolve()
    except Exception:
        return PlainTextResponse("Invalid path", status_code=400)
    if not resolved.exists():
        return PlainTextResponse("File not found", status_code=404)
    if not resolved.is_file():
        return PlainTextResponse("Not a file", status_code=400)
    if resolved.suffix.lower() != ".pdf":
        return PlainTextResponse("Only PDF files allowed", status_code=400)
    return FileResponse(str(resolved), media_type="application/pdf")


# 读取 PDF.js 渲染器模板
_RENDERER_HTML = ""
try:
    _renderer_path = Path(__file__).resolve().parent.parent / "ui" / "pdf_renderer_page.html"
    _RENDERER_HTML = _renderer_path.read_text(encoding="utf-8")
except Exception:
    pass

@app.get("/pdf-viewer-renderer")
async def pdf_viewer_renderer(file: str = "", page: int = 1):
    if not _RENDERER_HTML:
        return PlainTextResponse("Renderer template not found", status_code=500)
    html = _RENDERER_HTML.replace(
        "const filePath = urlParams.get('file');",
        f"const filePath = urlParams.get('file') || {json.dumps(file)};",
    )
    html = html.replace(
        "let startPage = parseInt(urlParams.get('page')) || 1;",
        f"let startPage = parseInt(urlParams.get('page')) || {page};",
    )
    return HTMLResponse(html)


# ── _ingest_md_file / _ingest_pdf_file 团队版本 ─────────────────
# 这里定义使用团队 engine 和 fts_conn 的版本

def _ingest_md_file(eng, path: Path, rid: str, doc_name: str, team_id: str) -> dict:
    import hashlib
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from src.ingestor import _split_by_headings, _chunk_hash

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {"success": False, "message": "文件为空"}

    sections = _split_by_headings(text, max_chunk=1024)
    docs = []
    fts_conn = eng.get("fts_conn")
    prefix = "" if team_id == "default" else f"{team_id}_"

    for sec_title, sec_text in sections:
        if not sec_text.strip():
            continue
        docs.append(Document(
            text=sec_text,
            metadata={
                "doc_name": doc_name, "doc_type": "obsidian_note",
                "language": "zh", "file_path": str(path),
                "doc_id": rid, "chunk_hash": _chunk_hash(sec_text),
                "section_title": sec_title,
            },
        ))
        if fts_conn:
            try:
                fts_conn.execute(
                    f"INSERT OR REPLACE INTO [{prefix}doc_fts] VALUES (?,?,?,?,?,?)",
                    (rid, doc_name, "obsidian_note", "", sec_title, sec_text),
                )
                fts_conn.execute(
                    f"INSERT OR REPLACE INTO [{prefix}doc_meta] VALUES (?,?,?,?,?,datetime('now'))",
                    (rid, doc_name, "obsidian_note", str(path), ""),
                )
                fts_conn.commit()
            except Exception:
                pass

    if not docs:
        return {"success": False, "message": "未能提取到有效内容"}

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs, show_progress=False)

    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
    return {"success": True, "message": f"摄入完成：{doc_name}（{len(nodes)} chunks）", "chunks": len(nodes)}


def _ingest_pdf_file(eng, path: Path, rid: str, doc_name: str, team_id: str) -> dict:
    import hashlib
    import pymupdf4llm
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from src.ingestor import _split_by_headings, _chunk_hash

    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    full_text = ""
    page_map = {}
    for chunk in pages:
        text = chunk.get("text", "").strip()
        if not text:
            continue
        page = chunk.get("metadata", {}).get("page", 0) + 1
        page_map[page] = len(full_text)
        full_text += text + "\n\n"

    if not full_text.strip():
        return {"success": False, "message": "未能提取到文本，可能是纯图片 PDF"}

    def _guess_page(char_pos: int) -> int:
        best = 1
        for pg, pos in sorted(page_map.items()):
            if pos <= char_pos:
                best = pg
        return best

    sections = _split_by_headings(full_text, max_chunk=1024)
    child_splitter = SentenceSplitter(chunk_size=256, chunk_overlap=32)
    all_nodes = []
    fts_conn = eng.get("fts_conn")
    prefix = "" if team_id == "default" else f"{team_id}_"

    for sec_title, sec_text in sections:
        if len(sec_text.strip()) < 20:
            continue
        page = _guess_page(full_text.find(sec_text[:50]))
        pid = _chunk_hash(sec_text)
        parent = Document(
            text=sec_text,
            metadata={
                "doc_name": doc_name, "doc_type": "manual",
                "language": "en", "page_start": page,
                "machine_model": "", "chunk_type": "parent",
                "doc_id": rid, "chunk_hash": pid,
                "section_title": sec_title,
            },
        )
        all_nodes.append(parent)
        if len(sec_text) > 256:
            children = child_splitter.get_nodes_from_documents([parent], show_progress=False)
            for c in children:
                c.metadata["chunk_type"] = "child"
                c.metadata["parent_hash"] = pid
            all_nodes.extend(children)
        if fts_conn:
            try:
                fts_conn.execute(
                    f"INSERT OR REPLACE INTO [{prefix}doc_fts] VALUES (?,?,?,?,?,?)",
                    (rid, doc_name, "manual", "", sec_title, sec_text),
                )
                fts_conn.execute(
                    f"INSERT OR REPLACE INTO [{prefix}doc_meta] VALUES (?,?,?,?,?,datetime('now'))",
                    (rid, doc_name, "manual", str(path), ""),
                )
                fts_conn.commit()
            except Exception:
                pass

    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    VectorStoreIndex(all_nodes, storage_context=ctx, show_progress=False)
    return {"success": True, "message": f"摄入完成：{doc_name}（{len(all_nodes)} chunks）", "chunks": len(all_nodes)}
