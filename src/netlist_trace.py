"""线表追线：从 data/netlist.db（AI 从电路图配线图抽取的线缆级连接）里，
按接插件号 / 线缆号 / 设备名一跳一跳列出连接关系和所在页码，结果按树状显示（每条下面缩进的是从它引出的下一跳）。

规则：
- J 与 P 同号视为对插（J7554A ≡ P7554A），合并成同一个接头节点。
- 只有"全局唯一"的接插件号（J/P + 4 位以上数字）才跨页追；
  CN1、CN7、TB4、PS23 这类板上局部编号只在同一页内连接，避免把不相干的板串在一起。
- 一个字段里写了多个全局接插件号（AI 把一组接头归成一条，如 "J7544B / J7602H / J7544C"）的连接，
  照常显示、也能被直接查到，但不从这些号继续往外追，否则会串到别的单元。
- 线缆号是零件号，同一个号可能用在好几处（如一整排相同单元）。只有在所有页上都只连着同两个接插件的线缆号，
  才拿来跨页连接；这种线缆在某页上一头写的是"?"时，按别的页补上另一头。
- 同一条连接在多页重复出现（同两头、同线缆）合并成一行，列出全部页码。
- 经过一个接了 3 条以上支路的接头时，按针脚过滤：和上一段针脚不重叠的支路折叠成一行不展开；
  针脚没写或认不出的照常展开，标"针脚未知"。
"""
import os
import re
import sqlite3

_TOK = re.compile(r"[A-Z]{1,4}\d{1,5}[A-Z]{0,3}\d?")
_JP = re.compile(r"[JP](\d{2,5}[A-Z]{0,3}\d?)")
_UNIQ = re.compile(r"J/P\d{4}[A-Z0-9]*")
_CABLE = re.compile(r"\d?[A-Z]\d{3}-\d{3}(?:-\d+)?")
_NET = {"mtime": None}
JUNCTION = 3            # 一个接头上的候选支路达到这个数才按针脚过滤


def keys_of(*fields) -> set:
    ks = set()
    for f in fields:
        for t in _TOK.findall((f or "").upper().replace(" ", "")):
            m = _JP.fullmatch(t)
            ks.add("J/P" + m.group(1) if m else t)
    return ks


def _uniq(k: str) -> bool:
    return bool(_UNIQ.fullmatch(k))


def _lumped(*fields) -> set:
    """一个字段里写了多个全局接插件号（"J7544B / J7544C"、"J4145~J4150"）时返回这些号（不往外追）。
    conn 与 mate 是两个不同的接头（P1028A 插 J1113A），分开判断，不算归组。"""
    out = set()
    for f in fields:
        ks = {k for k in keys_of(f) if _uniq(k)}
        if len(ks) > 1:
            out |= ks
    return out


def _cable_no(c: str) -> str:
    m = _CABLE.search((c or "").upper())
    return m.group(0) if m else ""


_RANGE = re.compile(r"([A-Z]?)(\d+)\s*~\s*([A-Z]?)(\d+)")


def pins_of(w: str):
    """把 wires 字段（"45~47"、"A1~B2"、"S, T, U"、"1~20 (ND1-A, …)"）解析成针脚集合；认不出返回 None。"""
    s = re.sub(r"[（(].*?[)）]", " ", (w or "").upper())
    out = set()
    for p1, n1, p2, n2 in _RANGE.findall(s):
        a, b = int(n1), int(n2)
        if b < a or b - a > 300:
            continue
        l1, l2 = p1 or p2, p2 or p1
        letters = [chr(c) for c in range(ord(l1), ord(l2) + 1)] if l1 else [""]
        out |= {f"{l}{n}" for l in letters for n in range(a, b + 1)}
    s = _RANGE.sub(" ", s)
    out |= set(re.findall(r"(?<![A-Z0-9])(?:[A-Z]?\d{1,3}|[A-Z])(?![A-Z0-9])", s))
    return out or None


def _pretty(k: str) -> str:
    return f"J{k[3:]}/P{k[3:]}" if k.startswith("J/P") else k


