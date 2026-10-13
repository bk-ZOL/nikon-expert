"""用矢量字形识读电路图上的 CAD 笔画字（src/vector_glyph.py），结果合并进全文索引。

两步，读图在 Mac 上跑（服务器只有 2 核，慢 20 倍以上），合并在服务器上做：
  1) Mac：读整本 PDF，输出每页的编号
     python3 scripts/vector_glyph_read.py read --pdf ~/Downloads/<图册>.pdf --out /tmp/vg.json [--workers 8] [--pages 70,2853]
     重复页按低分辨率渲染指纹只读一次。
  2) 服务器：把编号合并进全文索引每页的「【OCR标签】」段（与 OCR 结果取并集）
     scp /tmp/vg.json root@<服务器>:/tmp/ && .venv/bin/python scripts/vector_glyph_read.py apply --json /tmp/vg.json --doc 已融合 [--dry]
     原 OCR 段先备份到 data/vector_glyph_backup_<时间>.json；合并后重建编号索引：
     .venv/bin/python scripts/build_diagram_index.py --full
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OCR_MARK = "【OCR标签】"
_PDF = None
_READER = None


def _init(pdf):
    global _PDF, _READER
    import fitz
    from src.vector_glyph import Reader
    _PDF = fitz.open(pdf)
    _READER = Reader()


def _fingerprint(p):
    import fitz
    return p, hashlib.md5(_PDF[p - 1].get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples).hexdigest()


def _read(p):
    return p, sorted(_READER.page_tokens(_PDF[p - 1]))


def cmd_read(a):
    import fitz
    n = fitz.open(a.pdf).page_count
    pages = [int(x) for x in a.pages.split(",")] if a.pages else list(range(1, n + 1))
    t0 = time.time()
    with ProcessPoolExecutor(a.workers, initializer=_init, initargs=(a.pdf,)) as ex:
        fps = dict(ex.map(_fingerprint, pages, chunksize=16))
        first = {}
        for p in pages:
            first.setdefault(fps[p], p)
        uniq = sorted(set(first.values()))
        print(f"{len(pages)} 页，去重后 {len(uniq)} 页，开始识读…", flush=True)
        got = {}
        for i, (p, toks) in enumerate(ex.map(_read, uniq, chunksize=4), 1):
            got[p] = toks
            if i % 200 == 0:
                print(f"  {i}/{len(uniq)}，已用 {time.time() - t0:.0f}s", flush=True)
    out = {"pdf": Path(a.pdf).name, "pages": {str(p): got[first[fps[p]]] for p in pages}}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False))
    print(f"完成：{len(pages)} 页，编号 {sum(len(v) for v in out['pages'].values())} 个，用时 {time.time() - t0:.0f}s → {a.out}")


def cmd_apply(a):
    from dotenv import load_dotenv
    load_dotenv(".env")
    try:
        import pysqlite3 as sq
    except Exception:
        import sqlite3 as sq
    data = json.loads(Path(a.json).read_text())
    con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"), timeout=120)
    names = [r[0] for r in con.execute("select distinct doc_name from doc_fts where doc_type='circuit_diagram' "
                                       "and doc_name like ?", (f"%{a.doc}%",))]
    if len(names) != 1:
        sys.exit(f"--doc 要恰好对上一份电路图，现在对上 {len(names)} 份：{names}")
    if names[0] != data["pdf"]:
        sys.exit(f"JSON 是 {data['pdf']} 的结果，和 {names[0]} 对不上")
    rows = {}
    for rowid, title, text in con.execute("select rowid, section_title, text from doc_fts "
                                          "where doc_type='circuit_diagram' and doc_name=?", (names[0],)):
        m = re.search(r"(\d+)", title or "")
        if m:
            rows[int(m.group(1))] = (rowid, text or "")
    changes, added = {}, 0
    for p, (rowid, text) in rows.items():
        vt = set(data["pages"].get(str(p), []))
        head, mark, ocr = text.partition(OCR_MARK)
        old = set(ocr.split())
        new = old | vt
        if new != old:
            changes[rowid] = (ocr if mark else None, head.rstrip("\n") + "\n" + OCR_MARK + " ".join(sorted(new)))
            added += len(new - old)
    print(f"{names[0]}：全文索引 {len(rows)} 页，要更新 {len(changes)} 页，新增编号 {added} 个")
    if a.dry:
        print("（试跑，没写入）")
        return
    backup = Path("data") / f"vector_glyph_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup.write_text(json.dumps({str(r): v[0] for r, v in changes.items()}, ensure_ascii=False))
    for rowid, (_, new_text) in changes.items():
        con.execute("update doc_fts set text=? where rowid=?", (new_text, rowid))
    con.commit()
    print(f"已写入。原 OCR 段备份在 {backup}（值为 null 的页原来没有 OCR 段）。"
          f"记得重建编号索引：.venv/bin/python scripts/build_diagram_index.py --full")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("--pdf", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--pages", default="")
    ap_ = sub.add_parser("apply")
    ap_.add_argument("--json", required=True)
    ap_.add_argument("--doc", required=True)
    ap_.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    a.pdf = os.path.expanduser(getattr(a, "pdf", "") or "")
    {"read": cmd_read, "apply": cmd_apply}[a.cmd](a)
