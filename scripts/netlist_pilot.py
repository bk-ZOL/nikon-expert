"""线表试点：视觉模型读配线图 → 线缆级连接关系 JSON。

用法（服务器上）:
  .venv/bin/python scripts/netlist_pilot.py                 # 默认：S207D SMIF Loader 全部配线图
  .venv/bin/python scripts/netlist_pilot.py --doc 关键字 --limit 10
输出:
  data/netlist_pilot/<模型>/p<页码>.json   每页原始结果
  data/netlist_pilot/summary.txt           人看的汇总 + token 用量 + 费用估算
"""
import argparse, base64, json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
import fitz, httpx

try:
    import pysqlite3 as sq
except Exception:
    import sqlite3 as sq

API = os.getenv("EMBED_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
KEY = os.getenv("EMBED_API_KEY", "")
OUT = Path("data/netlist_pilot")
CLIENT = httpx.Client(timeout=300, trust_env=False)

PROMPT = """这是 Nikon NSR 光刻机电路图中的一页配线图（Wiring Diagram / 配線図）。
请把图上每一根线缆（cable / harness）的连接关系抄出来，只输出 JSON，不要任何解释。

格式：
{
  "figure_title": "图的标题，如 SMIF Loader 部 配線図 No.2",
  "connections": [
    {
      "cable": "线缆编号，如 4S082-013；图上没写就填空串",
      "a": {"connector": "线缆这一端的接插件，如 J1394G", "mate": "它插在设备上的对插接插件，如 P1394A；没有就空串",
            "device": "这一端连接的设备/基板/单元名称，如 Robot Controller", "device_no": "设备型号，如 4S064-563", "ref": "单元号，如 A-131"},
      "b": {"connector": "", "mate": "", "device": "", "device_no": "", "ref": ""},
      "wires": "芯数或针脚范围，如 34 芯 / 1~20；看不清就空串",
      "note": "备注，如跨页引用 →No.3"
    }
  ],
  "offpage": ["指向其他页/图号的引用原文，如 To 配線図 No.5"]
}

要求：
- 编号必须照图上原样抄写，不要猜。下面给出了这一页文字层和 OCR 识别到的编号，拼写以它们为准。
- 看不清的字段填空串，不要编造。
- 同一根线缆两端分别是 a 和 b；只画了一端的也要列出，另一端留空。

这一页已知的文字和编号：
{hints}
"""


def list_models():
    r = CLIENT.get(f"{API}/models", headers={"Authorization": f"Bearer {KEY}"},
                   params={"type": "text", "sub_type": "chat"})
    r.raise_for_status()
    return [m["id"] for m in r.json()["data"]]


def pick_models(avail):
    big = [m for m in avail if re.search(r"Qwen3-VL-235B.*Instruct", m)]
    mid = [m for m in avail if m == "Qwen/Qwen3-VL-32B-Instruct"]
    if not mid:
        mid = [m for m in avail if re.search(r"Qwen3-VL-32B.*Instruct", m)]
    return (mid[0] if mid else None), (big[0] if big else None)


def pages_for(doc_kw):
    con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
    rows = con.execute(
        "select doc_name, page from diagram_pages where doc_name like ? "
        "and kind in ('wiring','general_wiring') order by page", (f"%{doc_kw}%",)).fetchall()
    if not rows:
        sys.exit(f"没找到 '{doc_kw}' 的配线图页")
    doc = rows[0][0]
    from src.diagram_index import get_page_text
    path = None
    for d, in [(doc,)]:
        hit = list(Path("data/kwiki_mirror").rglob(d))
        path = str(hit[0]) if hit else None
    if not path:
        r = con.execute("select file_path from doc_fts where doc_name=? and file_path!='' limit 1", (doc,)).fetchone()
        path = r[0] if r else None
    if not path or not Path(path).exists():
        sys.exit(f"找不到 {doc} 的 PDF 文件")
    return doc, path, [p for _, p in rows], get_page_text


def render(pdf, page_no, long_side=2400):
    pg = pdf[page_no - 1]
    zoom = min(long_side / max(pg.rect.width, pg.rect.height), 300 / 72)
    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return base64.b64encode(pix.tobytes("png")).decode()


def ask(model, img_b64, hints):
    body = {
        "model": model, "temperature": 0, "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"}},
            {"type": "text", "text": PROMPT.replace("{hints}", hints[:3000])},
        ]}],
    }
    if "Qwen3.5" in model or "Thinking" not in model and "Qwen3" in model:
        body["enable_thinking"] = False          # 抄图不需要思考模式，省 token
    delay = 3
    for attempt in range(6):
        try:
            r = CLIENT.post(f"{API}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {KEY}"})
            if r.status_code == 200:
                j = r.json()
                return j["choices"][0]["message"]["content"], j.get("usage", {})
            if r.status_code == 402:
                sys.exit(f"\n❌ 账户余额不足（402），已停止。充值后重跑即可。")
            if r.status_code in (429, 500, 502, 503, 504):
                print(f"    ⏳ {r.status_code}，{delay}s 后重试", flush=True)
            else:
                return f"__ERROR__ {r.status_code} {r.text[:300]}", {}
        except httpx.TransportError as e:
            print(f"    ⏳ {type(e).__name__}，{delay}s 后重试", flush=True)
        time.sleep(delay); delay = min(delay * 2, 60)
    return "__ERROR__ 重试耗尽", {}


