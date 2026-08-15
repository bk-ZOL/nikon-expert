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

    # 启动默认 LLM：LLM_PROVIDER 指定云端 provider（如 deepseek）时用它，否则本地 Ollama。
    # 云端部署（无本地 Ollama）靠这个默认到可用的 API。
    _provider = os.getenv("LLM_PROVIDER", "").strip()
    if _provider and _provider not in ("ollama_local", "ollama_lan"):
        from src.providers import build_llm
        print(f"⚙️  连接 LLM（provider）：{_provider} / {llm_model}")
        Settings.llm = build_llm(_provider, llm_model)
    else:
        print(f"⚙️  连接 LLM：{llm_model} @ {llm_url}")
        Settings.llm = Ollama(
            model=llm_model,
            base_url=llm_url,
            request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
        )

    client = QdrantClient(url=os.getenv("QDRANT_URL")) if os.getenv("QDRANT_URL") else QdrantClient(path=qdrant_path)
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
        "index": index,           # 供按请求构建带 ACL filter 的 retriever
        "top_k": top_k,
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


# 记录当前 provider/model，供 UI 显示
_current_provider = os.getenv("LLM_PROVIDER", "ollama_local")


def get_current_provider() -> str:
    return _current_provider


def switch_provider(provider_id: str, model_name: str) -> str:
    """切换外接大脑：设置 Settings.llm 为指定 provider 的模型。
    RAG 与 Agent 两条路径都用 Settings.llm，故一次切换全局生效。
    返回给 UI 的状态文案；缺 key 等错误以 ValueError 抛出由调用方兜底。"""
    global _current_provider
    from llama_index.core import Settings
    from src.providers import build_llm, get_provider

    llm = build_llm(provider_id, model_name)   # 缺 key 会在此抛 ValueError
    Settings.llm = llm
    _current_provider = provider_id
    label = get_provider(provider_id)["label"]
    print(f"✅ LLM 已切换至：{label} / {model_name}")
    return f"{label} · {model_name}"


def ingest_okf(path: str) -> dict:
    """导入 OKF（Open Knowledge Format）目录或单个 .md，复用当前引擎的 Qdrant 连接。
    返回 {chunks, message}。"""
    from llama_index.core import StorageContext
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from src.ingestor import ingest_okf as _ingest_okf

    eng = _init_engine()
    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    n = _ingest_okf(path, storage=(vs, ctx, eng["client"]))
    return {"chunks": n, "message": f"✅ OKF 导入完成：{n} 个 Chunk"}


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
        result = _ingest_md_file(eng, path, rid, doc_name)
    elif suffix == ".pdf":
        result = _ingest_pdf_file(eng, path, rid, doc_name)
    else:
        return {"success": False, "message": f"不支持的格式：{suffix}，仅支持 PDF / MD"}

    # 摄入成功 → 自动定级 + 写 Qdrant ACL payload（新文档即时可查，或按密级正确隐身）
    if isinstance(result, dict) and (result.get("success") or result.get("chunks")):
        try:
            from src.acl_classify import apply_doc_acl
            apply_doc_acl(doc_name)
        except Exception:
            pass
    return result


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

