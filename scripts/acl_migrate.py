#!/usr/bin/env python3
"""scripts/acl_migrate.py —— 建 ACL 相关表（方案第 3.2 章），幂等。

在 FTS 同库(data/fts.db)建：doc_acl / doc_acl_users / users / user_scopes。
不改任何既有表、不动数据，可反复运行。

    python scripts/acl_migrate.py                 # 用 FTS_DB_PATH 或 ./data/fts.db
    FTS_DB_PATH=/opt/nikon-expert/data/fts.db python scripts/acl_migrate.py
"""
import os
import sqlite3
import sys

DDL = [
    # 文档级 ACL 主表：FTS 过滤依赖它，同时作为 Qdrant payload 的真值来源
    """
    CREATE TABLE IF NOT EXISTS doc_acl (
        doc_id        TEXT PRIMARY KEY,
        filename      TEXT NOT NULL DEFAULT '',
        sec_level     INTEGER NOT NULL DEFAULT 4,          -- 兜底 L4
        owner_scope   TEXT    NOT NULL DEFAULT 'internal',
        review_status TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
        source_class  TEXT,
        classified_by TEXT,
        classified_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_acl_lookup ON doc_acl(review_status, sec_level, owner_scope)",
    # L4 显式白名单
    """
    CREATE TABLE IF NOT EXISTS doc_acl_users (
        doc_id TEXT NOT NULL,
        uid    TEXT NOT NULL,
        PRIMARY KEY (doc_id, uid)
    )
    """,
    # 用户
    """
    CREATE TABLE IF NOT EXISTS users (
        uid          TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL,
        clearance    INTEGER NOT NULL,
        pw_hash      TEXT,                                  -- Gradio 登录用（P0 简化：sha256）
        status       TEXT NOT NULL DEFAULT 'active',        -- active/suspended/left
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 归属域授权（valid_until 强制非空、到期自动失效）
    """
    CREATE TABLE IF NOT EXISTS user_scopes (
        uid         TEXT NOT NULL,
        scope       TEXT NOT NULL,
        granted_by  TEXT NOT NULL DEFAULT '',
        granted_at  TEXT NOT NULL DEFAULT (datetime('now')),
        valid_until TEXT NOT NULL,
        PRIMARY KEY (uid, scope)
    )
    """,
]


def main() -> int:
    db_path = os.getenv("FTS_DB_PATH", "./data/fts.db")
    if not os.path.exists(db_path):
        print(f"✗ FTS 库不存在: {db_path}")
        return 1
    conn = sqlite3.connect(db_path)
    try:
        for stmt in DDL:
            conn.execute(stmt)
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('doc_acl','doc_acl_users','users','user_scopes')"
        ).fetchall()]
        print(f"✓ ACL 表已就绪 @ {db_path}")
        for t in ("doc_acl", "doc_acl_users", "users", "user_scopes"):
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            mark = "✓" if t in tables else "✗"
            print(f"  {mark} {t:<16} rows={n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
