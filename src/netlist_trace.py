"""线表追线：从 data/netlist.db（AI 从电路图配线图抽取的线缆级连接）里，
按接插件号 / 线缆号 / 设备名一跳一跳列出连接关系和所在页码。

规则：
- J 与 P 同号视为对插（J7554A ≡ P7554A），合并成同一个接头节点。
- 只有"全局唯一"的接插件号（J/P + 4 位以上数字）才跨页追；
  CN1、CN7、TB4、PS23 这类板上局部编号只在同一页内连接，避免把不相干的板串在一起。
"""
import os
import re
import sqlite3

_TOK = re.compile(r"[A-Z]{1,4}\d{1,5}[A-Z]{0,3}\d?")
_JP = re.compile(r"[JP](\d{2,5}[A-Z]{0,3}\d?)")
_UNIQ = re.compile(r"J/P\d{4}[A-Z0-9]*")
_NET = {"mtime": None, "edges": [], "index": {}}


def keys_of(*fields) -> set:
    ks = set()
    for f in fields:
        for t in _TOK.findall((f or "").upper().replace(" ", "")):
            m = _JP.fullmatch(t)
            ks.add("J/P" + m.group(1) if m else t)
    return ks


def _uniq(k: str) -> bool:
    return bool(_UNIQ.fullmatch(k))


def load():
    db = os.getenv("NETLIST_DB", "./data/netlist.db")
    if not os.path.exists(db):
        return None
    mt = os.path.getmtime(db)
    if mt != _NET["mtime"]:
        con = sqlite3.connect(db)
        rows = con.execute(
            "select doc_name, page, figure_title, cable, a_conn, a_mate, a_device, a_device_no, a_ref, "
            "b_conn, b_mate, b_device, b_device_no, b_ref, wires, model from edges").fetchall()
        con.close()
        edges, index = [], {}
        for r in rows:
            e = {"doc": r[0], "page": r[1], "title": r[2] or "", "cable": r[3] or "",
                 "a": (r[4] or "", r[5] or "", " ".join(x for x in r[6:9] if x)),
                 "b": (r[9] or "", r[10] or "", " ".join(x for x in r[11:14] if x)),
                 "wires": r[14] or "", "model": r[15] or ""}
            e["keys"] = keys_of(r[4], r[5], r[9], r[10])
            i = len(edges)
            edges.append(e)
            for k in e["keys"]:
                index.setdefault(k, []).append(i)
        _NET.update(mtime=mt, edges=edges, index=index)
    return _NET


def _doc_short(d: str) -> str:
    if "SMIF" in d:
        return "SMIF Loader 电路图"
    if "已融合" in d:
        return "S207D 电路图（已融合版）"
    return d[:40]


def _end(t) -> str:
    c, m, d = t
    ids = "/".join(x for x in (c, m) if x)
    return (ids or "?") + (f" [{d}]" if d else "")


def trace(query: str, max_hops: int = 3, max_lines: int = 80) -> str:
    net = load()
    if not net:
        return "线表数据库不存在（data/netlist.db）。"
    E, I = net["edges"], net["index"]
    q = (query or "").strip()
    if not q:
        return "请给一个接插件号、线缆号或设备名。"

    def lookup(k, ctx):
        return [i for i in I.get(k, []) if ctx is None or (E[i]["doc"], E[i]["page"]) == ctx]

    start = keys_of(q)
    level = set()
    for k in start:
        level |= set(lookup(k, None))
    if not level:                                   # 线缆号 / 设备名
        ql = q.lower()
        level = {i for i, e in enumerate(E)
                 if e["cable"].lower() == ql or ql in e["a"][2].lower() or ql in e["b"][2].lower()}
    if not level:
        return (f"线表里没找到「{q}」。可以试试接插件号（J7554A、P1394A）、线缆号（4S095-884）、"
                f"设备名（Robot Controller），或者用 nikon_view_diagram 直接搜图。")

    out, seen, visited = [f"追线：{q}（最多 {max_hops} 跳）"], set(), set(start)
    for hop in range(max_hops + 1):
        todo = sorted(i for i in level if i not in seen)
        if not todo:
            break
        out.append("\n【直接相关】" if hop == 0 else f"\n【第 {hop} 跳】")
        nxt = set()
        for i in todo:
            if len(out) >= max_lines:
                out.append("  …结果太多已截断，换个更具体的接插件号再查。")
                break
            seen.add(i)
            e = E[i]
            cab = e["cable"] or "无线缆号"
            w = f" {e['wires']}" if e["wires"] else ""
            out.append(f"  {_end(e['a'])}  ──[{cab}{w}]──  {_end(e['b'])}"
                       f"   《{_doc_short(e['doc'])}》第 {e['page']} 页")
            for k in e["keys"]:
                if k in visited:
                    continue
                visited.add(k)
                nxt |= set(lookup(k, None if _uniq(k) else (e["doc"], e["page"])))
        if len(out) >= max_lines:
            break
        level = nxt
    out.append("\n说明：J 与 P 同号按对插处理；CN/TB 等板上局部编号只在同一页内连接。"
               "连接关系由 AI 读图抽取，关键结论请用 nikon_view_diagram 调原图核对。")
    return "\n".join(out)
