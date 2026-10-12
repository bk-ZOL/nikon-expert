# src/net_trace.py
# Nikon Expert — 连接追踪（按线号交叉引用还原元件连接关系）
#
# 电路图的连接关系编码在"共享的线号/网络号"里（如 2613C）：
#   同一线号出现在 sensor 处、也出现在某面板/板卡处 → 它俩就是这根线连起来的。
# 这正是工程师看图的追线方式。本模块：
#   1. 定位元件所在页（FTS，circuit_diagram）；
#   2. 从该页文字（全文索引里的版本，含入库时 OCR 补出的竖排编号）抽出线号、全局接插件号（J/P）与 pin；
#   3. 交叉引用每个编号在全篇其它页的出现处（另一端）→ 还原连接。
# 局限：给出"线号级"连接（哪根线连哪些点），非像素级走线；密集细节仍需看原图。

import os
import re
from pathlib import Path

WIRE_RE = re.compile(r"(?<![A-Za-z0-9])\d{3,4}[A-Z](?:\d[A-Z]?)?(?![A-Za-z0-9])")  # 线号/端子号，如 2613C、7214A2
CONN_RE = re.compile(r"(?<![A-Za-z0-9])[JP](\d{4,5}[A-Z]?)(?![A-Za-z0-9])")        # 全局接插件号，J1394G ≡ P1394G
BARE_CONN = re.compile(r"\d{4,5}[A-Z]")                                            # 不带前缀的接插件号
PIN_RE  = re.compile(r"\bPS\d{1,2}\b")        # pin，如 PS1 PS5
OCR_MARK = "【OCR标签】"

_text_cache = {}   # (机型族, fts mtime) -> {(doc_name, page): text}


def _family(doc_name: str) -> str:
    from src.machine_model import normalize
    fams = normalize(doc_name)
    return fams[0] if fams else doc_name


def _pages_text(doc_name: str, pdf_path: str) -> dict:
    """{(图册名, 页码): 文字}，范围是与 doc_name 同机型的所有电路图（S206D 分 4 卷，另一端常在别的卷）。
    从全文索引读（含入库时 OCR 补出的竖排编号）；索引里一页都没有才直接读这份 PDF 的文字层。"""
    db = os.getenv("FTS_DB_PATH", "./data/fts.db")
    fam = _family(doc_name)
    key = (fam, doc_name if fam == doc_name else "", os.path.getmtime(db) if os.path.exists(db) else 0)
    if key in _text_cache:
        return _text_cache[key]
    pages = {}
    if os.path.exists(db):
        try:
            try:
                import pysqlite3 as sq
            except Exception:
                import sqlite3 as sq
            con = sq.connect(db)
            for name, title, text in con.execute(
                    "select doc_name, section_title, text from doc_fts where doc_type='circuit_diagram'"):
                if name != doc_name and _family(name) != fam:
                    continue
                m = re.search(r"(\d+)", title or "")
                if m:
                    pages[(name, int(m.group(1)))] = text or ""
            con.close()
        except Exception:
            pages = {}
    if not pages and pdf_path and Path(pdf_path).exists():
        import fitz
        doc = fitz.open(pdf_path)
        try:
            pages = {(doc_name, i + 1): doc[i].get_text() for i in range(len(doc))}
        finally:
            doc.close()
    _text_cache.clear()                      # 只留当前这一族，图册很大
    _text_cache[key] = pages
    return pages


def _short(name: str) -> str:
    n = name.replace(".pdf", "")
    m = re.search(r"Volume\s*(\d)\s*of\s*(\d)", n, re.I)
    if "SMIF" in n:
        return "SMIF Loader"
    if "已融合" in n:
        return "已融合版"
    return f"第{m.group(1)}卷" if m else n[:30]


def _tokens(text: str) -> dict:
    """一页里可交叉引用的编号：{编号: 是否只在 OCR 部分出现}。接插件号统一写成 J/P1394G。"""
    body, _, ocr = (text or "").partition(OCR_MARK)
    out = {}
    for part, is_ocr in ((body, False), (ocr, True)):
        # 图上中继面板等处的接插件号常不写 J/P 前缀（2613C 就是 P2613C），4 位数字 + 1 个字母的统一成 J/P 号
        found = {("J/P" + w if BARE_CONN.fullmatch(w) else w) for w in WIRE_RE.findall(part)}
        found |= {"J/P" + n for n in CONN_RE.findall(part)}
        for t in found:
            out[t] = out.get(t, True) and is_ocr
    return out


def _page_header(text: str, width: int = 60) -> str:
    """取一页的面板/单元标识（去掉重复的抬头行）。"""
    t = " ".join(text.split())
    t = t.replace("NSR-S207D/S307E 電気回路図 Rev. 1.0 / NSR-S207D/S307E Electrical Circuit Diagram Rev. 1.0", "").strip()
    return t[:width]