def load():
    db = os.getenv("NETLIST_DB", "./data/netlist.db")
    if not os.path.exists(db):
        return None
    mt = os.path.getmtime(db)
    if mt == _NET["mtime"]:
        return _NET
    con = sqlite3.connect(db)
    rows = con.execute(
        "select doc_name, page, figure_title, cable, a_conn, a_mate, a_device, a_device_no, a_ref, "
        "b_conn, b_mate, b_device, b_device_no, b_ref, wires, model from edges").fetchall()
    con.close()

    # 1) 合并多页重复的连接：同线缆号 + 同一组接插件号
    groups, gidx = [], {}
    for r in rows:
        keys = keys_of(r[4], r[5], r[9], r[10])
        cab = _cable_no(r[3])
        sig = (cab, frozenset(keys)) if keys else None
        e = {"a": (r[4] or "", r[5] or "", " ".join(x for x in r[6:9] if x)),
             "b": (r[9] or "", r[10] or "", " ".join(x for x in r[11:14] if x)),
             "cable": r[3] or "", "wires": r[14] or ""}
        if sig in gidx:
            g = groups[gidx[sig]]
        else:
            g = {"cab": cab, "keys": keys, "follow": keys - _lumped(r[4], r[5], r[9], r[10]),
                 "pages": [], "best": e, "pins": None}
            if sig is not None:
                gidx[sig] = len(groups)
            groups.append(g)
        if (r[0], r[1]) not in g["pages"]:
            g["pages"].append((r[0], r[1]))
        info = lambda x: len(x["a"][2] + x["b"][2] + x["wires"]) + 20 * bool(x["a"][0] and x["b"][0])
        if info(e) > info(g["best"]):
            g["best"] = e
        p = pins_of(r[14])
        if p:
            g["pins"] = (g["pins"] or set()) | p

    # 2) 线缆号：所有页上只连着同两个接插件的，才可跨页连接
    cab_conns, cab_pages = {}, {}
    for g in groups:
        if g["cab"]:
            cab_conns.setdefault(g["cab"], set()).update(k for k in g["follow"] if _uniq(k))
    ok_cab = {c for c, ks in cab_conns.items() if len(ks) <= 2}
    for i, g in enumerate(groups):
        if g["cab"] in ok_cab:
            g["follow"] = g["follow"] | {"C:" + g["cab"]}
            for k in g["follow"]:
                if _uniq(k):
                    cab_pages.setdefault((g["cab"], k), set()).update(p for _, p in g["pages"])
    # 3) 一头是"?"的：这根线缆在别的页写了另一头，就补上
    for g in groups:
        g["fill"] = ""
        b = g["best"]
        if g["cab"] not in ok_cab or (b["a"][0] or b["a"][1]) and (b["b"][0] or b["b"][1]):
            continue
        other = cab_conns[g["cab"]] - {k for k in g["follow"] if _uniq(k)}
        if len(other) == 1:
            k = other.pop()
            pg = sorted(cab_pages.get((g["cab"], k), set()) - {p for _, p in g["pages"]})
            if pg:
                g["fill"] = f"{_pretty(k)}（据第 {'、'.join(map(str, pg[:3]))} 页）"

    index = {}
    for i, g in enumerate(groups):
        for k in g["keys"] | {k for k in g["follow"] if k.startswith("C:")}:
            index.setdefault(k, []).append(i)
    _NET.update(mtime=mt, groups=groups, index=index)
    return _NET


def _doc_short(d: str) -> str:
    if "SMIF" in d:
        return "SMIF Loader 电路图"
    if "已融合" in d:
        return "S207D 电路图（已融合版）"
    return d[:40]


def _end(t, fill="") -> str:
    c, m, d = t
    ids = "/".join(x for x in (c, m) if x)
    return (ids or (f"?→{fill}" if fill else "?")) + (f" [{d}]" if d else "")


def _where(g) -> str:
    by = {}
    for d, p in g["pages"]:
        by.setdefault(_doc_short(d), []).append(p)
    return "；".join(f"《{d}》第 {'、'.join(map(str, sorted(ps)))} 页" for d, ps in by.items())


def _line(g) -> str:
    b = g["best"]
    cab = b["cable"] or "无线缆号"
    w = f" {b['wires']}" if b["wires"] else ""
    a_empty = not (b["a"][0] or b["a"][1])
    return (f"{_end(b['a'], g['fill'] if a_empty else '')}  ──[{cab}{w}]──  "
            f"{_end(b['b'], '' if a_empty else g['fill'])}   {_where(g)}")


