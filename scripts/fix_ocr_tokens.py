"""纠正电路图 OCR 编号里"字母认成数字"的错误（J75448 → J7544B），只改全文索引里「【OCR标签】」后面那段。

依据：接插件号几乎都是 4 位数字 + 字母（2801A）或纯 4 位数字；5 位纯数字在线表里只有十几个。
OCR 用的 CAD 笔画字里，B/8、C/0、D/0、G/6、S/5、I/1、Z/2、A/4 形状很像，末位字母常被认成数字。
规则：OCR 的 J/P 号不在"已知号"里时，把末位数字换成形近字母，恰好只有一种换法能对上已知号才改；
已知号 = 线表（Claude 读图）+ 所有电路图文字层里的 J/P 号；还对不上时，再加上"格式正确、在两张以上不同的图上
都被 OCR 认出过"的号。有多种可能（2233C / 2233D）时，挑同一页文字、OCR、线表里出现过的那个。都定不下来的不改。

用法（在 /opt/nikon-expert 下）:
  .venv/bin/python scripts/fix_ocr_tokens.py          # 只预览
  .venv/bin/python scripts/fix_ocr_tokens.py --apply  # 写入；原 OCR 段先备份到 data/ocr_tokens_backup_<时间>.json
改完要重建编号索引：.venv/bin/python scripts/build_diagram_index.py --full
"""
import collections
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
try:
    import pysqlite3 as sq
except Exception:
    sq = sqlite3

APPLY = "--apply" in sys.argv
MARK = "【OCR标签】"
JP = re.compile(r"(?<![A-Za-z0-9])([JP])(\d{3,6}[A-Z]?\d?)(?![A-Za-z0-9])")
SUB = {"8": "B", "0": "CDO", "6": "G", "5": "S", "1": "I", "2": "Z", "4": "A"}

fts = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"), timeout=120)
known = set()
nl_db = os.getenv("NETLIST_DB", "./data/netlist.db")
if os.path.exists(nl_db):
    for f in sqlite3.connect(nl_db).execute("select a_conn, a_mate, b_conn, b_mate from edges"):
        for x in f:
            known |= {n for _, n in JP.findall((x or "").upper().replace(" ", ""))}
rows = fts.execute("select rowid, doc_name, text from doc_fts where doc_type='circuit_diagram'").fetchall()
for _, _, t in rows:
    known |= {n for _, n in JP.findall(t.partition(MARK)[0].upper())}
# 线表里每页的号（多种可能时用同页信息挑）
page_nl = collections.defaultdict(set)
if os.path.exists(nl_db):
    for d, pg, *f in sqlite3.connect(nl_db).execute("select doc_name, page, a_conn, a_mate, b_conn, b_mate from edges"):
        for x in f:
            page_nl[(d, pg)] |= {n for _, n in JP.findall((x or "").upper().replace(" ", ""))}
# 格式正确（4 位数字 + 可选字母/尾号）的 OCR 号，在两张以上不同的图上认出过的，也算已知
VALID = re.compile(r"\d{4}(?:[A-Z]\d?)?")
seen = collections.defaultdict(set)
for _, _, t in rows:
    head, _, ocr = t.partition(MARK)
    for _, n in JP.findall(ocr):
        if VALID.fullmatch(n):
            seen[n].add(hash(" ".join(head.split())))
consensus = {n for n, h in seen.items() if len(h) >= 2} - known
print(f"已知接插件号 {len(known)} 个（线表 + 电路图文字层），另有 OCR 在两张以上不同的图上认出过的 {len(consensus)} 个")


def fix(num: str, page_ctx: set):
    """返回纠正后的号，不用改返回 None。先看线表/文字层，再看 OCR 多图共识；多种可能时挑同页出现过的。"""
    if num in known or not num[-1].isdigit() or len(num) < 5:
        return None
    for pool in (known, known | consensus):
        cands = [num[:-1] + L for L in SUB.get(num[-1], "") if num[:-1] + L in pool]
        if len(cands) > 1:
            cands = [c for c in cands if c in page_ctx]
        if len(cands) == 1:
            return cands[0]
    return None


changes, per_doc, examples = {}, collections.Counter(), collections.Counter()
left5 = collections.Counter()
sec = {r[0]: r for r in fts.execute("select rowid, section_title from doc_fts where doc_type='circuit_diagram'")}
for rowid, doc, t in rows:
    if MARK not in t:
        continue
    head, _, ocr = t.partition(MARK)
    toks = ocr.split()
    pm = re.search(r"(\d+)", sec[rowid][1] or "")
    ctx = {n for _, n in JP.findall(t.upper())} | (page_nl.get((doc, int(pm.group(1))), set()) if pm else set())
    new, changed = [], False
    for tok in toks:
        m = JP.fullmatch(tok)
        if m:
            f = fix(m.group(2), ctx)
            if f:
                examples[(tok, m.group(1) + f)] += 1
                tok, changed = m.group(1) + f, True
                per_doc[doc] += 1
            elif re.fullmatch(r"\d{5}", m.group(2)) and m.group(2) not in known:
                left5[tok] += 1
        new.append(tok)
    if changed:
        changes[rowid] = (ocr, " ".join(sorted(set(new))), head)

print(f"要改的页 {len(changes)}，改掉的号 {sum(per_doc.values())} 个：")
for d, n in per_doc.most_common():
    print(f"  {n:5d}  {d[:70]}")
print("例：", [f"{a}→{b}×{n}" for (a, b), n in examples.most_common(12)])
print(f"改不了、仍是 5 位纯数字的 {sum(left5.values())} 个（对不上已知号或有多种可能），例：", left5.most_common(8))

if not APPLY:
    print("\n（预览模式，没写入。确认无误后加 --apply）")
    sys.exit(0)
backup = Path("data") / f"ocr_tokens_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
backup.write_text(json.dumps({str(r): v[0] for r, v in changes.items()}, ensure_ascii=False))
print(f"原 OCR 段已备份：{backup}")
for rowid, (_, new, head) in changes.items():
    fts.execute("update doc_fts set text=? where rowid=?", (head + MARK + new, rowid))
fts.commit()
print(f"完成：改了 {len(changes)} 页。记得重建编号索引：.venv/bin/python scripts/build_diagram_index.py --full")
