"""给已入库的电路图补 OCR 编号：不重新入库，只往全文索引里每页文字后面追加「【OCR标签】…」。

电路图上的接插件号、针脚号多是 CAD 笔画字，不在 PDF 文字层里。入库时的 OCR（ingestor._ocr_page_tokens）
只按 270° 认竖排字；S207D 已融合版有不少横排标注（如第 70 页的 P7544E/J7544E），所以这里默认做三遍：
270° psm 11（竖排）、0° psm 11 + 0° psm 6（横排，两种模式各认出一部分）。实测 300 dpi 比 400 dpi 认得还多、还快。

- 已融合版里同一张图重复出现好几次：按低分辨率渲染图的指纹去重，重复页直接复用结果。
- 进度存在 data/ocr_progress_<图册指纹>.json，可随时停（kill）、再跑会接着做；已带「【OCR标签】」的页跳过。
- 每做完一页就写进全文索引，边跑边生效。建议低优先级后台跑：
    nohup nice -n 19 .venv/bin/python scripts/ocr_circuit_pages.py --doc 已融合 > /tmp/ocr_s207d.log 2>&1 &
- 跑完后重建电路图编号索引：.venv/bin/python scripts/build_diagram_index.py --full

用法：
  .venv/bin/python scripts/ocr_circuit_pages.py --doc 已融合 [--limit 20] [--pages 70,2853] [--dpi 300]
      [--passes 270:11,0:11,0:6] [--dry]
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")

try:
    import pysqlite3 as sq
except Exception:
    import sqlite3 as sq

OCR_MARK = "【OCR标签】"

ap = argparse.ArgumentParser()
ap.add_argument("--doc", required=True, help="图册名关键字（全文索引 doc_name 里的子串）")
ap.add_argument("--dpi", type=int, default=300)
ap.add_argument("--passes", default="270:11,0:11,0:6", help="旋转角度:psm，逗号分隔")
ap.add_argument("--limit", type=int, default=0, help="最多做多少页（试跑用）")
ap.add_argument("--pages", default="", help="只做这些页，逗号分隔")
ap.add_argument("--dry", action="store_true", help="只识别、打印，不写库")
args = ap.parse_args()
PASSES = [tuple(int(x) for x in p.split(":")) for p in args.passes.split(",") if p.strip()]

from src import diagram_config as C
from src.figures import _locate_pdf
import fitz
from PIL import Image

con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"), timeout=120)
names = [r[0] for r in con.execute(
    "select distinct doc_name from doc_fts where doc_type='circuit_diagram' and doc_name like ?", (f"%{args.doc}%",))]
if len(names) != 1:
    sys.exit(f"--doc 要恰好对上一份电路图，现在对上 {len(names)} 份：{names}")
doc_name = names[0]
pdf = _locate_pdf(doc_name)
if not pdf:
    sys.exit(f"找不到 PDF：{doc_name}")

rows = {}
for rowid, title, text in con.execute(
        "select rowid, section_title, text from doc_fts where doc_type='circuit_diagram' and doc_name=?", (doc_name,)):
    m = re.search(r"(\d+)", title or "")
    if m:
        rows[int(m.group(1))] = (rowid, text or "")
todo = sorted(p for p, (_, t) in rows.items() if OCR_MARK not in t)
if args.pages:
    want = {int(x) for x in args.pages.split(",") if x.strip()}
    todo = [p for p in todo if p in want]
if args.limit:
    todo = todo[:args.limit]

prog_file = Path("data") / f"ocr_progress_{hashlib.md5(doc_name.encode()).hexdigest()[:8]}.json"
prog = json.loads(prog_file.read_text()) if prog_file.exists() else {"doc": doc_name, "by_hash": {}}
print(f"图册：{doc_name}\n全文索引里 {len(rows)} 页，还没 OCR 的 {len(todo)} 页；已缓存的图 {len(prog['by_hash'])} 张；"
      f"设置 {args.dpi} dpi，{args.passes}{'（试跑不写库）' if args.dry else ''}", flush=True)


def tokens_of(page) -> str:
    toks = set()
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "p.png"
        page.get_pixmap(dpi=args.dpi).save(str(png))
        rotated = {}
        for rot, psm in PASSES:
            f = png
            if rot % 360:
                if rot not in rotated:
                    rotated[rot] = Path(td) / f"r{rot}.png"
                    Image.open(png).rotate(rot, expand=True).save(rotated[rot])
                f = rotated[rot]
            out = subprocess.run(["tesseract", str(f), "-", "--psm", str(psm), "-l", "eng"],
                                 capture_output=True, text=True, timeout=600).stdout
            for _, pat in C.IDENTIFIER_PATTERNS:
                toks |= {m.upper() for m in pat.findall(out) if len(m) >= C.MIN_TOKEN_LEN}
    return " ".join(sorted(toks))


doc = fitz.open(pdf)
t0, done, reused = time.time(), 0, 0
for n, p in enumerate(todo, 1):
    page = doc[p - 1]
    fp = hashlib.md5(page.get_pixmap(dpi=40, colorspace=fitz.csGRAY).samples).hexdigest()
    if fp in prog["by_hash"]:
        toks = prog["by_hash"][fp]
        reused += 1
    else:
        toks = tokens_of(page)
        prog["by_hash"][fp] = toks
        done += 1
    if args.dry:
        print(f"  第 {p} 页：{toks[:300]}", flush=True)
        continue
    rowid, text = rows[p]
    con.execute("update doc_fts set text=? where rowid=?", (text + "\n" + OCR_MARK + toks, rowid))
    con.commit()
    if done and (done % 10 == 0 or n == len(todo)) or n == len(todo):
        prog_file.write_text(json.dumps(prog, ensure_ascii=False))
        el = time.time() - t0
        eta = el / max(done, 1) * (len(todo) - n) * (done / max(n, 1))
        print(f"  {n}/{len(todo)} 页（新识别 {done}，复用 {reused}），已用 {el/3600:.1f} 小时，"
              f"预计还要 {eta/3600:.1f} 小时", flush=True)
prog_file.write_text(json.dumps(prog, ensure_ascii=False))
print(f"完成：新识别 {done} 页，复用 {reused} 页，用时 {(time.time()-t0)/3600:.2f} 小时。"
      f"{'' if args.dry else '记得重建编号索引：.venv/bin/python scripts/build_diagram_index.py --full'}", flush=True)
