# src/engine.py
# Nikon Expert — 核心引擎（FTS + 语义检索 + 路由融合 + LLM 问答）
# Karpathy (FTS/grep) + Gbrain (结构分块/元数据) + RAG (检索增强生成)

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_engine = None


def _init_engine():
    """初始化引擎：Embedding + LLM + Qdrant + FTS"""
    global _engine
    if _engine is not None:
        return _engine

    from llama_index.core import VectorStoreIndex, StorageContext, Settings
    from llama_index.core.retrievers import VectorIndexRetriever
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.llms.ollama import Ollama
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient
    from src.fulltext import init_fts
    from src.device import get_device

    embed_path    = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
    reranker_path = os.getenv("RERANKER_MODEL_PATH", "./models/bge-reranker-v2-m3")
    qdrant_path   = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    fts_path      = os.getenv("FTS_DB_PATH", "./data/fts.db")
    collection    = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    llm_model     = os.getenv("LLM_MODEL", "qwen2.5:14b-instruct-q6_K")
    llm_url       = os.getenv("LLM_BASE_URL", "http://localhost:11434")
    top_k         = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    rerank_top_n  = int(os.getenv("RERANK_TOP_N", "4"))

    print(f"⚙️  加载 Embedding 模型：{embed_path}")
    Settings.embed_model = HuggingFaceEmbedding(
        model_name=embed_path,
        max_length=512,
        device=get_device(),
    )

    print(f"⚙️  连接 LLM：{llm_model} @ {llm_url}")
    Settings.llm = Ollama(
        model=llm_model,
        base_url=llm_url,
        request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
    )

    client = QdrantClient(path=qdrant_path)
    vector_store = QdrantVectorStore(client=client, collection_name=collection)
    storage_ctx  = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_ctx
    )

    retriever = VectorIndexRetriever(index=index, similarity_top_k=top_k)

    reranker = None
    try:
        from llama_index.postprocessor.flag_embedding_reranker import FlagEmbeddingReranker
        reranker = FlagEmbeddingReranker(model=reranker_path, top_n=rerank_top_n)
        print("⚙️  重排序：FlagEmbeddingReranker")
    except Exception:
        try:
            from llama_index.core.postprocessor import SentenceTransformerRerank
            reranker = SentenceTransformerRerank(model=reranker_path, top_n=rerank_top_n)
            print("⚙️  重排序：SentenceTransformerRerank")
        except Exception as e:
            print(f"⚠️  重排序不可用，跳过：{e}")

    # 初始化 FTS 层 (Karpathy)
    init_fts(fts_path)
    fts_status = None
    try:
        from src.fulltext import fts_status as _fts_st
        fts_status = _fts_st()
    except Exception:
        pass
    print(f"⚙️  FTS 全文搜索：{fts_status}")

    _engine = {
        "retriever": retriever,
        "reranker": reranker,
        "client": client,
        "collection": collection,
    }
    print("✅ 引擎初始化完成（FTS + 语义双层）")
    return _engine


def get_current_model() -> str:
    from llama_index.core import Settings
    if Settings.llm is None:
        return os.getenv("LLM_MODEL", "unknown")
    return getattr(Settings.llm, "model", os.getenv("LLM_MODEL", "unknown"))


def switch_llm(model_name: str) -> None:
    from llama_index.core import Settings
    from llama_index.llms.ollama import Ollama
    Settings.llm = Ollama(
        model=model_name,
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434"),
        request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
    )
    print(f"✅ LLM 已切换至：{model_name}")


def ingest_file(file_path: str) -> dict:
    """摄入单个上传文件（PDF / MD），使用新分块策略"""
    import gc
    import hashlib
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.vector_stores.qdrant import QdrantVectorStore

    eng = _init_engine()
    path = Path(file_path)
    suffix = path.suffix.lower()
    rid = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    doc_name = path.name

    # 去重：先删除旧数据
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    eng["client"].delete(
        collection_name=eng["collection"],
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))])
        ),
    )
    try:
        from src.fulltext import delete_doc_fts_by_name
        delete_doc_fts_by_name(doc_name)
    except Exception:
        pass

    if suffix == ".md":
        return _ingest_md_file(eng, path, rid, doc_name)
    elif suffix == ".pdf":
        return _ingest_pdf_file(eng, path, rid, doc_name)
    else:
        return {"success": False, "message": f"不支持的格式：{suffix}，仅支持 PDF / MD"}


