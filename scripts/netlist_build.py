"""线表批量抽取：配线图 → 线缆级连接，写入 data/netlist.db（可断点续跑）。

  wiring 页         → Qwen3.5-122B（硅基流动，国内）
  general_wiring 页 → Claude Sonnet（Message Batches，半价）

用法:
  .venv/bin/python scripts/netlist_build.py plan              # 各类页数、待抽页数
  .venv/bin/python scripts/netlist_build.py qwen --workers 3  # 抽 wiring 页
  .venv/bin/python scripts/netlist_build.py claude-submit     # 提交 general_wiring 批次
  .venv/bin/python scripts/netlist_build.py claude-collect    # 轮询批次，完成后入库
  .venv/bin/python scripts/netlist_build.py stats             # 结果统计
"""
import argparse, hashlib, json, os, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dotenv import load_dotenv; load_dotenv(".env")
import fitz, httpx
import netlist_pilot as N

DOCS = ["S207DS307E Electrical Circuit Diagram", "NSR-S207D S307E Reticle SMIF Loader"]
QWEN = os.getenv("NETLIST_QWEN_MODEL", "Qwen/Qwen3.5-122B-A10B")
CLAUDE = os.getenv("NETLIST_CLAUDE_MODEL", "claude-sonnet-5-5")
DB = Path("data/netlist.db")
BATCH_FILE = Path("data/netlist_batches.json")
AKEY = os.getenv("ANTHROPIC_API_KEY", "")
AHDR = {"x-api-key": AKEY, "anthropic-version": "2023-06-01"}
NOW = lambda: time.strftime("%F %T")


def db():
    con = sqlite3.connect(DB, timeout=60)
    con.executescript("""
    create table if not exists pages(doc_name text, page int, kind text, model text, status text,
        figure_title text, raw text, tok_in int, tok_out int, updated_at text,
        primary key(doc_name, page));
    create table if not exists edges(doc_name text, page int, figure_title text, cable text,
        a_conn text, a_mate text, a_device text, a_device_no text, a_ref text,
        b_conn text, b_mate text, b_device text, b_device_no text, b_ref text,
        wires text, note text, model text);
    create index if not exists edges_doc_page on edges(doc_name, page);
    create index if not exists edges_a on edges(a_conn);
    create index if not exists edges_b on edges(b_conn);
    create index if not exists edges_cable on edges(cable);
    """)
    return con


def fts():
    try:
        import pysqlite3 as sq
    except Exception:
        import sqlite3 as sq
    return sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))


def targets():
    c, out = fts(), []
    for kw in DOCS:
        out += c.execute("select doc_name, page, kind from diagram_pages where doc_name like ? "
                         "and kind in ('wiring','general_wiring') order by doc_name, page",
                         (f"%{kw}%",)).fetchall()
    return out


_paths, _mirror = {}, None
def pdf_path(doc):
    global _mirror
    if doc in _paths:
        return _paths[doc]
    if _mirror is None:
        _mirror = {p.name: str(p) for p in Path("data/kwiki_mirror").rglob("*.pdf")}
    path = _mirror.get(doc)
    if not path:                      # 手工放进 data/ 的（如"已融合"版电路图）
        hit = [p for p in Path("data").rglob("*.pdf") if p.name == doc and "kwiki_mirror" not in str(p)]
        path = str(hit[0]) if hit else None
    if not path or not Path(path).exists():
        raise SystemExit(f"❌ 找不到 PDF：{doc}")
    _paths[doc] = path
    return path


def done_pages(con):
    return {(d, p) for d, p in con.execute("select doc_name, page from pages where status='ok'")}


