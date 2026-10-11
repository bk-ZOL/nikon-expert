"""线表试点 · Claude 对比：同样的页面、同样的提示词。
用法: .venv/bin/python scripts/netlist_claude.py [--model claude-sonnet-5-5]
"""
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dotenv import load_dotenv; load_dotenv(".env")
import fitz, httpx
import netlist_pilot as N

KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLIENT = httpx.Client(timeout=300)


def ask(model, img_b64, hints):
    body = {"model": model, "max_tokens": 8000, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": N.PROMPT.replace("{hints}", hints[:3000])}]}]}
    delay = 5
    for _ in range(6):
        try:
            r = CLIENT.post("https://api.anthropic.com/v1/messages", json=body,
                            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"})
            if r.status_code == 200:
                j = r.json()
                text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
                u = j.get("usage", {})
                return text, {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0)}
            if r.status_code in (401, 403):
                sys.exit(f"❌ key 无效或无权限：{r.text[:200]}")
            if r.status_code == 400 and "credit" in r.text.lower():
                sys.exit(f"❌ Anthropic 账户余额不足：{r.text[:200]}")
            if r.status_code in (429, 500, 502, 503, 529):
                print(f"    ⏳ {r.status_code}，{delay}s 后重试", flush=True)
            else:
                return f"__ERROR__ {r.status_code} {r.text[:300]}", {}
        except httpx.TransportError as e:
            print(f"    ⏳ {type(e).__name__}，{delay}s 后重试", flush=True)
        time.sleep(delay); delay = min(delay * 2, 60)
    return "__ERROR__ 重试耗尽", {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--doc", default="NSR-S207D S307E Reticle SMIF Loader")
    args = ap.parse_args()
    if not KEY:
        sys.exit("❌ .env 里没有 ANTHROPIC_API_KEY")
    doc, path, pages, get_text = N.pages_for(args.doc)
    pages = list(dict.fromkeys(pages[:10] + [170, 228]))
    print(f"文档: {doc}\n模型: {args.model}\n页面: {pages}\n", flush=True)
    out = N.OUT / args.model; out.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open(path)
    tin = tout = ok = 0; t0 = time.time(); summary = [f"# Claude 对比 · {args.model}"]
    for n in pages:
        raw, usage = ask(args.model, N.render(pdf, n), get_text(doc, n) or "")
        data = N.parse(raw)
        tin += usage.get("prompt_tokens", 0); tout += usage.get("completion_tokens", 0); ok += bool(data)
        (out / f"p{n}.json").write_text(json.dumps({"page": n, "raw": raw, "data": data}, ensure_ascii=False, indent=1))
        conns = (data or {}).get("connections", []) if isinstance(data, dict) else []
        mark = "✓" if data else ("✗ " + raw[:80])
        print(f"  第{n}页  {mark}  {len(conns)} 条连接  (in {usage.get('prompt_tokens',0)} / out {usage.get('completion_tokens',0)})", flush=True)
        summary.append(f"\n## 第 {n} 页 · {(data or {}).get('figure_title','') if isinstance(data, dict) else ''}")
        for c in conns:
            a, b = c.get("a") or {}, c.get("b") or {}
            summary.append(f"  {c.get('cable',''):11s} {a.get('connector','')}/{a.get('mate','')} [{a.get('device','')} {a.get('ref','')}]"
                           f"  ⇄  {b.get('connector','')}/{b.get('mate','')} [{b.get('device','')} {b.get('ref','')}]  {c.get('wires','')}")
    summary.append(f"\n== {args.model}: {len(pages)} 页，成功 {ok}，输入 {tin} tok，输出 {tout} tok，用时 {(time.time()-t0)/60:.1f} 分钟 ==")
    (out / "summary.txt").write_text("\n".join(summary))
    print(summary[-1], flush=True)


if __name__ == "__main__":
    main()