def _ingest_md_file(eng, path: Path, rid: str, doc_name: str) -> dict:
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
        try:
            from src.fulltext import ingest_fts
            ingest_fts(rid, doc_name, "obsidian_note", "", sec_title, sec_text, str(path))
        except Exception:
            pass

    if not docs:
        return {"success": False, "message": "未能提取到有效内容"}

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs, show_progress=False)

    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
    return {"success": True, "message": f"✅ 摄入完成：**{doc_name}**（{len(nodes)} chunks）", "chunks": len(nodes)}


def _ingest_pdf_file(eng, path: Path, rid: str, doc_name: str) -> dict:
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
        try:
            from src.fulltext import ingest_fts
            ingest_fts(rid, doc_name, "manual", "", sec_title, sec_text)
        except Exception:
            pass

    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    VectorStoreIndex(all_nodes, storage_context=ctx, show_progress=False)
    return {"success": True, "message": f"✅ 摄入完成：**{doc_name}**（{len(all_nodes)} chunks）", "chunks": len(all_nodes)}


def get_kb_documents() -> list:
    from collections import Counter
    eng = _init_engine()
    doc_counts, doc_meta = Counter(), {}
    offset = None
    while True:
        try:
            points, next_offset = eng["client"].scroll(
                collection_name=eng["collection"],
                limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
        except (ValueError, Exception):
            return []
        if not points:
            break
        for p in points:
            name = p.payload.get("doc_name", "未知")
            doc_counts[name] += 1
            if name not in doc_meta:
                fp = p.payload.get("file_path", "")
                mm = p.payload.get("machine_model", "")
                dt = p.payload.get("doc_type", "")
                doc_meta[name] = {"doc_type": dt, "location": fp or mm or "—"}
        offset = next_offset
        if next_offset is None:
            break
    return [
        {"文档名": n, "类型": doc_meta[n]["doc_type"], "位置": doc_meta[n]["location"], "Chunks": c}
        for n, c in sorted(doc_counts.items())
    ]


def delete_document(doc_name: str) -> str:
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    eng = _init_engine()
    eng["client"].delete(
        collection_name=eng["collection"],
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))])
        ),
    )
    try:
        from src.fulltext import delete_doc_fts_by_name
        delete_doc_fts_by_name(doc_name)
    except Exception:
        pass
    return f"✅ 已删除：**{doc_name}**"


def engine_db_status() -> dict:
    if _engine is None:
        return {"collection": "—", "points": 0, "status": "engine not initialized"}
    try:
        info = _engine["client"].get_collection(_engine["collection"])
        qdrant_pts = info.points_count
    except (ValueError, Exception):
        qdrant_pts = 0

    fts_info = {"records": 0, "documents": 0}
    try:
        from src.fulltext import fts_status
        fts_info = fts_status()
    except Exception:
        pass

    return {
        "collection": _engine.get("collection", "—"),
        "points": qdrant_pts,
        "fts_records": fts_info.get("records", 0),
        "fts_documents": fts_info.get("documents", 0),
        "status": "ok",
    }


# ── 路由融合检索（核心）──────────────────────────────────────

def _retrieve_with_router(question: str, mode: str = "qa") -> list:
    """
    智能路由检索：分类查询 → FTS + 语义双路召回 → 融合排序
    返回: [{"doc_name", "doc_type", "text", "score", "source", ...}, ...]
    """
    from src.router import classify_query, get_route_weights, merge_results, extract_machine_models
    from src.fulltext import search_fts, search_fts_exact

    threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.30"))
    top_k = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    rerank_top_n = int(os.getenv("RERANK_TOP_N", "4"))
    eng = _init_engine()

    # ── 1. 查询分类
    qtype = classify_query(question)
    fts_w, sem_w = get_route_weights(qtype)

    # ── 2. FTS 检索 (Karpathy 层)
    fts_results = []
    try:
        fts_query = question
        # 精确匹配 Error Code
        from src.router import extract_error_codes
        codes = extract_error_codes(question)
        if codes:
            fts_results = search_fts_exact(codes[0], limit=top_k)
        else:
            # 用主要关键词做 FTS 搜索
            fts_query = question.replace("？", "").replace("?", "").strip()
            if fts_query:
                fts_results = search_fts(fts_query, limit=top_k)
    except Exception:
        pass

    # ── 3. 语义检索 (Gbrain 层)
    semantic_results = []
    try:
        nodes = eng["retriever"].retrieve(question)
        semantic_results = [n for n in nodes if (n.score or 0.0) >= threshold]
    except (ValueError, Exception):
        pass

    # ── 4. 融合
    merged = merge_results(fts_results, semantic_results, fts_w, sem_w, top_n=top_k)

    return merged, qtype


