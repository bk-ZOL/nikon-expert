# src/ingest_docx.py
# Nikon Expert — docx 摄入（WPS 知识库主力格式）
# docx --mammoth--> markdown --> 复用 ingestor 的标题分块 + Qdrant/FTS 双写
#
# 依赖：pip install mammoth   （加入 requirements.txt）

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from src.ingestor import (
    _doc_id, _chunk_hash, _get_storage,
    _delete_existing, _fts_sync, _fts_delete_by_name,
    _split_by_headings,
)


def docx_to_markdown(docx_path: str) -> str:
    """docx -> markdown。保留标题层级/列表/表格文本，忽略图片。"""
    import mammoth
    with open(docx_path, "rb") as f:
        result = mammoth.convert_to_markdown(
            f,
            convert_image=mammoth.images.img_element(lambda image: {"src": ""}),
        )
    return result.value.strip()


def ingest_docx(
    docx_path: str,
    doc_type: str = "sop",
    machine_model: str = "",
    language: str = "zh",
) -> int:
    """
    摄入单个 docx。返回写入的 chunk 数。
    doc_type 建议值（与 WPS 目录映射对齐）：
      sop / fault_history / manual / checksheet
    """
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter

    path = Path(docx_path)
    print(f"\n📄 摄入 docx：{path.name}")

    text = docx_to_markdown(str(path))
    if not text or len(text) < 10:
        print("  ⚠️  未提取到文本，跳过")
        return 0

    vs, ctx, qdrant_client = _get_storage()
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

    rid = _doc_id(str(path))
    doc_name = path.name

    # 去重：先删旧数据
    _delete_existing(qdrant_client, collection, doc_name)
    _fts_delete_by_name(doc_name)

    parent_chunk_size = int(os.getenv("PARENT_CHUNK_SIZE", "1024"))
    sections = _split_by_headings(text, max_chunk=parent_chunk_size)

    docs = []
    for sec_title, sec_text in sections:
        if not sec_text.strip():
            continue
        title = sec_title or doc_name
        docs.append(Document(
            text=sec_text,
            metadata={
                "doc_name":      doc_name,
                "doc_type":      doc_type,
                "language":      language,
                "file_path":     str(path),
                "machine_model": machine_model,
                "doc_id":        rid,
                "chunk_hash":    _chunk_hash(sec_text),
                "section_title": title,
            },
        ))
        # FTS 同步（双写）
        _fts_sync(rid, doc_name, doc_type, machine_model, title, sec_text, str(path))

    if not docs:
        qdrant_client.close()
        return 0

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)
    qdrant_client.close()

    print(f"  ✅ {len(nodes)} chunks（{len(sections)} 章节）")
    return len(nodes)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("用法: python -m src.ingest_docx <file.docx> [doc_type] [machine_model]")
    ingest_docx(
        sys.argv[1],
        doc_type=sys.argv[2] if len(sys.argv) > 2 else "sop",
        machine_model=sys.argv[3] if len(sys.argv) > 3 else "",
    )