def _retrieve_with_router(question: str, mode: str = "qa", user=None) -> list:
    """
    智能路由检索：分类查询 → FTS + 语义双路召回 → 融合排序
    返回: [{"doc_name", "doc_type", "text", "score", "source", ...}, ...]

    user: acl.User。security 开启时 FTS 与向量两路各自独立完成 ACL 预过滤
    （方案 4.5：两路都必须已过滤，否则融合排名失真且越权内容进上下文）。
    """
    from src.router import (classify_query, get_route_weights, merge_results,
                            extract_machine_models, extract_key_terms, is_circuit_query)
    from src.fulltext import search_fts, search_fts_exact
    from src import acl

    threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.30"))
    top_k = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    rerank_top_n = int(os.getenv("RERANK_TOP_N", "6"))
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
            fts_results = search_fts_exact(codes[0], limit=top_k, user=user)
        else:
            # 用主要关键词做 FTS 搜索
            fts_query = question.replace("？", "").replace("?", "").strip()
            if fts_query:
                fts_results = search_fts(fts_query, limit=top_k, user=user)
            # 实体优先：对显著领域名词（氦气/encoder…）各做一次定向 FTS，
            # 保证这类"细节名词"不被整句里的高频词淹没（追加进 FTS 候选池）。
            seen_terms = set()
            for term in extract_key_terms(question)[:4]:
                if term in seen_terms:
                    continue
                seen_terms.add(term)
                fts_results += search_fts(term, limit=5, user=user)
    except acl.PermissionDenied:
        raise
    except Exception:
        pass

    # ── 3. 语义检索 (Gbrain 层)：按请求注入 ACL filter，失败则 fail-closed
    semantic_results = []
    try:
        nodes = _retrieve_semantic(eng, question, user, top_k)
        semantic_results = [n for n in nodes if (n.score or 0.0) >= threshold]
    except acl.PermissionDenied:
        raise
    except Exception:
        pass

    # ── 4. 融合(RRF)：先取较大候选池，再交给 Rerank 精排
    rerank_pool = int(os.getenv("RERANK_POOL", "12"))
    pool = merge_results(fts_results, semantic_results, fts_w, sem_w, top_n=rerank_pool,
                         demote_circuit=not is_circuit_query(question))

    # ── 5. Rerank 精排：把语义蹭词但不相关的挤掉，只留最相关的 rerank_top_n 条
    merged = _rerank(question, pool, rerank_top_n)
    return merged, qtype


def _rerank(question: str, candidates: list, top_n: int) -> list:
    """第三层 Rerank：对 RRF 候选池按"与问题的真实相关性"精排，取 top_n。

    默认用 LLM(当前大脑)做精排——零额外模型/内存，天然跨语言(中/英/日)。
    RERANK_ENABLED=false 关闭；失败/不可用则回退 RRF 原序。
    （若日后要更快的本地 cross-encoder，可换 bge-reranker，但要占 ~2G 内存。）
    """
    import os as _os
    if not candidates:
        return candidates
    if (_os.getenv("RERANK_ENABLED", "true").lower() in ("0", "false", "no")
            or len(candidates) <= top_n):
        return candidates[:top_n]
    try:
        from llama_index.core import Settings
        if Settings.llm is None:
            return candidates[:top_n]
        listing = "\n".join(
            f"[{i}] 《{c.get('doc_name', '')}》{c.get('section_title', '') or ''}："
            f"{(c.get('text', '') or '')[:180]}"
            for i, c in enumerate(candidates))
        prompt = (f"下面是检索到的资料片段。挑出对回答问题最相关的，按相关性从高到低排序，"
                  f"只输出编号（逗号分隔，最多 {top_n} 个），不要解释、不要输出别的。\n\n"
                  f"问题：{question}\n\n候选片段：\n{listing}\n\n最相关编号：")
        resp = str(Settings.llm.complete(prompt)).strip()
        import re as _re
        order, seen = [], set()
        for x in _re.findall(r"\d+", resp):
            i = int(x)
            if 0 <= i < len(candidates) and i not in seen:
                seen.add(i)
                order.append(candidates[i])
            if len(order) >= top_n:
                break
        # LLM 漏选的按 RRF 原序补齐
        for i, c in enumerate(candidates):
            if len(order) >= top_n:
                break
            if i not in seen:
                order.append(c)
        return order[:top_n] if order else candidates[:top_n]
    except Exception:
        return candidates[:top_n]


