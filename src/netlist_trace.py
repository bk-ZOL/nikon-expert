"""线表追线：从 data/netlist.db（AI 从电路图配线图抽取的线缆级连接）里，
按接插件号 / 线缆号 / 设备名一跳一跳列出连接关系和所在页码，结果按树状显示（每条下面缩进的是从它引出的下一跳）。

规则：
- J 与 P 同号视为对插（J7554A ≡ P7554A），合并成同一个接头节点。
- 只有"全局唯一"的接插件号（J/P + 4 位以上数字）才跨页追；
  CN1、CN7、TB4、PS23 这类板上局部编号只在同一页内连接，避免把不相干的板串在一起。
- 接插件号只在同一机型内唯一：已融合版里同时有 S207D 和 S307E 的图，照明切换部两种机型编号不同
  （S307E 第 4142 页的 J7544E 是快门传感器，S207D 第 70 页的 J7544E 是 SPURE 原点传感器）。
  每页按页上的 "(NSR-S307E)" / "(NSR-S207D)" 标记推断机型，跨页时 S207D 页和 S307E 页不互连，通用页两边都连。
- 一个字段里写了一组接插件号（"J7544B / J7602H / J7544C …"）的，是一束线分出的多路（第 4142 页对过原图），照常往外追。
  设 NETLIST_BLOCK_LUMPED=1 可改回不往外追。
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
_BLOCK_LUMPED = os.getenv("NETLIST_BLOCK_LUMPED", "0") == "1"


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
             "adev": r[6] or "", "bdev": r[11] or "",
             "cable": r[3] or "", "wires": r[14] or ""}
        if sig in gidx:
            g = groups[gidx[sig]]
        else:
            g = {"cab": cab, "keys": keys, "follow": keys - (_lumped(r[4], r[5], r[9], r[10]) if _BLOCK_LUMPED else set()),
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

    fts = _fts_pages({d for g in groups for d, _ in g["pages"]})
    var = _page_variants(fts)
    for g in groups:
        g["var"] = {var.get(k, "") for k in g["pages"]} - {""}
    _add_hints(groups, ok_cab, None, fts)

    index = {}
    for i, g in enumerate(groups):
        for k in g["keys"] | {k for k in g["follow"] if k.startswith("C:")}:
            index.setdefault(k, []).append(i)
    _NET.update(mtime=mt, groups=groups, index=index)
    return _NET


_norm = lambda x: re.sub(r"[^A-Z0-9]", "", (x or "").upper())


def _fts_pages(docs: set) -> dict:
    """全文索引里这些电路图每页的 {(图册, 页): (去掉符号转大写的文字, 本页线缆号集合, 页上标的机型)}。读不到返回空。"""
    db = os.getenv("FTS_DB_PATH", "./data/fts.db")
    if not os.path.exists(db):
        return {}
    try:
        try:
            import pysqlite3 as sq
        except Exception:
            sq = sqlite3
        con = sq.connect(db)
        out = {}
        for name, title, text in con.execute(
                "select doc_name, section_title, text from doc_fts where doc_type='circuit_diagram'"):
            if name in docs:
                m = re.search(r"(\d+)", title or "")
                if m:
                    t = text or ""
                    mark = "S307E" if "(NSR-S307E)" in t else "S207D" if "(NSR-S207D)" in t else ""
                    out[(name, int(m.group(1)))] = (_norm(t), set(_CABLE.findall(t.upper())), mark)
        con.close()
        return out
    except Exception:
        return {}


def _page_variants(pages: dict, window: int = 80) -> dict:
    """已融合版里同时装着 S207D 和 S307E 的图，两种机型的接插件编号不一样
    （S307E 第 4142 页的 J7544E 是快门传感器，S207D 第 70 页的 J7544E 是 SPURE 原点传感器）。
    页上写了 "(NSR-S307E)" / "(NSR-S207D)" 的直接用；没写的，前后最近的标记一致（相距不超过 window 页）
    或 20 页内只有一种标记，就沿用；推不出来的算通用页。返回 {(图册, 页): "S207D" / "S307E" / ""}。"""
    import bisect
    out = {}
    by_doc = {}
    for (d, p), v in pages.items():
        by_doc.setdefault(d, {})[p] = v[2]
    for d, lab in by_doc.items():
        L = sorted(p for p, v in lab.items() if v)
        for p, v in lab.items():
            if not v and L:
                i = bisect.bisect_left(L, p)
                a = L[i - 1] if i > 0 else None
                b = L[i] if i < len(L) else None
                if a is not None and b is not None and lab[a] == lab[b] and b - a <= window:
                    v = lab[a]
                else:
                    near = {lab[x] for x in (a, b) if x is not None and abs(x - p) <= 20}
                    v = near.pop() if len(near) == 1 else ""
            out[(d, p)] = v
    return out


def _add_hints(groups, ok_cab, docs, pages=None):
    """一头是"?"、又没能按线缆补上的连接：按全文索引找"另一头可能画在哪页"。
    依据从强到弱：别的页同时有这根线缆号和这个设备名 > 只有线缆号（仅限不一号多用的线缆，≤3 页）
    > 只有设备名（这个设备在线表里出现不超过 4 页）。"""
    pages = _fts_pages(docs) if pages is None else pages
    cab_pages = {}
    for key, (_, cabs, _) in pages.items():
        for c in cabs:
            cab_pages.setdefault(c, set()).add(key)
    dev_pages = {}
    for g in groups:
        b = g["best"]
        for (c, m, _), dev in ((b["a"], b["adev"]), (b["b"], b["bdev"])):
            if (c or m) and len(_norm(dev)) >= 4:
                dev_pages.setdefault(_norm(dev), set()).update(g["pages"])
    for g in groups:
        g["hint"] = ""
        b = g["best"]
        a_empty, b_empty = not (b["a"][0] or b["a"][1]), not (b["b"][0] or b["b"][1])
        if g["fill"] or a_empty == b_empty:
            continue
        dev = _norm(b["adev"] if a_empty else b["bdev"])
        own = set(g["pages"])
        found, why = set(), ""
        if g["cab"]:
            cands = cab_pages.get(g["cab"], set()) - own
            if len(dev) >= 4:
                both = {k for k in cands if dev[:24] in pages[k][0]}
                if both:
                    found, why = both, "同线缆号、同设备名"
            if not found and g["cab"] in ok_cab and 0 < len(cands) <= 3:
                found, why = cands, "同线缆号"
        if not found and len(dev) >= 4:
            dp = dev_pages.get(dev, set()) - own
            if 0 < len(dp) <= 4:
                found, why = dp, "这个设备的接线"
        if found:
            pg = sorted(p for _, p in found)
            more = f" 等 {len(pg)} 页" if len(pg) > 4 else " 页"
            g["hint"] = f"另一头可能在第 {'、'.join(map(str, pg[:4]))}{more}（{why}）"


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
    v = "/".join(sorted(g.get("var") or []))
    return "；".join(f"《{d}》第 {'、'.join(map(str, sorted(ps)))} 页" for d, ps in by.items()) + (f"〔{v}〕" if v else "")


def _line(g) -> str:
    b = g["best"]
    cab = b["cable"] or "无线缆号"
    w = f" {b['wires']}" if b["wires"] else ""
    a_empty = not (b["a"][0] or b["a"][1])
    hint = f"  ⇢ {g['hint']}" if g.get("hint") else ""
    return (f"{_end(b['a'], g['fill'] if a_empty else '')}  ──[{cab}{w}]──  "
            f"{_end(b['b'], '' if a_empty else g['fill'])}   {_where(g)}{hint}")


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

    def same_model(i, j):
        """S207D 页和 S307E 页不互连（两种机型编号不同）；通用页、推不出机型的页两边都连。"""
        vi, vj = G[i].get("var"), G[j].get("var")
        return not vi or not vj or bool(vi & vj)

    def lookup(k, frm=None):
        """经过 k 能到的连接。全局号/线缆号跨页（同机型）；局部编号只在同一页。"""
        hits = I.get(k, [])
        if frm is None:
            return list(hits)
        if _uniq(k) or k.startswith("C:"):
            return [j for j in hits if same_model(j, frm)]
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
               "〔S207D〕〔S307E〕是这页属于哪个机型（两种机型接插件编号不同，不互连，没标的是通用页）；"
               "“?→X（据第 N 页）”是按同一根线缆在别的页的记录补的另一头；“⇢ 另一头可能在第 N 页”是按全文索引找的线索，要看图确认。"
               "连接关系由 AI 读图抽取，关键结论请用 nikon_view_diagram 调原图核对。")
    return "\n".join(out)
