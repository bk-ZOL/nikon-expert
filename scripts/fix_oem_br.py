#!/usr/bin/env python3
"""一次性修复：抽取条目里 <br> 连着的故障代码拆成空格分隔，正文里的 <br> 换成空格。

背景：X6 エラー要因分析 这类日文资料是从 Excel 转的 md，单元格换行是 <br>，
gemma4 把一整列细分码连 <br> 原样抄进了 error_code（190 条），正文里也带了 24 条。
幂等，可以重复跑（只动含 <br> 的字段，其他字段不碰）。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from src import ts_table
from src.kb_extract import _clean_codes, _clean_text

DB = os.getenv("TS_DB_PATH", os.path.join(ROOT, "data/ts_table.db"))
c = sqlite3.connect(DB)
rows = c.execute("select id, error_code, title, root_causes, solutions, oem_detail, follow_up, notes "
                 "from ts_entries").fetchall()
n = n_code = 0
for rid, code, title, rc, sol, detail, follow, notes in rows:
    sets = {}
    if code and "<br" in code.lower():
        sets["error_code"] = _clean_codes(code)
        sets["match_key"] = ts_table.match_key(sets["error_code"])
        n_code += 1
    for col, val in (("title", title), ("root_causes", rc), ("solutions", sol),
                     ("oem_detail", detail), ("follow_up", follow), ("notes", notes)):
        if val and "<br" in val.lower():
            sets[col] = _clean_text(val)
    if sets:
        cols = ",".join(f"{k}=?" for k in sets)
        c.execute(f"update ts_entries set {cols} where id=?", (*sets.values(), rid))
        n += 1
c.commit()
print(f"共 {len(rows)} 条，修复 {n} 条（其中故障代码字段 {n_code} 条）")