def trace(query: str, max_hops: int = 3, max_lines: int = 80) -> str:
    net = load()
    if not net:
        return "线表数据库不存在（data/netlist.db）。"
    G, I = net["groups"], net["index"]
    q = (query or "").strip()
    if not q:
        return "请给一个接插件号、线缆号或设备名。"

    def share_page(i, j):
        return bool(set(G[i]["pages"]) & set(G[j]["pages"]))

    def lookup(k, frm=None):
        """经过 k 能到的连接。全局号/线缆号跨页；局部编号只在同一页。"""
        hits = I.get(k, [])
        if frm is None or _uniq(k) or k.startswith("C:"):
            return list(hits)
        return [j for j in hits if share_page(j, frm)]

    start = keys_of(q)
    roots = set()
    for k in start:
        roots |= set(lookup(k))
    cq = _cable_no(q)
    if not roots and cq:
        roots = {i for i, g in enumerate(G) if g["cab"] == cq}
    if not roots:
        ql = q.lower()
        roots = {i for i, g in enumerate(G)
                 if ql in g["best"]["a"][2].lower() or ql in g["best"]["b"][2].lower()}
    if not roots:
        return (f"线表里没找到「{q}」。可以试试接插件号（J7554A、P1394A）、线缆号（4S095-884）、"
                f"设备名（Robot Controller），或者用 nikon_view_diagram 直接搜图。")

    # 广度优先：每条连接记下是从谁、经哪个接头引出来的（取最短路径），再按树打印
    children, folded, mark = {}, {}, {}
    seen, used = set(roots), set(start) | ({"C:" + cq} if cq else set())
    # 同一跳里先展开写了针脚的连接，这样经过接头时有针脚可比，过滤才起作用
    order = lambda ids: sorted(ids, key=lambda i: (G[i]["pins"] is None, i))
    level = order(roots)
    for _ in range(max_hops):
        nxt = []
        for i in level:
            for k in sorted(G[i]["follow"]):
                if k in used:
                    continue
                used.add(k)
                cand = [j for j in lookup(k, i) if j not in seen]
                if not cand:
                    continue
                keep, drop = cand, []
                pi = G[i]["pins"]
                if len(cand) >= JUNCTION and pi:
                    keep, drop = [], []
                    for j in cand:
                        pj = G[j]["pins"]
                        if pj is None:
                            mark[j] = "（针脚未知）"
                            keep.append(j)
                        elif pj & pi:
                            keep.append(j)
                        else:
                            drop.append(j)
                for j in keep:
                    seen.add(j)
                    children.setdefault(i, []).append((k, j))
                    nxt.append(j)
                if drop:
                    folded.setdefault(i, []).append((k, drop))
        level = order(nxt)
        if not level:
            break

    out = [f"追线：{q}（最多 {max_hops} 跳；缩进一层 = 往外一跳，“经 X”表示通过哪个接头/线缆接过去）"]
    truncated = [False]

    def emit(s):
        if len(out) >= max_lines:
            truncated[0] = True
            return False
        out.append(s)
        return True

    def walk(i, depth, via):
        pad = "  " + "    " * depth
        head = "" if depth == 0 else f"└ 经 {_pretty(via[2:]) if via.startswith('C:') else _pretty(via)}："
        if not emit(f"{pad}{head}{_line(G[i])}{mark.get(i, '')}"):
            return
        for k, drop in folded.get(i, []):
            names = []
            for j in drop[:6]:
                b = G[j]["best"]
                other = [x for x in (b["a"], b["b"]) if k not in keys_of(x[0], x[1])]
                t = other[0] if other else b["b"]
                names.append(f"{_end(t)}（{b['wires']}）")
            more = f" 等 {len(drop)} 条" if len(drop) > 6 else ""
            emit(f"{pad}    … 经 {_pretty(k)} 还有{more}针脚和这一段不重叠的支路，未展开："
                 + "、".join(names))
        for k, j in children.get(i, []):
            walk(j, depth + 1, k)

    out.append("")
    for r in order(roots):
        walk(r, 0, "")
        if truncated[0]:
            break
    if truncated[0]:
        out.append("  …结果太多已截断，换个更具体的接插件号再查，或减少跳数。")
    out.append("\n说明：J 与 P 同号按对插处理；CN/TB 等板上局部编号只在同一页内连接；"
               "“?→X（据第 N 页）”是按同一根线缆在别的页的记录补的另一头。"
               "连接关系由 AI 读图抽取，关键结论请用 nikon_view_diagram 调原图核对。")
    return "\n".join(out)