def save(con, doc, page, kind, model, raw, usage):
    data = N.parse(raw)
    conns = data.get("connections", []) if isinstance(data, dict) else []
    title = data.get("figure_title", "") if isinstance(data, dict) else ""
    con.execute("delete from edges where doc_name=? and page=?", (doc, page))
    for c in conns:
        a, b = c.get("a") or {}, c.get("b") or {}
        g = lambda d, k: str(d.get(k, "") or "")
        con.execute("insert into edges values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc, page, title, str(c.get("cable", "") or ""),
                     g(a, "connector"), g(a, "mate"), g(a, "device"), g(a, "device_no"), g(a, "ref"),
                     g(b, "connector"), g(b, "mate"), g(b, "device"), g(b, "device_no"), g(b, "ref"),
                     str(c.get("wires", "") or ""), str(c.get("note", "") or ""), model))
    con.execute("insert or replace into pages values (?,?,?,?,?,?,?,?,?,?)",
                (doc, page, kind, model, "ok" if data else "fail", title, raw,
                 usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), NOW()))
    con.commit()
    return bool(data), len(conns)


def cmd_plan():
    con = db(); done = done_pages(con); t = targets()
    for kind in ("wiring", "general_wiring"):
        ks = [x for x in t if x[2] == kind]
        left = [x for x in ks if (x[0], x[1]) not in done]
        print(f"  {kind:15s} 共 {len(ks):4d} 页，已完成 {len(ks)-len(left):4d}，待抽 {len(left):4d}"
              f"  → {QWEN if kind == 'wiring' else CLAUDE + '（Batch）'}")
    for d in sorted({x[0] for x in t}):
        print(f"  文档: {d}  ({pdf_path(d)})")


def cmd_qwen(workers):
    from src.diagram_index import get_page_text
    con = db(); done = done_pages(con)
    todo = [x for x in targets() if x[2] == "wiring" and (x[0], x[1]) not in done]
    print(f"Qwen 待抽 {len(todo)} 页，{workers} 路并发，模型 {QWEN}", flush=True)

    def work(t):
        doc, page, kind = t
        pdf = fitz.open(pdf_path(doc))
        img = N.render(pdf, page); pdf.close()
        raw, usage = N.ask(QWEN, img, get_page_text(doc, page) or "")
        return t, raw, usage

    t0, ok, fail, tin, tout = time.time(), 0, 0, 0, 0
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            (doc, page, kind), raw, usage = fut.result()
            good, n = save(con, doc, page, kind, QWEN, raw, usage)
            ok += good; fail += not good
            tin += usage.get("prompt_tokens", 0); tout += usage.get("completion_tokens", 0)
            el = time.time() - t0
            print(f"  [{i}/{len(todo)}] {doc[:28]} 第{page}页 {'✓' if good else '✗'} {n} 条"
                  f"  · 剩余约 {el/i*(len(todo)-i)/60:.0f} 分钟", flush=True)
    print(f"\n== Qwen 完成：成功 {ok}，失败 {fail}，输入 {tin} tok，输出 {tout} tok，"
          f"用时 {(time.time()-t0)/60:.0f} 分钟 ==", flush=True)


def _claude_req(doc, page, hints):
    pdf = fitz.open(pdf_path(doc)); img = N.render(pdf, page); pdf.close()
    return {"model": CLAUDE, "max_tokens": 12000, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
        {"type": "text", "text": N.PROMPT.replace("{hints}", hints[:3000])}]}]}


