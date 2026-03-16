# src/ingestor.py
# Nikon Expert — 统一数据摄入器（兼容 llama-index 0.10+ / 0.12+）
# 支持：Obsidian Vault (.md) / PDF 手册 / Excel Checksheet / 故障履历 (JSON/CSV)

import os
import uuid
import json
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


def _get_storage():
    """初始化并返回 (vector_store, storage_ctx, client)"""
    from llama_index.core import StorageContext, Settings
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    embed_path  = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
    qdrant_path = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    collection  = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    # 只在首次调用时加载模型，避免重复加载占用内存
    if Settings.embed_model is None:
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=embed_path, max_length=512, device="mps"
        )
    Settings.llm = None

    client = QdrantClient(path=qdrant_path)
    existing = [c.name for c in client.get_collections().collections]
    if collection not in existing:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )
        print(f"  ✅ Qdrant Collection 创建：{collection}")

    vs  = QdrantVectorStore(client=client, collection_name=collection)
    ctx = StorageContext.from_defaults(vector_store=vs)
    return vs, ctx, client


# ─────────────────────────────────────────────────────────────
# 1. Obsidian Vault (.md 文件)
# ─────────────────────────────────────────────────────────────
def ingest_obsidian(vault_path: str, limit: int = 0) -> int:
    """摄入 Obsidian Vault 中所有 .md 文件"""
    from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
    from llama_index.core.node_parser import SentenceSplitter

    print(f"\n📂 摄入 Obsidian Vault：{vault_path}")
    _, ctx, _qdrant_client = _get_storage()

    # 预过滤：跳过 iCloud 占位符等不可读文件
    all_md = list(Path(vault_path).rglob("*.md"))
    readable = []
    skipped = 0
    for f in all_md:
        try:
            with open(f, "rb"):
                pass
            readable.append(f)
        except (OSError, IOError):
            skipped += 1
    if skipped:
        print(f"  ⚠️  跳过不可读文件（iCloud 占位符等）：{skipped} 个")

    if limit and limit > 0:
        readable = readable[:limit]
        print(f"  🔢 限制摄入前 {limit} 个文件")

    if not readable:
        print("  ❌ 没有可读的 .md 文件")
        return 0

    reader = SimpleDirectoryReader(
        input_files=readable,
        file_metadata=lambda p: {
            "doc_name":  Path(p).name,
            "doc_type":  "obsidian_note",
            "language":  "zh",
            "file_path": str(p),
            "doc_id":    str(uuid.uuid4()),
        },
    )
    docs  = reader.load_data()
    print(f"  📄 读取文件：{len(docs)} 个")

    nodes = SentenceSplitter(chunk_size=512, chunk_overlap=64).get_nodes_from_documents(
        docs, show_progress=True
    )
    print(f"  🔪 分块数量：{len(nodes)}")

    VectorStoreIndex(nodes, storage_context=ctx, show_progress=True)
    _qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {len(nodes)} 个 Chunk")
    return len(nodes)


# ─────────────────────────────────────────────────────────────
# 2. PDF 手册
# ─────────────────────────────────────────────────────────────
def ingest_pdf(pdf_path: str, machine_model: str = "", language: str = "en") -> int:
    """摄入单个 PDF 文件（官方手册）"""
    import pymupdf4llm
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter

    path = Path(pdf_path)
    print(f"\n📄 摄入 PDF：{path.name}")
    _, ctx, _qdrant_client = _get_storage()

    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    docs  = []
    for chunk in pages:
        text = chunk.get("text", "").strip()
        if not text:
            continue
        page = chunk.get("metadata", {}).get("page", 0) + 1
        docs.append(Document(
            text=text,
            metadata={
                "doc_name":     path.name,
                "doc_type":     "manual",
                "language":     language,
                "page_start":   page,
                "page_end":     page,
                "machine_model": machine_model,
                "chunk_type":   "body",
                "doc_id":       str(uuid.uuid4()),
            },
        ))

    nodes = SentenceSplitter(chunk_size=512, chunk_overlap=64).get_nodes_from_documents(
        docs, show_progress=True
    )
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=True)
    _qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {len(nodes)} 个 Chunk（{len(docs)} 页）")
    return len(nodes)


def ingest_pdf_dir(pdf_dir: str = None, machine_model: str = "") -> int:
    """批量摄入目录下所有 PDF"""
    pdf_dir = pdf_dir or os.getenv("MANUALS_DIR", "./data/raw/manuals")
    total = 0
    for pdf in Path(pdf_dir).glob("*.pdf"):
        total += ingest_pdf(str(pdf), machine_model=machine_model)
    print(f"\n  📊 PDF 批量摄入完成，共 {total} 个 Chunk")
    return total