def _build_context(merged_results: list, mode: str = "qa") -> tuple:
    """从融合结果构建 LLM context、显示用 citations 和结构化 citations_data"""
    from src.prompts import NO_RESULT_RESPONSE

    if not merged_results:
        return "", [], [], False

    ctx_parts, citations, citations_data = [], [], []
    for i, r in enumerate(merged_results, 1):
        doc_name = r.get("doc_name", "未知文档")
        doc_type = r.get("doc_type", "")
        page     = r.get("page_start", "")
        date     = r.get("fault_date", "")
        section  = r.get("section_title", "")
        score    = r.get("score", 0)
        source   = r.get("source", "semantic")
        text     = r.get("text", "")
        file_path = r.get("file_path", "")

        # 引用格式
        if doc_type == "manual" and page:
            cite = f"《{doc_name}》第 {page} 页"
        elif doc_type == "manual" and section:
            cite = f"《{doc_name}》— {section}"
        elif doc_type == "fault_history" and date:
            codes = r.get("error_codes", [])
            code_str = f"[{', '.join(codes)}] " if codes else ""
            cite = f"故障履历 {code_str}{date} — {doc_name}"
        elif doc_type == "checksheet":
            row = r.get("row_index", "")
            cite = f"《{doc_name}》第 {row} 行"
        else:
            fp = r.get("file_path", "")
            cite = f"{doc_name}" + (f"（{Path(fp).parent.name}）" if fp else "")

        # 来源标注
        source_label = {"fts": "关键词", "semantic": "语义", "fts+semantic": "双引擎"}.get(source, source)

        ctx_parts.append(
            f"[参考资料 {i}] 来源：{cite}\n检索方式：{source_label}\n"
            f"内容：\n{text}\n" + "─" * 50
        )
        citations.append(f"[{i}] {cite}  ({source_label})")

        # 结构化数据供 UI 构建可点击引用按钮
        citations_data.append({
            "index": i,
            "doc_name": doc_name,
            "doc_type": doc_type,
            "file_path": file_path,
            "page": int(page) if page else None,
            "section": section,
            "source": source_label,
            "cite_text": cite,
        })

    context = "\n\n".join(ctx_parts)
    return context, citations, citations_data, True


def query_stream(question: str, mode: str = "qa", history: list = None):
    """流式查询，使用路由融合检索"""
    from src.prompts import KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    from llama_index.core import Settings

    merged, qtype = _retrieve_with_router(question, mode)
    context, citations, citations_data, has_result = _build_context(merged, mode)

    if not has_result:
        yield NO_RESULT_RESPONSE, True, [], [], False
        return

    # 多轮对话历史
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

    for token in Settings.llm.stream_complete(final_prompt):
        yield token.delta, False, [], [], True

    yield "", True, citations, citations_data, True


def query(question: str, mode: str = "qa") -> dict:
    """执行一次完整查询（非流式）"""
    from src.prompts import KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    from llama_index.core import Settings

    merged, qtype = _retrieve_with_router(question, mode)
    context, citations, citations_data, has_result = _build_context(merged, mode)

    if not has_result:
        return {"answer": NO_RESULT_RESPONSE, "citations": [], "citations_data": [], "retrieved": 0, "has_result": False}

    prompt_tmpl = TROUBLESHOOTING_PROMPT if mode == "troubleshoot" else KNOWLEDGE_QA_PROMPT
    final_prompt = prompt_tmpl.format(context=context, query=question)
    response = Settings.llm.complete(final_prompt)

    return {
        "answer": str(response),
        "citations": citations,
        "citations_data": citations_data,
        "retrieved": len(merged),
        "has_result": True,
        "query_type": qtype,
    }
