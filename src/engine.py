# src/engine.py
# Nikon Expert — 核心引擎（检索 + 重排序 + LLM 问答）
# 单文件封装，供 scripts/ 和 ui/ 直接调用

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ── 延迟导入，避免启动时间过长 ──────────────────────────────
_engine = None  # 全局单例


def _init_engine():
    """初始化引擎（首次调用时执行，后续复用）"""
    global _engine
    if _engine is not None:
        return _engine

    from llama_index.core import VectorStoreIndex, StorageContext, Settings
    from llama_index.core.retrievers import VectorIndexRetriever
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.llms.ollama import Ollama
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient

    embed_path    = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
    reranker_path = os.getenv("RERANKER_MODEL_PATH", "./models/bge-reranker-v2-m3")
    qdrant_path   = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    collection    = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    llm_model     = os.getenv("LLM_MODEL", "qwen2.5:14b-instruct-q6_K")
    llm_url       = os.getenv("LLM_BASE_URL", "http://localhost:11434")
    top_k         = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    rerank_top_n  = int(os.getenv("RERANK_TOP_N", "4"))

    print(f"⚙️  加载 Embedding 模型：{embed_path}")
    Settings.embed_model = HuggingFaceEmbedding(
        model_name=embed_path,
        max_length=512,
        device="mps",
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

    # 重排序：优先用 FlagEmbedding，否则降级用 SentenceTransformer，失败则跳过
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

    _engine = {"retriever": retriever, "reranker": reranker, "client": client, "collection": collection}
    print("✅ 引擎初始化完成")
    return _engine


def get_current_model() -> str:
    """返回当前激活的 LLM 模型名。"""
    from llama_index.core import Settings
    if Settings.llm is None:
        return os.getenv("LLM_MODEL", "unknown")
    return getattr(Settings.llm, "model", os.getenv("LLM_MODEL", "unknown"))


def switch_llm(model_name: str) -> None:
    """热替换 LLM，不重新加载 Embedding / Retriever / Qdrant。"""
    from llama_index.core import Settings
    from llama_index.llms.ollama import Ollama
    Settings.llm = Ollama(
        model=model_name,
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434"),
        request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
    )
    print(f"✅ LLM 已切换至：{model_name}")


def ingest_file(file_path: str) -> dict:
    """摄入单个上传文件（PDF / MD）到知识库，复用 engine 已有 client。"""
    import gc
    from pathlib import Path
    from llama_index.core import VectorStoreIndex, Document, StorageContext
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.vector_stores.qdrant import QdrantVectorStore

    eng = _init_engine()
    path = Path(file_path)
    suffix = path.suffix.lower()
    docs = []

    if suffix == ".pdf":
        import pymupdf4llm
        pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        for chunk in pages:
            text = chunk.get("text", "").strip()
            if not text:
                continue
            page = chunk.get("metadata", {}).get("page", 0) + 1
            docs.append(Document(
                text=text,
                metadata={
                    "doc_name": path.name, "doc_type": "manual",
                    "language": "en", "page_start": page, "page_end": page,
                    "machine_model": "", "chunk_type": "body",
                },
            ))
        del pages
    elif suffix == ".md":
        text = path.read_text(encoding="utf-8").strip()
        if text:
            docs.append(Document(
                text=text,
                metadata={
                    "doc_name": path.name, "doc_type": "obsidian_note",
                    "language": "zh", "file_path": str(path),
                },
            ))
    else:
        return {"success": False, "message": f"不支持的格式：{suffix}，仅支持 PDF / MD"}

    if not docs:
        return {"success": False, "message": "未能提取到文本，可能是纯图片 PDF"}

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
    del docs

    vs = QdrantVectorStore(client=eng["client"], collection_name=eng["collection"])
    ctx = StorageContext.from_defaults(vector_store=vs)
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
    n = len(nodes)
    del nodes
    gc.collect()
    return {"success": True, "message": f"✅ 摄入完成：**{path.name}**（{n} chunks）", "chunks": n}


def get_kb_documents() -> list:
    """返回知识库中所有文档的统计列表（含文件路径）。"""
    from collections import Counter
    eng = _init_engine()
    doc_counts, doc_meta = Counter(), {}
    offset = None
    while True:
        points, next_offset = eng["client"].scroll(
            collection_name=eng["collection"],
            limit=500, offset=offset,
            with_payload=True, with_vectors=False,
        )
        if not points:
            break
        for p in points:
            name = p.payload.get("doc_name", "未知")
            doc_counts[name] += 1
            if name not in doc_meta:
                file_path    = p.payload.get("file_path", "")
                machine_model = p.payload.get("machine_model", "")
                doc_type     = p.payload.get("doc_type", "")
                # 优先用存储的 file_path；PDF 用 machine_model 作目录提示
                if file_path:
                    location = file_path
                elif machine_model:
                    location = machine_model
                else:
                    location = "—"
                doc_meta[name] = {"doc_type": doc_type, "location": location}
        offset = next_offset
        if next_offset is None:
            break
    return [
        {
            "文档名": n,
            "类型": doc_meta[n]["doc_type"],
            "位置": doc_meta[n]["location"],
            "Chunks": c,
        }
        for n, c in sorted(doc_counts.items())
    ]


def delete_document(doc_name: str) -> str:
    """删除知识库中指定文档的全部向量。"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    eng = _init_engine()
    eng["client"].delete(
        collection_name=eng["collection"],
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=doc_name))])
        ),
    )
    return f"✅ 已删除：**{doc_name}**"


def engine_db_status() -> dict:
    """通过引擎已有的 client 查询库状态（避免重复开锁）"""
    if _engine is None:
        return {"collection": "—", "points": 0, "status": "engine not initialized"}
    try:
        info = _engine["client"].get_collection(_engine["collection"])
        return {"collection": _engine["collection"], "points": info.points_count, "status": "ok"}
    except Exception as e:
        return {"collection": _engine.get("collection", "—"), "points": 0, "status": str(e)}


def query_stream(question: str, mode: str = "qa", history: list = None):
    """
    流式查询，逐 token yield。

    Yields:
        (delta: str, is_final: bool, citations: list, has_result: bool)
        - 普通 chunk：(文本片段, False, [], True)
        - 最终 chunk：("", True, 引用列表, True)
        - 无结果：(NO_RESULT_RESPONSE, True, [], False)
    """
    from src.prompts import (
        KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    )
    from llama_index.core import Settings

    threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.30"))
    rerank_top_n = int(os.getenv("RERANK_TOP_N", "4"))
    eng = _init_engine()

    # ── 1. 检索
    nodes = eng["retriever"].retrieve(question)
    nodes = [n for n in nodes if (n.score or 0.0) >= threshold]

    if not nodes:
        yield NO_RESULT_RESPONSE, True, [], False
        return

    # ── 2. 重排序
    if eng["reranker"] is not None:
        reranked = eng["reranker"].postprocess_nodes(nodes, query_str=question)
    else:
        reranked = nodes[:rerank_top_n] if len(nodes) > rerank_top_n else nodes

    # ── 3. 构建带溯源的 Context
    ctx_parts, citations = [], []
    for i, n in enumerate(reranked, 1):
        m = n.metadata
        doc_name = m.get("doc_name", "未知文档")
        doc_type = m.get("doc_type", "")
        page     = m.get("page_start", "")
        date     = m.get("fault_date", "")
        score    = n.score or 0.0

        if doc_type == "manual" and page:
            cite = f"《{doc_name}》第 {page} 页"
        elif doc_type == "fault_history" and date:
            codes = m.get("error_codes", [])
            code_str = f"[{', '.join(codes)}] " if codes else ""
            cite = f"故障履历 {code_str}{date} — {doc_name}"
        elif doc_type == "checksheet":
            row = m.get("row_index", "")
            cite = f"《{doc_name}》第 {row} 行"
        else:
            file_path = m.get("file_path", "")
            cite = f"{doc_name}" + (f"（{Path(file_path).parent.name}）" if file_path else "")

        ctx_parts.append(
            f"[参考资料 {i}] 来源：{cite}\n置信度：{score:.3f}\n"
            f"内容：\n{n.get_content()}\n" + "─" * 50
        )
        citations.append(f"[{i}] {cite}  (置信度 {score:.3f})")

    context = "\n\n".join(ctx_parts)

    # ── 4. 多轮对话历史（最近 6 条消息 = 3 轮）
    history_prefix = ""
    if history:
        recent = history[-6:]
        turns = []
        for msg in recent:
            role = "工程师" if msg["role"] == "user" else "助手"
            content = msg["content"][:400]
            turns.append(f"{role}：{content}")
        history_prefix = "【对话历史（最近几轮）】\n" + "\n".join(turns) + "\n\n"

    query_with_history = history_prefix + question

    # ── 5. 选择 Prompt
    prompt_tmpl = (
        TROUBLESHOOTING_PROMPT if mode == "troubleshoot"
        else KNOWLEDGE_QA_PROMPT
    )
    final_prompt = prompt_tmpl.format(context=context, query=query_with_history)

    # ── 6. 流式调用 LLM
    for token in Settings.llm.stream_complete(final_prompt):
        yield token.delta, False, [], True

    yield "", True, citations, True


def query(question: str, mode: str = "qa") -> dict:
    """
    执行一次完整查询。

    Args:
        question: 工程师输入的问题或故障描述
        mode: "qa"（知识问答）或 "troubleshoot"（故障排查）

    Returns:
        {
            "answer": str,          # LLM 回答
            "citations": list[str], # 引用列表
            "retrieved": int,       # 检索到的文档数
            "has_result": bool,     # 是否找到相关内容
        }
    """
    from src.prompts import (
        KNOWLEDGE_QA_PROMPT, TROUBLESHOOTING_PROMPT, NO_RESULT_RESPONSE
    )
    from llama_index.core import Settings

    threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.30"))
    rerank_top_n = int(os.getenv("RERANK_TOP_N", "4"))
    eng = _init_engine()

    # ── 1. 检索 ──────────────────────────────────────────────
    nodes = eng["retriever"].retrieve(question)
    nodes = [n for n in nodes if (n.score or 0.0) >= threshold]

    if not nodes:
        return {
            "answer": NO_RESULT_RESPONSE,
            "citations": [],
            "retrieved": 0,
            "has_result": False,
        }

    # ── 2. 重排序 ─────────────────────────────────────────────
    if eng["reranker"] is not None:
        reranked = eng["reranker"].postprocess_nodes(nodes, query_str=question)
    else:
        reranked = nodes[:rerank_top_n] if len(nodes) > rerank_top_n else nodes

    # ── 3. 构建带溯源的 Context ──────────────────────────────
    ctx_parts, citations = [], []
    for i, n in enumerate(reranked, 1):
        m = n.metadata
        doc_name = m.get("doc_name", "未知文档")
        doc_type = m.get("doc_type", "")
        page     = m.get("page_start", "")
        date     = m.get("fault_date", "")
        score    = n.score or 0.0

        if doc_type == "manual" and page:
            cite = f"《{doc_name}》第 {page} 页"
        elif doc_type == "fault_history" and date:
            codes = m.get("error_codes", [])
            code_str = f"[{', '.join(codes)}] " if codes else ""
            cite = f"故障履历 {code_str}{date} — {doc_name}"
        elif doc_type == "checksheet":
            row = m.get("row_index", "")
            cite = f"《{doc_name}》第 {row} 行"
        else:
            file_path = m.get("file_path", "")
            cite = f"{doc_name}" + (f"（{Path(file_path).parent.name}）" if file_path else "")

        ctx_parts.append(
            f"[参考资料 {i}] 来源：{cite}\n置信度：{score:.3f}\n"
            f"内容：\n{n.get_content()}\n" + "─" * 50
        )
        citations.append(f"[{i}] {cite}  (置信度 {score:.3f})")

    context = "\n\n".join(ctx_parts)

    # ── 4. 选择 Prompt ────────────────────────────────────────
    prompt_tmpl = (
        TROUBLESHOOTING_PROMPT if mode == "troubleshoot"
        else KNOWLEDGE_QA_PROMPT
    )
    final_prompt = prompt_tmpl.format(context=context, query=question)

    # ── 5. 调用 LLM ───────────────────────────────────────────
    response = Settings.llm.complete(final_prompt)

    return {
        "answer": str(response),
        "citations": citations,
        "retrieved": len(reranked),
        "has_result": True,
    }
