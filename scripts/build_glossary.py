#!/usr/bin/env python3
"""重建缩写对照表（src/glossary.py）：从全文索引挖「全称 (缩写)」和对照表格，合并 config/glossary.tsv。

  .venv/bin/python scripts/build_glossary.py              # 重建 data/glossary.json
  .venv/bin/python scripts/build_glossary.py --show RR     # 看某个缩写
  .venv/bin/python scripts/build_glossary.py --missing     # 自编 SOP / 履历 / 飞书文档里常出现、但对照表里没有的缩写（待人工补）
WPS / 飞书同步有资料变化时会自动重建。改 config/glossary.tsv 不用重建，下次提问就生效。
"""
import argparse
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from src import glossary

ap = argparse.ArgumentParser()
ap.add_argument("--show", default=None)
ap.add_argument("--missing", action="store_true")
a = ap.parse_args()

if a.show:
    for ab, senses in glossary.lookup(a.show.upper()):
        print(ab)
        for s in senses:
            print("  ", s)
elif a.missing:
    try:
        import pysqlite3 as sqlite3
    except ImportError:
        import sqlite3
    from src.fulltext import _db_path
    c = sqlite3.connect(_db_path)
    t = glossary.table()
    cnt, where = Counter(), Counter()
    for name, text in c.execute("select doc_name, text from doc_fts where doc_type in ('sop', 'feishu_worklog', 'fault_history')"):
        for tok in re.findall(r"(?<![A-Za-z0-9\-$])([A-Z][A-Z0-9]{1,5})(?![A-Za-z0-9])", text or ""):
            if tok not in t and tok not in glossary._SKIP and not re.fullmatch(r"[A-Z]\d+|\d+", tok):
                cnt[tok] += 1
                where[(tok, name)] += 1
    for tok, n in cnt.most_common(60):
        docs = [d for (k, d), _ in where.most_common() if k == tok][:2]
        print(f"{tok}\t{n}\t{' / '.join(docs)}")
else:
    print(glossary.build())
