# src/ingest_worklog.py
# 飞书工作日志 -> 向量库 + FTS
# 复用 src/ingestor 的存储/去重/FTS 助手，切块粒度=每天一条日报。

import os
import hashlib

from src.ingestor import (
    _get_storage, _delete_existing, _fts_sync, _fts_delete_by_name, _chunk_hash,
)


def _rid(doc_id: str) -> str:
    return hashlib.sha256(doc_id.encode()).hexdigest()[:12]


def ingest_worklog_days(days, doc_name, doc_id, wiki_node="", last_edited="",
                        machine_model="", storage=None) -> int:
    """把已解析的日志（[{date,text}]）灌进向量库 + FTS。

    切块=每天一条；metadata 打 source=feishu-worklog + feishu_doc_id + date + last_edited。
    去重键=doc_name：整篇先删旧再全量重灌，避免日志被编辑后产生重复向量。
    storage 传入 (vs,ctx,client) 则复用（app 内调用）；否则自建并在结束时关闭（CLI）。
    """
    from llama_index.core import VectorStoreIndex, Document
    from llama_index.core.node_parser import SentenceSplitter

    own = storage is None
    vs, ctx, client = _get_storage() if own else storage
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    rid = _rid(doc_id)

    print(f"\n📓 摄入飞书工作日志：{doc_name}（{len(days)} 天）")

    # 去重：整篇先删（向量 + FTS）
    _delete_existing(client, collection, doc_name)
    _fts_delete_by_name(doc_name)

    docs = []
    for d in days:
        date, text = d["date"], d["text"]
        if not text.strip():
            continue
        sec = f"工作日志 {date}"
        meta = {
            "doc_name":      doc_name,
            "doc_type":      "feishu_worklog",
            "language":      "zh",
            "source":        "feishu-worklog",
            "feishu_doc_id": doc_id,
            "wiki_node":     wiki_node,
            "date":          date,
            "section_title": sec,
            "doc_id":        rid,
            "chunk_hash":    _chunk_hash(text),
        }
        if machine_model:
            meta["machine_model"] = machine_model
        if last_edited:
            meta["last_edited"] = last_edited
        docs.append(Document(text=text, metadata=meta))
        # FTS 同步（每天一条，section_title 存日期，便于 nikon_search 关键词命中）
        _fts_sync(f"{rid}_{date}", doc_name, "feishu_worklog", machine_model, sec, text)

    if not docs:
        if own:
            client.close()
        print("  ⚠️  没有可摄入的日报")
        return 0

    # 每天通常几百字：短的整体保留，长的按句子切子块
    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs, show_progress=False)
    VectorStoreIndex(nodes, storage_context=ctx, show_progress=False)

    if own:
        client.close()

    # 自动定级 + 写 Qdrant ACL payload（工作日志→internal L3，日志即时可查/受权限）
    try:
        from src.acl_classify import apply_doc_acl
        apply_doc_acl(doc_name)
    except Exception:
        pass

    print(f"  ✅ 摄入完成，共 {len(nodes)} 个 Chunk（{len(docs)} 天）")
    return len(nodes)
