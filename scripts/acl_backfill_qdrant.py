#!/usr/bin/env python3
"""scripts/acl_backfill_qdrant.py —— 把 doc_acl 回填进 Qdrant payload + 建 payload 索引。

doc_acl(SQLite) 是密级真值来源，Qdrant payload 是其副本（方案 3.2）。
本脚本按 doc_name 把 sec_level/owner_scope/review_status/acl_users 写进每个 chunk 的
payload，并为过滤字段建 payload index（否则 Qdrant 过滤退化为全量扫描）。

    # 预览（默认，不写）
    QDRANT_URL=http://127.0.0.1:6333 python scripts/acl_backfill_qdrant.py
    # 落库
    QDRANT_URL=http://127.0.0.1:6333 python scripts/acl_backfill_qdrant.py --commit

依赖 doc_acl 已由 classify_docs.py 填好。未在 doc_acl 中的 doc_name 会被列出并（--commit 时）
写成 pending 兜底（不可检索），保证 fail-closed。
"""
import argparse
import os
import sqlite3
import sys

COLL = os.getenv("QDRANT_COLLECTION", "nikon_expert_v1")
INDEX_FIELDS = [
    ("sec_level", "integer"),
    ("owner_scope", "keyword"),
    ("shared_scopes", "keyword"),
    ("acl_users", "keyword"),
    ("review_status", "keyword"),
]


def _client():
    from qdrant_client import QdrantClient
    url = os.getenv("QDRANT_URL")
    if url:
        return QdrantClient(url=url)
    return QdrantClient(path=os.getenv("QDRANT_PATH", "./data/qdrant_db"))


def _acl_by_name(conn):
    """doc_name → {sec_level, owner_scope, review_status, acl_users}。
    同名多 doc_id 若定级不一致，取最严（最高密级、pending 优先）。"""
    rows = conn.execute(
        "SELECT doc_id, filename, sec_level, owner_scope, review_status FROM doc_acl"
    ).fetchall()
    acl_users = {}
    for did, uid in conn.execute("SELECT doc_id, uid FROM doc_acl_users").fetchall():
        acl_users.setdefault(did, []).append(uid)
    out = {}
    for did, name, lvl, scope, rev in rows:
        if not name:
            continue
        cur = out.get(name)
        cand = {"sec_level": lvl, "owner_scope": scope, "review_status": rev,
                "acl_users": sorted(set(acl_users.get(did, [])))}
        if cur is None:
            out[name] = cand
        else:
            # 取最严：密级高者胜；同级 pending 胜
            if cand["sec_level"] > cur["sec_level"] or (
                cand["sec_level"] == cur["sec_level"] and cand["review_status"] != "approved"):
                out[name] = cand
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写入 Qdrant（默认仅预览）")
    args = ap.parse_args()

    from qdrant_client import models as qm

    db = os.getenv("FTS_DB_PATH", "./data/fts.db")
    conn = sqlite3.connect(db)
    acl_map = _acl_by_name(conn)
    if not acl_map:
        print("✗ doc_acl 为空，请先跑 classify_docs.py --commit")
        return 1
    client = _client()

    # Qdrant 里实际存在的 doc_name（用 scroll 聚合）
    present = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLL, with_payload=["doc_name"], with_vectors=False,
            limit=1000, offset=offset,
        )
        for p in points:
            nm = (p.payload or {}).get("doc_name")
            if nm:
                present[nm] = present.get(nm, 0) + 1
        if offset is None:
            break

    matched = [n for n in present if n in acl_map]
    unmatched = [n for n in present if n not in acl_map]
    total_pts = sum(present.values())
    print(f"集合 {COLL}: {total_pts} 点 / {len(present)} 个 doc_name")
    print(f"  可定级: {len(matched)} 个 doc_name")
    print(f"  未在 doc_acl（将兜底 pending）: {len(unmatched)} 个")
    for n in unmatched[:20]:
        print(f"    · {n} ({present[n]} 点)")

    if not args.commit:
        # 预览按密级/域统计
        from collections import Counter
        c = Counter()
        for n in matched:
            a = acl_map[n]
            c[(a["owner_scope"], a["sec_level"], a["review_status"])] += present[n]
        print("\n将写入的 payload 分布（点数）：")
        for (scope, lvl, rev), k in sorted(c.items(), key=lambda x: -x[1]):
            print(f"  {scope:<20} L{lvl} {rev:<9} {k:>6}")
        print("\n预览完成，加 --commit 落库并建索引")
        return 0

    # 建 payload 索引（幂等，已存在会抛错，忽略）
    for field, schema in INDEX_FIELDS:
        try:
            client.create_payload_index(COLL, field_name=field, field_schema=schema)
            print(f"  ✓ index {field}")
        except Exception as e:
            print(f"  · index {field} 跳过（{str(e)[:40]}）")

    # 按 doc_name 批量 set_payload
    n_docs = 0
    for name, a in acl_map.items():
        if name not in present:
            continue
        client.set_payload(
            collection_name=COLL,
            payload={"sec_level": a["sec_level"], "owner_scope": a["owner_scope"],
                     "review_status": a["review_status"], "acl_users": a["acl_users"]},
            points=qm.Filter(must=[qm.FieldCondition(
                key="doc_name", match=qm.MatchValue(value=name))]),
        )
        n_docs += 1
    # 未定级的兜底 pending（fail-closed）
    for name in unmatched:
        client.set_payload(
            collection_name=COLL,
            payload={"sec_level": 4, "owner_scope": "internal",
                     "review_status": "pending", "acl_users": []},
            points=qm.Filter(must=[qm.FieldCondition(
                key="doc_name", match=qm.MatchValue(value=name))]),
        )
    print(f"\n✓ 已写 {n_docs} 个 doc_name 的 payload + {len(unmatched)} 个兜底 pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