def _locate(query: str):
    """定位元件页：在多个命中里挑**线号最多**的那页（真正的接线表，
    而非目录/布局页）。返回 (doc_name, pdf_path, page, page_text) 或 None。"""
    from src.figures import _circuit_pages
    hits = _circuit_pages(query, k=6)
    if not hits:                         # J7554A 在图上可能写成 P7554A 或不带前缀的 7554A
        for n in CONN_RE.findall(query.upper()):
            hits = _circuit_pages("P" + n, k=6) or _circuit_pages(n, k=6)
            if hits:
                break
    if not hits:
        return None
    from src.machine_model import model_factor
    want = set(_tokens(query.upper()))
    # 先挑含所查编号的页，再按机型（没点名时 S207D 优先），最后挑编号最多的（真正的接线表）
    best = max(hits, key=lambda h: (bool(want & set(_tokens(h[3]))),
                                    model_factor(h[0] or "", query), len(_tokens(h[3]))))
    doc_name, pdf, page, text = best
    return doc_name, pdf, page, text


def trace_connection(query: str, max_wires: int = 6, max_refs: int = 6) -> str:
    """按线号交叉引用，还原某元件/信号的连接关系。"""
    loc = _locate(query)
    if not loc:
        return f"未在电路图里定位到与「{query}」相关的元件页。请用图纸术语或线号（如 2613C）。"
    doc_name, pdf, page, ptext = loc

    toks = _tokens(ptext)
    asked = [t for t in _tokens(query.upper()) if t in toks]            # 所查的编号排第一
    body_toks = [("J/P" + w if BARE_CONN.fullmatch(w) else w) for w in WIRE_RE.findall(ptext.partition(OCR_MARK)[0])]
    wires = asked + [t for t in dict.fromkeys(body_toks) if t not in asked][:max_wires]
    wires += [t for t in sorted(toks) if t not in wires and (toks[t] or t.startswith("J/P"))
              ][:max(0, max_wires + 1 - len(wires))]
    pins  = list(dict.fromkeys(PIN_RE.findall(ptext)))
    if not wires:
        return (f"《{doc_name}》第{page}页 有相关元件，但未识别到线号或接插件号。"
                f"\n本页内容摘录：{' '.join(ptext.split())[:200]}")

    pages = _pages_text(doc_name, pdf)
    show = lambda t: (f"J{t[3:]}/P{t[3:]}" if t.startswith("J/P") else t) + ("（OCR）" if toks.get(t) else "")

    # 本页就是接线表 → 直接给出连接内容（sensor→线号→pin→面板）
    wiring = " ".join(ptext.partition(OCR_MARK)[0].split())
    lines = [f"【连接追踪 · {query}】",
             f"起点接线表：《{doc_name}》第 {page} 页",
             f"线号/接插件号：{', '.join(show(t) for t in wires)}" + (f"　pin：{', '.join(pins)}" if pins else ""),
             f"接线内容（文字层）：{wiring[:500]}",
             ""]

    # 交叉引用：把含任一编号的其它页按“内容签名”去重（合并卷常有重复页）
    import hashlib
    want = set(wires)
    sig_seen, distinct = {}, []
    for (dn, pg) in sorted(pages):
        if (dn, pg) == (doc_name, page):
            continue
        t = pages[(dn, pg)]
        if not _quick(t, want):
            continue
        tt = _tokens(t)
        shared = [w for w in wires if w in tt]
        if not shared:
            continue
        sig = hashlib.md5(" ".join(t.split()).encode()).hexdigest()[:10]
        if sig in sig_seen:
            sig_seen[sig][1].append((dn, pg))
            continue
        entry = [_page_header(t.partition(OCR_MARK)[0]), [(dn, pg)], shared]
        sig_seen[sig] = entry
        distinct.append(entry)

    # 含所查编号的排最前；其次别的卷（同卷的多半是同一单元的相邻页）；再按页码
    distinct.sort(key=lambda e: (not set(asked) & set(e[2]), e[1][0][0] == doc_name, e[1][0]))
    if distinct:
        lines.append("▶ 这些编号还出现在（同机型全部电路图，已按内容去重，别的卷排前面）：")
        for hdr, pgs, shared in distinct[:max_refs]:
            dup = f"（另有 {len(pgs)-1} 页内容相同）" if len(pgs) > 1 else ""
            dn, pg = pgs[0]
            where = f"第{pg}页" if dn == doc_name else f"《{_short(dn)}》第{pg}页"
            lines.append(f"    · {where} {dup}｜共享 {', '.join(show(w) for w in shared)}｜{hdr}")
        if len(distinct) > max_refs:
            lines.append(f"    · …还有 {len(distinct) - max_refs} 处，换个更具体的编号再查")
        lines.append("\n说明：同一线号/接插件号出现处即电气连通点（J 与 P 同号按对插，图上不带前缀的 2613C 也算同一个）。"
                     "标（OCR）的编号是从图上竖排字识别出来的，可能认错，请对照原图。")
    else:
        lines.append("▶ 这些编号在同机型的其它电路图页里都没有出现。")
    lines.append("（线号级连接，非像素走线；端子细节请点开原图。）")
    return "\n".join(lines)


def _quick(text: str, want: set) -> set:
    """粗筛：去掉 J/P 前缀后按子串判断，命中才做精确匹配，省时间。"""
    return {w for w in want if (w[3:] if w.startswith("J/P") else w) in text}
