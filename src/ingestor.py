# src/ingestor.py
# Nikon Expert — 统一数据摄入器
# 结构感知分块 + doc_id 去重 + FTS 同步写入

import os
import re
import hashlib
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


def _doc_id(file_path: str) -> str:
    """从文件路径生成稳定的 doc_id"""
    return hashlib.sha256(file_path.encode()).hexdigest()[:12]


def _chunk_hash(text: str) -> str:
    """内容哈希，用于去重"""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


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

    # 直接查内部属性，避免读 Settings.embed_model 触发 llama_index 的 OpenAI 默认解析器
    if Settings._embed_model is None:
        from src.device import get_device
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=embed_path, max_length=512, device=get_device()
        )
    Settings.llm = None

    client = QdrantClient(url=os.getenv("QDRANT_URL")) if os.getenv("QDRANT_URL") else QdrantClient(path=qdrant_path)
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


def _delete_existing(client, collection: str, doc_name: str) -> int:
    """删除向量库中指定文档的全部数据（去重前置步骤）"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
    try:
        result = client.delete(
            collection_name=collection,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="doc_name", match=MatchValue(value=doc_name))
                ])
            ),
        )
        return getattr(result, "status", "ok")
    except Exception:
        return "skip"


def _fts_sync(rid, doc_name, doc_type, machine_model, section_title, text, file_path=""):
    """同步写入 FTS 层"""
    try:
        from src.fulltext import ingest_fts
        ingest_fts(rid, doc_name, doc_type, machine_model, section_title, text, file_path)
    except Exception:
        pass


def _fts_delete_by_name(doc_name: str):
    """删除 FTS 中指定文档"""
    try:
        from src.fulltext import delete_doc_fts_by_name
        delete_doc_fts_by_name(doc_name)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 辅助: 按 markdown 标题分割文本
# ─────────────────────────────────────────────────────────────
def _split_by_headings(text: str, max_chunk: int = 1024) -> list:
    """
    按 # / ## / ### 标题分割文本，返回 [(section_title, section_text), ...]
    超长 section 按 max_chunk 继续切分。
    """
    sections = re.split(r'^(#{1,3}\s.+)$', text, flags=re.MULTILINE)

    # sections 交替: [preamble, heading, body, heading, body, ...]
    result = []
    current_title = ""
    current_body = ""

    for i, part in enumerate(sections):
        if re.match(r'^#{1,3}\s', part):
            # 先 flush 之前的
            if current_body.strip():
                result.append((current_title, current_body.strip()))
            current_title = part.strip()
            current_body = ""
        else:
            current_body += part

    if current_body.strip():
        result.append((current_title, current_body.strip()))

    # 超长 section 二次切分
    final = []
    for title, body in result:
        if len(body) <= max_chunk * 3:  # 粗估字符数
            final.append((title, body))
        else:
            # 按段落切分
            paragraphs = body.split('\n\n')
            chunk = ""
            for para in paragraphs:
                if len(chunk) + len(para) > max_chunk * 3 and chunk:
                    final.append((title, chunk.strip()))
                    chunk = ""
                chunk += para + "\n\n"
            if chunk.strip():
                final.append((title, chunk.strip()))

    return final


# ─────────────────────────────────────────────────────────────
# 1. Obsidian Vault (.md 文件)
# ─────────────────────────────────────────────────────────────
def ingest_obsidian(vault_path: str, limit: int = 0) -> int:
    """摄入 Obsidian Vault，按 ## 标题分区，短笔记保持整体"""
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter

    print(f"\n📂 摄入 Obsidian Vault：{vault_path}")
    vs, ctx, qdrant_client = _get_storage()
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

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
        print(f"  ⚠️  跳过不可读文件：{skipped} 个")

    if limit and limit > 0:
        readable = readable[:limit]
        print(f"  🔢 限制摄入前 {limit} 个文件")

    if not readable:
        print("  ❌ 没有可读的 .md 文件")
        return 0

    parent_chunk_size = int(os.getenv("PARENT_CHUNK_SIZE", "1024"))
    total_nodes = 0

    for f in readable:
        text = f.read_text(encoding="utf-8").strip()
        if not text or len(text) < 10:
            continue

        rid = _doc_id(str(f))
        doc_name = f.name

        # 去重：先删除旧数据
        _delete_existing(qdrant_client, collection, doc_name)
        _fts_delete_by_name(doc_name)

        # 解析 frontmatter
        fm_title, fm_tags = "", []
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm_text = parts[1]
                body = parts[2].strip()
                for line in fm_text.strip().split('\n'):
                    if line.lower().startswith("title"):
                        fm_title = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.lower().startswith("tags"):
                        raw = line.split(":", 1)[1].strip()
                        fm_tags = [t.strip().strip("[]'\"") for t in raw.split(",")]
                text = body

        # 按标题分区
        sections = _split_by_headings(text, max_chunk=parent_chunk_size)

        docs = []
        for sec_title, sec_text in sections:
            if not sec_text.strip():
                continue
            title = fm_title or sec_title or doc_name
            docs.append(Document(
                text=sec_text,
                metadata={
                    "doc_name":      doc_name,
                    "doc_type":      "obsidian_note",
                    "language":      "zh",
                    "file_path":     str(f),
                    "doc_id":        rid,
                    "chunk_hash":    _chunk_hash(sec_text),
                    "section_title": title,
                    "tags":          fm_tags,
                },
            ))

            # FTS 同步
            _fts_sync(rid, doc_name, "obsidian_note", "", title, sec_text, str(f))

        if not docs:
            continue

        # 短笔记（<512 字符）保持整体，长笔记再按句子切
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
        nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
        VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
        total_nodes += len(nodes)

    qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {total_nodes} 个 Chunk（{len(readable)} 文件）")
    return total_nodes