# ─────────────────────────────────────────────────────────────
# 3. Excel Checksheet（日文）
# ─────────────────────────────────────────────────────────────
def ingest_excel(excel_path: str, machine_model: str = "") -> int:
    """摄入 Excel Checksheet（逐行转为自然语言 Chunk）"""
    import openpyxl
    from llama_index.core import VectorStoreIndex, Document

    path = Path(excel_path)
    print(f"\n📊 摄入 Checksheet：{path.name}")
    _, ctx, _qdrant_client = _get_storage()

    wb    = openpyxl.load_workbook(str(path), data_only=True)
    total = 0

    for sheet_name in wb.sheetnames:
        ws   = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            continue
        headers = [str(c) if c else "" for c in rows[0]]
        docs    = []

        for ri, row in enumerate(rows[1:], start=2):
            cells = [str(c) if c else "" for c in row]
            if all(c == "" for c in cells):
                continue
            text = " | ".join(
                f"{h}: {v}" for h, v in zip(headers, cells) if h and v
            )
            docs.append(Document(
                text=text,
                metadata={
                    "doc_name":     path.name,
                    "doc_type":     "checksheet",
                    "language":     "ja",
                    "sheet_name":   sheet_name,
                    "row_index":    ri,
                    "machine_model": machine_model,
                    "chunk_type":   "record",
                    "doc_id":       str(uuid.uuid4()),
                },
            ))

        VectorStoreIndex(docs, storage_context=ctx, show_progress=True)
        total += len(docs)
        print(f"  ✅ 工作表 '{sheet_name}'：{len(docs)} 行摄入完成")

    _qdrant_client.close()
    return total


# ─────────────────────────────────────────────────────────────
# 4. 故障履历（JSON / CSV）
# ─────────────────────────────────────────────────────────────
def ingest_fault_history(data_path: str) -> int:
    """
    摄入故障履历。
    支持格式：
      - JSON：List[dict]，字段参考下方
      - CSV：同字段，含 header 行
    必须字段（可为空）：fault_date, machine_id, machine_model,
                        error_code, symptom, root_cause, solution, engineer
    """
    import pandas as pd
    from llama_index.core import VectorStoreIndex, Document

    path = Path(data_path)
    print(f"\n🗂  摄入故障履历：{path.name}")
    _, ctx, _qdrant_client = _get_storage()

    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
    elif path.suffix.lower() in (".csv", ".xlsx", ".xls"):
        df      = pd.read_csv(path) if path.suffix == ".csv" else pd.read_excel(path)
        records = df.to_dict("records")
    else:
        print(f"  ⚠️  不支持的格式：{path.suffix}")
        return 0

    docs = []
    for rec in records:
        g  = lambda k: str(rec.get(k, "") or "")
        text = (
            f"故障日期: {g('fault_date')}\n"
            f"机台: {g('machine_id')} ({g('machine_model')})\n"
            f"Error Code: {g('error_code')}\n"
            f"故障现象: {g('symptom')}\n"
            f"根因分析: {g('root_cause')}\n"
            f"解决方案: {g('solution')}\n"
            f"处理人员: {g('engineer')}"
        )
        ec = [g("error_code")] if g("error_code") else []
        docs.append(Document(
            text=text,
            metadata={
                "doc_name":     path.name,
                "doc_type":     "fault_history",
                "language":     "zh",
                "machine_model": g("machine_model"),
                "error_codes":  ec,
                "fault_date":   g("fault_date"),
                "chunk_type":   "record",
                "doc_id":       str(uuid.uuid4()),
            },
        ))

    VectorStoreIndex(docs, storage_context=ctx, show_progress=True)
    _qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {len(docs)} 条故障记录")
    return len(docs)


# ─────────────────────────────────────────────────────────────
# 5. 查询向量库状态
# ─────────────────────────────────────────────────────────────
def db_status() -> dict:
    """返回向量数据库当前状态"""
    from qdrant_client import QdrantClient

    client     = QdrantClient(path=os.getenv("QDRANT_PATH", "./data/qdrant_db"))
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    try:
        info = client.get_collection(collection)
        return {
            "collection": collection,
            "points": info.points_count,
            "status": "ok",
        }
    except Exception as e:
        return {"collection": collection, "points": 0, "status": str(e)}
