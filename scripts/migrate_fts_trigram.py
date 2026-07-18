#!/usr/bin/env python3
"""
一次性迁移：把 fts.db 的分词器从 unicode61 换成 trigram。
- unicode61 把连续汉字当成单个 token，中文关键词搜索基本失效
- trigram 支持中文子串匹配（查询词需 >= 3 字符，短词由 fulltext.py 的 LIKE 降级兜底）

用法：
    python scripts/migrate_fts_trigram.py            # 默认迁移 ./data/fts.db
    python scripts/migrate_fts_trigram.py /path/to/fts.db

会先备份为 fts.db.bak-<时间戳>，失败不影响原库。
需要 SQLite >= 3.34（python3.9+ 自带的一般都满足）。
"""
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data/fts.db")

if not DB.exists():
    sys.exit(f"找不到 {DB}，请在 nikon-expert 根目录运行，或传入路径")

ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
if ver < (3, 34, 0):
    sys.exit(f"SQLite {sqlite3.sqlite_version} 太旧，trigram 需要 >= 3.34")

backup = DB.with_name(f"{DB.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(DB, backup)
print(f"已备份 -> {backup}")

conn = sqlite3.connect(DB)
try:
    old_count = conn.execute("SELECT count() FROM doc_fts").fetchone()[0]

    # 检查是否已经是 trigram
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='doc_fts'"
    ).fetchone()[0]
    if "trigram" in sql:
        sys.exit("doc_fts 已经是 trigram，无需迁移")

    conn.execute("""
        CREATE VIRTUAL TABLE doc_fts_new USING fts5(
            doc_id, doc_name, doc_type, machine_model, section_title, text,
            tokenize='trigram'
        )
    """)
    conn.execute("""
        INSERT INTO doc_fts_new
        SELECT doc_id, doc_name, doc_type, machine_model, section_title, text
        FROM doc_fts
    """)
    conn.execute("DROP TABLE doc_fts")
    conn.execute("ALTER TABLE doc_fts_new RENAME TO doc_fts")
    conn.commit()

    new_count = conn.execute("SELECT count() FROM doc_fts").fetchone()[0]
    assert new_count == old_count, f"行数不一致 {old_count} -> {new_count}"
    print(f"迁移完成：{new_count} 条记录")

    # 冒烟测试：随便抓一条中文记录，取中间 4 个字搜一下
    row = conn.execute(
        "SELECT text FROM doc_fts WHERE length(text) > 20 LIMIT 1"
    ).fetchone()
    if row:
        probe = row[0].strip()[5:9]
        if len(probe) >= 3:
            hits = conn.execute(
                "SELECT count() FROM doc_fts WHERE doc_fts MATCH ?",
                (f'"{probe}"',),
            ).fetchone()[0]
            print(f"冒烟测试：搜「{probe}」命中 {hits} 条（>0 即正常）")
finally:
    conn.close()