def cmd_claude_submit(chunk=60, kind="general_wiring", redo=""):
    from src.diagram_index import get_page_text
    if not AKEY:
        sys.exit("❌ .env 里没有 ANTHROPIC_API_KEY")
    con = db(); done = done_pages(con)
    batches = json.loads(BATCH_FILE.read_text()) if BATCH_FILE.exists() else []
    inflight = {tuple(v[:2]) for b in batches if not b.get("collected") for v in b["map"].values()}
    if redo:   # 把某个模型抽过的页当作待重抽（如 --redo Qwen）
        done -= {(d, p) for d, p in con.execute("select doc_name, page from pages where model like ?", (f"%{redo}%",))}
    todo = [x for x in targets() if x[2] == kind
            and (x[0], x[1]) not in done and (x[0], x[1]) not in inflight]
    print(f"Claude 待提交 {len(todo)} 页（每批 {chunk} 页），模型 {CLAUDE}", flush=True)
    client = httpx.Client(timeout=600)
    for s in range(0, len(todo), chunk):
        part, reqs, mp = todo[s:s + chunk], [], {}
        for doc, page, kind in part:
            cid = f"{hashlib.md5(doc.encode()).hexdigest()[:8]}_{page}"
            reqs.append({"custom_id": cid, "params": _claude_req(doc, page, get_page_text(doc, page) or "")})
            mp[cid] = [doc, page, kind]
        r = client.post("https://api.anthropic.com/v1/messages/batches", headers=AHDR, json={"requests": reqs})
        if r.status_code != 200:
            sys.exit(f"❌ 提交失败 {r.status_code}: {r.text[:300]}")
        bid = r.json()["id"]
        batches.append({"id": bid, "submitted_at": NOW(), "model": CLAUDE, "map": mp})
        BATCH_FILE.write_text(json.dumps(batches, ensure_ascii=False, indent=1))
        print(f"  ✓ 已提交批次 {bid}（{len(part)} 页）", flush=True)


def cmd_claude_collect(interval=120):
    con = db(); client = httpx.Client(timeout=600)
    while True:
        batches = json.loads(BATCH_FILE.read_text()) if BATCH_FILE.exists() else []
        open_ = [b for b in batches if not b.get("collected")]
        if not open_:
            print("== Claude 批次全部收取完毕 ==", flush=True); return
        for b in open_:
            r = client.get(f"https://api.anthropic.com/v1/messages/batches/{b['id']}", headers=AHDR)
            j = r.json(); st = j.get("processing_status"); cnt = j.get("request_counts", {})
            print(f"  {NOW()}  批次 {b['id'][:20]}…  {st}  {cnt}", flush=True)
            if st != "ended":
                continue
            res = client.get(j["results_url"], headers=AHDR)
            ok = fail = 0
            for line in res.text.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line); doc, page, kind = b["map"][item["custom_id"]]
                rr = item.get("result", {})
                if rr.get("type") == "succeeded":
                    msg = rr["message"]
                    text = "".join(x.get("text", "") for x in msg.get("content", []) if x.get("type") == "text")
                    u = msg.get("usage", {})
                    good, _ = save(con, doc, page, kind, b.get("model", CLAUDE), text,
                                   {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)})
                else:
                    good = False
                    save(con, doc, page, kind, b.get("model", CLAUDE), "__ERROR__ " + json.dumps(rr)[:500], {})
                ok += good; fail += not good
            b["collected"] = True
            BATCH_FILE.write_text(json.dumps(batches, ensure_ascii=False, indent=1))
            print(f"  ✓ 批次 {b['id'][:20]}… 入库：成功 {ok}，失败 {fail}", flush=True)
        time.sleep(interval)


def cmd_stats():
    con = db()
    for kind, model, st, n, tin, tout in con.execute(
            "select kind, model, status, count(*), sum(tok_in), sum(tok_out) from pages group by 1,2,3"):
        print(f"  {kind:15s} {model:28s} {st:5s} {n:4d} 页  in {tin or 0} / out {tout or 0}")
    print(f"  连接总数: {con.execute('select count(*) from edges').fetchone()[0]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "qwen", "claude-submit", "claude-collect", "stats"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kind", default="general_wiring")
    ap.add_argument("--model", default="")
    ap.add_argument("--redo", default="")
    a = ap.parse_args()
    if a.model:
        CLAUDE = a.model
    {"plan": cmd_plan, "stats": cmd_stats, "claude-submit": lambda: cmd_claude_submit(kind=a.kind, redo=a.redo),
     "claude-collect": cmd_claude_collect, "qwen": lambda: cmd_qwen(a.workers)}[a.cmd]()