# ─────────────────────────────────────────────────────────────
# 1b. OKF（Open Knowledge Format，Google 2026 开放标准）
#     一个概念=一个 .md（YAML frontmatter + 正文）。这是"标准输入口"：
#     别处按 OKF 整理好的知识可直接导入本系统的 FTS + 向量库。
# ─────────────────────────────────────────────────────────────
def _parse_okf(text: str):
    """解析 OKF markdown，返回 (frontmatter_dict, body)。无/坏 frontmatter 时 fm={}。"""
    import yaml
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1])
            except Exception:
                fm = None
            if isinstance(fm, dict):
                return fm, parts[2].strip()
    return {}, text.strip()


def _as_list(v) -> list:
    """把 frontmatter 里可能是 str / list 的字段统一成 list[str]。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def ingest_okf(path: str, limit: int = 0, storage=None) -> int:
    """摄入 OKF：可传 OKF bundle 目录，或单个 .md 文件。

    storage: 可选 (vector_store, storage_ctx, qdrant_client)。
        传入则复用（供运行中的 app 内调用，避免重复开 Qdrant client 触发锁冲突）；
        为空则自建并在结束时关闭（CLI 独立运行模式）。

    frontmatter 映射：
      type(必填) → doc_type；title → doc_name；description 并入正文利于检索；
      tags → tags，并从中抽取机型/error code；timestamp|fault_date → fault_date；
      resource → file_path；额外字段 machine_model / error_codes 直接采纳。
    正文按 markdown 标题分块，写入向量库 + FTS（与其他摄入器一致，含去重）。
    log.md（OKF 变更日志）会跳过。
    """
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter
    from src.router import extract_machine_models, extract_error_codes

    p = Path(path)
    if p.is_dir():
        files = [f for f in sorted(p.rglob("*.md")) if f.name.lower() != "log.md"]
    elif p.suffix.lower() == ".md":
        files = [p]
    else:
        print(f"  ⚠️  OKF 导入需要 .md 文件或目录：{path}")
        return 0
    if limit and limit > 0:
        files = files[:limit]
    if not files:
        print("  ❌ 未找到 .md 文件")
        return 0

    print(f"\n📗 摄入 OKF：{path}（{len(files)} 个文件）")
    own_storage = storage is None
    if own_storage:
        vs, ctx, qdrant_client = _get_storage()
    else:
        vs, ctx, qdrant_client = storage
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    parent_chunk_size = int(os.getenv("PARENT_CHUNK_SIZE", "1024"))
    total_nodes = 0

    for f in files:
        try:
            text = f.read_text(encoding="utf-8").strip()
        except (OSError, IOError):
            continue
        if not text:
            continue

        fm, body = _parse_okf(text)
        if not body:
            body = text

        rid       = _doc_id(str(f))
        doc_type  = str(fm.get("type") or "okf_note").strip()
        title     = str(fm.get("title") or f.stem).strip()
        doc_name  = title
        tags      = _as_list(fm.get("tags"))
        timestamp = str(fm.get("timestamp") or fm.get("fault_date") or "").strip()
        resource  = str(fm.get("resource") or str(f)).strip()
        description = str(fm.get("description") or "").strip()
        language  = str(fm.get("language") or "zh").strip()

        # 机型 / error code：优先显式字段，否则从 tags 抽
        tag_blob = " ".join(tags)
        models = _as_list(fm.get("machine_model")) or extract_machine_models(tag_blob)
        machine_model = models[0] if models else ""
        ecs = _as_list(fm.get("error_codes")) or _as_list(fm.get("error_code"))
        ecs = list(dict.fromkeys(ecs + extract_error_codes(tag_blob)))

        # 去重
        _delete_existing(qdrant_client, collection, doc_name)
        _fts_delete_by_name(doc_name)

        # description 并入正文开头（提升召回）
        full = f"{description}\n\n{body}".strip() if description else body
        sections = _split_by_headings(full, max_chunk=parent_chunk_size)

        docs = []
        for sec_title, sec_text in sections:
            if not sec_text.strip():
                continue
            sec = sec_title or title
            meta = {
                "doc_name":      doc_name,
                "doc_type":      doc_type,
                "language":      language,
                "file_path":     resource,
                "doc_id":        rid,
                "chunk_hash":    _chunk_hash(sec_text),
                "section_title": sec,
                "tags":          tags,
            }
            if machine_model:
                meta["machine_model"] = machine_model
            if ecs:
                meta["error_codes"] = ecs
            if timestamp:
                meta["fault_date"] = timestamp
            docs.append(Document(text=sec_text, metadata=meta))
            _fts_sync(rid, doc_name, doc_type, machine_model, sec, sec_text, resource)

        if not docs:
            continue
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
        nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
        VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
        total_nodes += len(nodes)

    if own_storage:
        qdrant_client.close()
    print(f"  ✅ OKF 摄入完成，共 {total_nodes} 个 Chunk（{len(files)} 文件）")
    return total_nodes


# ─────────────────────────────────────────────────────────────
# 1c. 大型电路图 PDF（文字优先，逐页 FTS，存真实页码+路径）
#     针对 S207 电路图这类 几千页/矢量密集图：整页 VLM 不可行，
#     文字层才是可查资产（元件名/接线号/信号名/面板单元）。
# ─────────────────────────────────────────────────────────────
_BLANK_MARKERS = ("intentionally left blank", "このページは空白")


def ingest_circuit_pdf(pdf_path: str, machine_model: str = "", limit: int = 0,
                       vector: bool = False) -> int:
    """把大型电路图 PDF 的文字逐页写入 FTS（可选同时向量化，支持中文跨语言检索）。

    每页一条记录，section_title/page_start 记真实页码，file_path 记源 PDF 绝对路径，
    doc_type=circuit_diagram —— 便于按元件/接线号搜索并定位到确切页。
    vector=True 时额外用 BGE-M3 向量化每页文字（多语言，可用中文查日/英图纸）。
    跳过空白页/封面。
    """
    import fitz

    path = Path(pdf_path).expanduser().resolve()
    if not path.exists():
        print(f"  ❌ 未找到 PDF：{pdf_path}")
        return 0
    doc_name = path.name
    rid = _doc_id(str(path))
    print(f"\n🔌 摄入电路图（文字层{'＋向量' if vector else ''}）：{doc_name}")

    # 去重旧记录
    _fts_delete_by_name(doc_name)

    from src.fulltext import init_fts, ingest_fts
    init_fts(os.getenv("FTS_DB_PATH", "./data/fts.db"))

    vs = ctx = qdrant_client = collection = None
    if vector:
        vs, ctx, qdrant_client = _get_storage()
        collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
        _delete_existing(qdrant_client, collection, doc_name)

    docpdf = fitz.open(str(path))
    n_pages = len(docpdf)
    if limit and limit > 0:
        n_pages = min(n_pages, limit)

    from llama_index.core import VectorStoreIndex, Document

    count = 0
    batch_docs = []
    for i in range(n_pages):
        text = docpdf[i].get_text().strip()
        if len(text) < 20:
            continue
        low = text.lower()
        if any(m in low for m in _BLANK_MARKERS) and len(text) < 200:
            continue
        page = i + 1
        ingest_fts(
            f"{rid}_{page}", doc_name, "circuit_diagram", machine_model,
            f"第{page}页", text, str(path),
        )
        if vector:
            batch_docs.append(Document(
                text=text[:4000],
                metadata={
                    "doc_name": doc_name, "doc_type": "circuit_diagram",
                    "machine_model": machine_model, "page_start": page,
                    "file_path": str(path), "doc_id": rid,
                    "chunk_hash": _chunk_hash(text[:200]),
                },
            ))
        count += 1
        if count % 500 == 0:
            print(f"    …已处理 {count} 页")
            if vector and batch_docs:
                VectorStoreIndex(batch_docs, storage_context=ctx, show_progress=False)
                batch_docs = []
    docpdf.close()
    if vector and batch_docs:
        VectorStoreIndex(batch_docs, storage_context=ctx, show_progress=False)
    if vector and qdrant_client:
        qdrant_client.close()
    print(f"  ✅ 电路图摄入完成：{count} 页（源 {n_pages} 页）"
          + ("＋向量化" if vector else ""))
    return count


# ─────────────────────────────────────────────────────────────
# 2. PDF 手册 — 父子分块
# ─────────────────────────────────────────────────────────────
def ingest_pdf(pdf_path: str, machine_model: str = "", language: str = "en") -> int:
    """摄入 PDF，按章节做父子分块"""
    import pymupdf4llm
    from llama_index.core import VectorStoreIndex, Document

    path = Path(pdf_path)
    print(f"\n📄 摄入 PDF：{path.name}")
    vs, ctx, qdrant_client = _get_storage()
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    rid = _doc_id(str(path))
    doc_name = path.name

    # 去重
    _delete_existing(qdrant_client, collection, doc_name)
    _fts_delete_by_name(doc_name)

    pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)

    # 合并所有页为一个完整文档
    full_text = ""
    page_map = {}  # page_number -> start_char
    for chunk in pages:
        text = chunk.get("text", "").strip()
        if not text:
            continue
        page = chunk.get("metadata", {}).get("page", 0) + 1
        page_map[page] = len(full_text)
        full_text += text + "\n\n"

    if not full_text.strip():
        print("  ❌ 未能提取到文本")
        return 0

    # 按标题分出大块（父块）
    parent_chunk_size = int(os.getenv("PARENT_CHUNK_SIZE", "1024"))
    sections = _split_by_headings(full_text, max_chunk=parent_chunk_size)

    # 确定每个 section 属于哪一页
    def _guess_page(char_pos: int) -> int:
        best_page = 1
        for pg, pos in sorted(page_map.items()):
            if pos <= char_pos:
                best_page = pg
        return best_page

    from llama_index.core.node_parser import SentenceSplitter
    child_splitter = SentenceSplitter(chunk_size=256, chunk_overlap=32)

    all_nodes = []
    for sec_title, sec_text in sections:
        if len(sec_text.strip()) < 20:
            continue

        page = _guess_page(full_text.find(sec_text[:50]))
        pid = _chunk_hash(sec_text)

        # 父块直接存（用于返回上下文）
        parent_doc = Document(
            text=sec_text,
            metadata={
                "doc_name":      doc_name,
                "doc_type":      "manual",
                "language":      language,
                "page_start":    page,
                "machine_model": machine_model,
                "chunk_type":    "parent",
                "doc_id":        rid,
                "chunk_hash":    pid,
                "section_title": sec_title,
            },
        )
        all_nodes.append(parent_doc)

        # 子块用于精准检索
        if len(sec_text) > 256:
            children = child_splitter.get_nodes_from_documents([parent_doc], show_progress=False)
            for child in children:
                child.metadata["chunk_type"] = "child"
                child.metadata["parent_hash"] = pid
            all_nodes.extend(children)

        # FTS 同步（存父块文本）
        _fts_sync(rid, doc_name, "manual", machine_model, sec_title, sec_text)

    VectorStoreIndex(all_nodes, storage_context=ctx, show_progress=True)
    qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {len(all_nodes)} 个 Chunk（{len(pages)} 页，{len(sections)} 章节）")
    return len(all_nodes)


def ingest_pdf_dir(pdf_dir: str = None, machine_model: str = "") -> int:
    """批量摄入目录下所有 PDF"""
    pdf_dir = pdf_dir or os.getenv("MANUALS_DIR", "./data/raw/manuals")
    total = 0
    for pdf in Path(pdf_dir).glob("*.pdf"):
        total += ingest_pdf(str(pdf), machine_model=machine_model)
    print(f"\n  📊 PDF 批量摄入完成，共 {total} 个 Chunk")
    return total


# ─────────────────────────────────────────────────────────────
# 3. Excel Checksheet（保持现有逻辑 + 去重 + FTS）
# ─────────────────────────────────────────────────────────────
def ingest_excel(excel_path: str, machine_model: str = "") -> int:
    """摄入 Excel Checksheet"""
    import openpyxl
    from llama_index.core import VectorStoreIndex, Document

    path = Path(excel_path)
    print(f"\n📊 摄入 Checksheet：{path.name}")
    vs, ctx, qdrant_client = _get_storage()
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    rid = _doc_id(str(path))
    doc_name = path.name
    _delete_existing(qdrant_client, collection, doc_name)
    _fts_delete_by_name(doc_name)

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
                    "doc_name":      doc_name,
                    "doc_type":      "checksheet",
                    "language":      "ja",
                    "sheet_name":    sheet_name,
                    "row_index":     ri,
                    "machine_model": machine_model,
                    "chunk_type":    "record",
                    "doc_id":        rid,
                    "chunk_hash":    _chunk_hash(text),
                },
            ))
            _fts_sync(rid, doc_name, "checksheet", machine_model, f"Sheet:{sheet_name} Row:{ri}", text)

        VectorStoreIndex(docs, storage_context=ctx, show_progress=True)
        total += len(docs)
        print(f"  ✅ 工作表 '{sheet_name}'：{len(docs)} 行摄入完成")

    qdrant_client.close()
    return total


# ─────────────────────────────────────────────────────────────
# 4. 故障履历（保持现有逻辑 + 去重 + FTS）
# ─────────────────────────────────────────────────────────────
def ingest_fault_history(data_path: str) -> int:
    """摄入故障履历 JSON/CSV"""
    import json
    import pandas as pd
    from llama_index.core import VectorStoreIndex, Document

    path = Path(data_path)
    print(f"\n🗂  摄入故障履历：{path.name}")
    vs, ctx, qdrant_client = _get_storage()
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    rid = _doc_id(str(path))
    doc_name = path.name
    _delete_existing(qdrant_client, collection, doc_name)
    _fts_delete_by_name(doc_name)

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
                "doc_name":      doc_name,
                "doc_type":      "fault_history",
                "language":      "zh",
                "machine_model": g("machine_model"),
                "error_codes":   ec,
                "fault_date":    g("fault_date"),
                "chunk_type":    "record",
                "doc_id":        rid,
                "chunk_hash":    _chunk_hash(text),
            },
        ))
        _fts_sync(rid, doc_name, "fault_history", g("machine_model"),
                  g("error_code"), text)

    VectorStoreIndex(docs, storage_context=ctx, show_progress=True)
    qdrant_client.close()
    print(f"  ✅ 摄入完成，共 {len(docs)} 条故障记录")
    return len(docs)


# ─────────────────────────────────────────────────────────────
# 5. 查询向量库状态
# ─────────────────────────────────────────────────────────────
def db_status() -> dict:
    """返回向量数据库当前状态"""
    from qdrant_client import QdrantClient

    client     = QdrantClient(url=os.getenv("QDRANT_URL")) if os.getenv("QDRANT_URL") else QdrantClient(path=os.getenv("QDRANT_PATH", "./data/qdrant_db"))
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
