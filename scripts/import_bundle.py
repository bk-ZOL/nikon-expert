#!/usr/bin/env python3
"""scripts/import_bundle.py —— 服务器端秒导入 embed_bundle 产出的 .nebundle（不重嵌）。

把本机(Mac)嵌好的向量点 + FTS 行直接 upsert 到云端 Qdrant + 写 FTS + 自动定级。
幂等：先按 doc_name 删旧再写。

    QDRANT_URL=http://127.0.0.1:6333 python scripts/import_bundle.py xxx.nebundle [yyy.nebundle ...]
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _import_one(path: str) -> int:
    with open(path, "r", encoding="utf-8") as f:
        b = json.load(f)
    doc_name = b["doc_name"]
    coll = b.get("collection") or os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    points = b.get("qdrant_points", [])
    fts_rows = b.get("fts_rows", [])
    meta_rows = b.get("meta_rows", [])
    print(f"→ {doc_name}: {len(points)} 点 / {len(fts_rows)} FTS 行")

    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
    url = os.getenv("QDRANT_URL")
    if not url:
        print("   ✗ 未设 QDRANT_URL（生产是 http://127.0.0.1:6333）"); return 1

    from qdrant_client import QdrantClient, models as qm
    qc = QdrantClient(url=url)

    # 幂等：先删旧点
    qc.delete(coll, points_selector=qm.FilterSelector(filter=qm.Filter(
        must=[qm.FieldCondition(key="doc_name", match=qm.MatchValue(value=doc_name))])))
    # upsert 向量点
    ps = [qm.PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"]) for p in points]
    for i in range(0, len(ps), 256):
        qc.upsert(coll, points=ps[i:i + 256])
    qc.close()

    # FTS：先删旧再写
    from src.fulltext import init_fts, delete_doc_fts_by_name, ingest_fts
    init_fts()
    delete_doc_fts_by_name(doc_name)
    meta_by_id = {m[0]: m for m in meta_rows}   # doc_id -> [doc_id,doc_name,doc_type,file_path,machine_model]
    for row in fts_rows:
        doc_id, dn, dt, mm, sec, text = row
        fp = meta_by_id.get(doc_id, [None, None, None, "", None])[3] or ""
        ingest_fts(doc_id, dn, dt, mm, sec, text, fp)

    # 自动定级（写 doc_acl + 回填 Qdrant payload）
    try:
        from src.acl_classify import apply_doc_acl
        apply_doc_acl(doc_name)
    except Exception as e:
        print(f"   ⚠️ 定级失败（可事后 classify_docs 补）：{e}")

    print(f"   ✓ 已导入 {doc_name}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python scripts/import_bundle.py <bundle.nebundle> [...]")
        return 1
    rc = 0
    for p in sys.argv[1:]:
        if not os.path.isfile(p):
            print(f"✗ 找不到：{p}"); rc |= 1; continue
        rc |= _import_one(p)
    return rc


if __name__ == "__main__":
    sys.exit(main())
