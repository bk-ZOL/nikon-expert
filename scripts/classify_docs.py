#!/usr/bin/env python3
"""scripts/classify_docs.py —— 存量文档批量定级（方案第 6.1 规则 + 第 9 章底稿）。

定级规则复用 src/acl_classify.classify（与摄入侧增量定级同一份，避免漂移）。
  · L1–L3 自动判定 → approved（可检索）；L4（商务/客户）→ pending（待复核）——止血
  · 判不出 → pending L4 internal（不可检索，进待办）
默认 dry-run；--commit 写库。已人工定级(classified_by!='auto')默认不覆盖，--force 强制。

    python scripts/classify_docs.py            # 预览
    python scripts/classify_docs.py --commit   # 写入
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.acl_classify import classify  # noqa: E402


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写库（默认仅预览）")
    ap.add_argument("--force", action="store_true", help="覆盖已人工定级的行")
    args = ap.parse_args()

    db_path = os.getenv("FTS_DB_PATH", "./data/fts.db")
    conn = sqlite3.connect(db_path)
    docs = conn.execute("SELECT DISTINCT doc_id, doc_name, doc_type FROM doc_meta").fetchall()
    if not docs:
        print("doc_meta 为空")
        return 1

    manual = {r[0] for r in conn.execute(
        "SELECT doc_id FROM doc_acl WHERE classified_by IS NOT NULL AND classified_by != 'auto'"
    ).fetchall()} if _table_exists(conn, "doc_acl") else set()

    stats, by_scope_level = Counter(), Counter()
    written = skipped_manual = 0
    for doc_id, doc_name, doc_type in docs:
        if doc_id in manual and not args.force:
            skipped_manual += 1
            continue
        c = classify(doc_name, doc_type)
        stats[c["review_status"]] += 1
        by_scope_level[(c["owner_scope"], c["sec_level"], c["review_status"])] += 1
        if args.commit:
            conn.execute(
                """INSERT INTO doc_acl
                   (doc_id, filename, sec_level, owner_scope, review_status,
                    source_class, classified_by, classified_at)
                   VALUES (?,?,?,?,?,?, 'auto', datetime('now'))
                   ON CONFLICT(doc_id) DO UPDATE SET
                     filename=excluded.filename, sec_level=excluded.sec_level,
                     owner_scope=excluded.owner_scope, review_status=excluded.review_status,
                     source_class=excluded.source_class, classified_by='auto',
                     classified_at=datetime('now')""",
                (doc_id, doc_name, c["sec_level"], c["owner_scope"],
                 c["review_status"], c["source_class"]),
            )
            written += 1
    if args.commit:
        conn.commit()

    print(f"库: {db_path}   文档(doc_id): {len(docs)}   保护未覆盖(人工): {skipped_manual}")
    print(f"review_status 分布: {dict(stats)}")
    print("\n归属域 / 密级 / 状态  →  doc_id 数：")
    for (scope, lvl, rev), n in sorted(by_scope_level.items(), key=lambda x: -x[1]):
        print(f"  {scope:<20} L{lvl}  {rev:<9} {n:>6}")
    print(f"\n{'已写入 ' + str(written) + ' 行' if args.commit else '预览(未写库)，加 --commit 落库'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
