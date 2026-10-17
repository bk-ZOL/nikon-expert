"""电路图识图后台任务：给新入库 / 重新入库的电路图补上 CAD 笔画字编号（接插件号、针脚号等）。

电路图入库（ingestor.ingest_circuit_pdf）时只取文字层（.env 里 CIRCUIT_OCR=0），不在同步里做慢的 OCR；
识图交给这个任务，按入库时间判断哪些图还没做，依次：
  1. OCR（scripts/ocr_circuit_pages.py：300 dpi，270°+0° 两种模式，按图去重）；
  2. 矢量字形识读（scripts/vector_glyph_read.py read → apply，与 OCR 结果取并集）；
  3. 纠正字母认成数字（scripts/fix_ocr_tokens.py --apply）；
  4. 重建编号索引（scripts/build_diagram_index.py --full）。
同步入库了新电路图会立刻启动本任务（systemctl start --no-block nikon-circuit-recognize.service），
另有定时器每天兜底跑一次。同步在跑时先等它结束；同一时间只跑一个。

用法（在 /opt/nikon-expert 下）：
  .venv/bin/python scripts/circuit_recognize.py              # 处理所有还没识图的电路图
  .venv/bin/python scripts/circuit_recognize.py --list       # 只列出状态
  .venv/bin/python scripts/circuit_recognize.py --mark-done  # 把现有的都记为已完成（不处理）
  .venv/bin/python scripts/circuit_recognize.py --no-ocr     # 只做矢量识读（快，OCR 每页约 35 秒）
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

STATE = ROOT / "data" / "circuit_recognize_state.json"
PY = sys.executable


def _docs():
    try:
        import pysqlite3 as sq
    except Exception:
        import sqlite3 as sq
    con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
    rows = con.execute("select doc_name, max(ingested_at) from doc_meta where doc_type='circuit_diagram' "
                       "group by doc_name").fetchall()
    con.close()
    return dict(rows)


def _state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def _run(cmd, log):
    print(f"  $ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip() and "Warning" not in l and "MuPDF" not in l]
    for l in lines[-6:]:
        print("    " + l, flush=True)
    log.extend(lines[-20:])
    if r.returncode:
        raise RuntimeError(f"命令失败（{r.returncode}）：{' '.join(cmd)}")


def _wait_sync():
    while subprocess.run(["systemctl", "is-active", "--quiet", "nikon-wps-sync.service"]).returncode == 0:
        print("  同步正在跑，等它结束…", flush=True)
        time.sleep(60)


def process(doc_name, do_ocr=True):
    from src.figures import _locate_pdf
    pdf = _locate_pdf(doc_name)
    if not pdf:
        raise RuntimeError(f"找不到 PDF：{doc_name}")
    log = []
    if do_ocr:
        _wait_sync()
        _run([PY, "scripts/ocr_circuit_pages.py", "--doc", doc_name], log)
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "vg.json")
        _run([PY, "scripts/vector_glyph_read.py", "read", "--pdf", pdf, "--out", out, "--workers", "2"], log)
        _wait_sync()
        _run([PY, "scripts/vector_glyph_read.py", "apply", "--json", out, "--doc", doc_name], log)
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mark-done", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    a = ap.parse_args()
    docs, st = _docs(), _state()
    todo = [d for d, t in docs.items() if st.get(d, {}).get("ingested_at") != t]
    if a.list:
        for d, t in sorted(docs.items()):
            s = st.get(d, {})
            print(f"{'待识图' if d in todo else '已完成'}  入库 {t}  识图 {s.get('done_at', '-')}  {d}")
        return
    if a.mark_done:
        for d in todo:
            st[d] = {"ingested_at": docs[d], "done_at": time.strftime("%F %T"), "note": "手工标记"}
        _save(st)
        print(f"已把 {len(todo)} 份记为已完成")
        return
    lock = open(ROOT / "data" / ".circuit_recognize.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("另一个识图任务正在跑，这次跳过")
    if not todo:
        print("没有待识图的电路图")
        return
    done = 0
    for d in todo:
        print(f"[{time.strftime('%F %T')}] 识图：{d}", flush=True)
        try:
            process(d, do_ocr=not a.no_ocr)
            st = _state()
            st[d] = {"ingested_at": docs[d], "done_at": time.strftime("%F %T")}
            _save(st)
            done += 1
        except Exception as e:
            print(f"  ⚠️ 失败：{e}", flush=True)
    if done:
        log = []
        _wait_sync()
        _run([PY, "scripts/fix_ocr_tokens.py", "--apply"], log)
        _run([PY, "scripts/build_diagram_index.py", "--full"], log)
    print(f"[{time.strftime('%F %T')}] 完成 {done}/{len(todo)} 份", flush=True)


if __name__ == "__main__":
    main()