def parse(text):
    if not text or text.startswith("__ERROR__"):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def run(model, doc, path, pages, get_page_text, summary):
    d = OUT / model.replace("/", "_"); d.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open(path)
    tok_in = tok_out = ok = 0
    t0 = time.time()
    for n in pages:
        hints = get_page_text(doc, n) or ""
        raw, usage = ask(model, render(pdf, n), hints)
        data = parse(raw)
        tok_in += usage.get("prompt_tokens", 0); tok_out += usage.get("completion_tokens", 0)
        (d / f"p{n}.json").write_text(json.dumps({"page": n, "raw": raw, "data": data}, ensure_ascii=False, indent=1))
        conns = (data or {}).get("connections", []) if isinstance(data, dict) else []
        ok += bool(data)
        mark = "✓" if data else ("✗ " + raw[:80] if raw.startswith("__ERROR__") else "✗ 解析失败")
        print(f"  {model.split('/')[-1]}  第{n}页  {mark}  {len(conns)} 条连接  "
              f"(in {usage.get('prompt_tokens', 0)} / out {usage.get('completion_tokens', 0)})", flush=True)
        summary.append(f"\n## {model} · 第 {n} 页 · {(data or {}).get('figure_title', '') if isinstance(data, dict) else ''}")
        for c in conns:
            a, b = c.get("a", {}) or {}, c.get("b", {}) or {}
            summary.append(f"  {c.get('cable',''):12s} {a.get('connector','')}/{a.get('mate','')} [{a.get('device','')} {a.get('device_no','')} {a.get('ref','')}]"
                           f"  ⇄  {b.get('connector','')}/{b.get('mate','')} [{b.get('device','')} {b.get('device_no','')} {b.get('ref','')}]  {c.get('wires','')}")
        if not data:
            summary.append(f"  ✗ 解析失败: {raw[:200]}")
    dt = time.time() - t0
    summary.append(f"\n== {model}: {len(pages)} 页，成功 {ok}，输入 {tok_in} tok，输出 {tok_out} tok，用时 {dt/60:.1f} 分钟 ==")
    return tok_in, tok_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="NSR-S207D S307E Reticle SMIF Loader")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--compare", type=int, default=10, help="大模型对比的页数")
    args = ap.parse_args()

    avail = list_models()
    vl = [m for m in avail if "VL" in m or "Qwen3.5" in m]
    print("账户可用的视觉相关模型:\n  " + "\n  ".join(vl), flush=True)
    mid, big = pick_models(avail)
    if not mid:
        sys.exit("没找到 Qwen3-VL-32B-Instruct，把上面的列表发给 Claude 选型号")
    doc, path, pages, gpt = pages_for(args.doc)
    if args.limit:
        pages = pages[:args.limit]
    print(f"\n文档: {doc}\n配线图 {len(pages)} 页: {pages}\n主模型: {mid}\n对比模型: {big or '（无 235B）'}\n", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    summary = [f"# 线表试点 · {doc}"]
    run(mid, doc, path, pages, gpt, summary)
    if big:
        run(big, doc, path, pages[:args.compare], gpt, summary)
    (OUT / "summary.txt").write_text("\n".join(summary))
    print("\n" + "\n".join(s for s in summary if s.startswith("\n==") or s.startswith("==")), flush=True)
    print(f"\n汇总: {OUT/'summary.txt'}", flush=True)


if __name__ == "__main__":
    main()