def _retrieve_semantic(eng: dict, question: str, user, top_k: int) -> list:
    """向量检索。security 开启时用带 ACL 的 qdrant Filter 现建 retriever 预过滤。

    security 关 → 用缓存的默认 retriever（行为不变）。
    security 开 → 注入 build_qdrant_filter；若注入机制异常，fail-closed 返回 []，
    绝不回退到未过滤检索（宁可查不到，不可越权带出他人资料）。
    """
    from src import acl
    if not acl.security_enabled():
        return eng["retriever"].retrieve(question)

    qfilter = acl.build_qdrant_filter(user)  # 无身份会抛 PermissionDenied
    if qfilter is None:  # 超级用户
        return eng["retriever"].retrieve(question)

    from llama_index.core.retrievers import VectorIndexRetriever
    retriever = VectorIndexRetriever(
        index=eng["index"],
        similarity_top_k=top_k,
        vector_store_kwargs={"qdrant_filters": qfilter},
    )
    return retriever.retrieve(question)


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

        # 适用机型：优先元数据，回退从文档名里推断（很多手册名含型号）
        mm = (r.get("machine_model") or "").strip()
        if not mm:
            import re as _re
            _hit = _re.findall(r"NSR-S\w+|NSR-SF\w+|S\d{3}[A-Z]", f"{doc_name}")
            mm = " / ".join(dict.fromkeys(_hit)) if _hit else "未标注"

        ctx_parts.append(
            f"[参考资料 {i}] 来源：{cite}\n适用机型：{mm}\n检索方式：{source_label}\n"
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


def _agent_query_stream(question: str, history: list = None):
    """把 ReAct agent 的事件流适配成 query_stream 的 5 元组契约。

    yield (delta, is_final, citations, citations_data, has_result)
    非 final：把「推理轨迹（工具调用/观察）+ 最终答案」作为 delta 累积显示；
    final：给出去重后的引用。
    """
    from src.agent import run_agent_sync

    yield "🔍 **多步排查中……**\n\n", False, [], [], True

    final = None
    for ev in run_agent_sync(question, history):
        kind = ev["kind"]
        if kind == "tool_call":
            args = "，".join(f"{k}={v}" for k, v in ev["args"].items())
            yield f"🔧 `{ev['tool']}`（{args}）\n", False, [], [], True
        elif kind == "observation":
            out = (ev["output"] or "").strip().replace("\n", " ")[:100]
            yield f"　↳ {out}…\n", False, [], [], True
        elif kind == "answer":
            final = ev
            yield "\n---\n\n" + ev["text"], False, [], [], True
        elif kind == "error":
            yield f"\n\n⚠️ Agent 执行出错：{ev['error']}", True, [], [], False
            return

    if final:
        yield "", True, final["citations"], final["citations_data"], final["has_result"]
    else:
        yield "", True, [], [], False


def query_stream(question: str, mode: str = "qa", history: list = None, user=None):
    """流式查询：智能路由决定走多步 Agent 还是单次 RAG 快路径。

    user: acl.User。绑定到 contextvar，使 agent 工具路径深处的检索也按 ACL 过滤。
    """
    from src.prompts import KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    from src.router import should_use_agent
    from llama_index.core import Settings
    from src import acl

    if user is None:                      # 上层（如 Gradio chat）可能已绑 contextvar
        user = acl.get_current_user()
    _tok = acl.set_current_user(user)
    try:
        # ── 智能路由：故障排查 / 复合问题 → 多步 Agent
        if should_use_agent(question, mode):
            yield from _agent_query_stream(question, history)
            return

        merged, qtype = _retrieve_with_router(question, mode, user=user)
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
    finally:
        acl.reset_current_user(_tok)


def query(question: str, mode: str = "qa", user=None) -> dict:
    """执行一次完整查询（非流式）"""
    from src.prompts import KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    from llama_index.core import Settings
    from src import acl

    _tok = acl.set_current_user(user)
    try:
        merged, qtype = _retrieve_with_router(question, mode, user=user)
    finally:
        acl.reset_current_user(_tok)
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
