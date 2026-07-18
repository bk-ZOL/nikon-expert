# src/net_trace.py
# Nikon Expert — 连接追踪（按线号交叉引用还原元件连接关系）
#
# 电路图的连接关系编码在"共享的线号/网络号"里（如 2613C）：
#   同一线号出现在 sensor 处、也出现在某面板/板卡处 → 它俩就是这根线连起来的。
# 这正是工程师看图的追线方式。本模块：
#   1. 定位元件所在页（FTS，circuit_diagram）；
#   2. 从该页文字抽出线号（\d{3,4}[A-Z] 型）与 pin（PS\d 型）；
#   3. 交叉引用每个线号在全篇其它页的出现处（另一端）→ 还原连接。
# 局限：给出"线号级"连接（哪根线连哪些点），非像素级走线；密集细节仍需看原图。

import os
import re
from pathlib import Path

WIRE_RE = re.compile(r"\b\d{3,4}[A-Z]\b")     # 线号/端子号，如 2613C 2615F
PIN_RE  = re.compile(r"\bPS\d{1,2}\b")        # pin，如 PS1 PS5

_text_cache = {}   # pdf_path -> [page_text, ...]


def _pages_text(pdf_path: str):
    if pdf_path in _text_cache:
        return _text_cache[pdf_path]
    import fitz
    doc = fitz.open(pdf_path)
    try:
        pages = [doc[i].get_text() for i in range(len(doc))]
    finally:
        doc.close()
    _text_cache[pdf_path] = pages
    return pages


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
    if not hits:
        return None
    best = max(hits, key=lambda h: len(WIRE_RE.findall(h[3] or "")))
    doc_name, pdf, page, text = best
    return doc_name, pdf, page, text


def trace_connection(query: str, max_wires: int = 6, max_refs: int = 6) -> str:
    """按线号交叉引用，还原某元件/信号的连接关系。"""
    loc = _locate(query)
    if not loc:
        return f"未在电路图里定位到与「{query}」相关的元件页。请用图纸术语或线号（如 2613C）。"
    doc_name, pdf, page, ptext = loc

    wires = list(dict.fromkeys(WIRE_RE.findall(ptext)))[:max_wires]
    pins  = list(dict.fromkeys(PIN_RE.findall(ptext)))
    if not wires:
        return (f"《{doc_name}》第{page}页 有相关元件，但未识别到线号。"
                f"\n本页内容摘录：{' '.join(ptext.split())[:200]}")

    pages = _pages_text(pdf)

    # 本页就是接线表 → 直接给出连接内容（sensor→线号→pin→面板，文字层准确）
    wiring = " ".join(ptext.split())
    lines = [f"【连接追踪 · {query}】",
             f"起点接线表：《{doc_name}》第 {page} 页",
             f"线号：{', '.join(wires)}" + (f"　pin：{', '.join(pins)}" if pins else ""),
             f"接线内容（文字层准确）：{wiring[:500]}",
             ""]

    # 交叉引用：把含任一线号的其它页按“内容签名”去重（合并卷常有重复页）
    import hashlib
    sig_seen, distinct = {}, []
    for i, t in enumerate(pages):
        if i + 1 == page:
            continue
        shared = [w for w in wires if w in t]
        if not shared:
            continue
        sig = hashlib.md5(" ".join(t.split()).encode()).hexdigest()[:10]
        if sig in sig_seen:
            sig_seen[sig][1].append(i + 1)
            continue
        entry = [_page_header(t), [i + 1], shared]
        sig_seen[sig] = entry
        distinct.append(entry)

    if distinct:
        lines.append("▶ 这些线号还出现在（已按内容去重）：")
        for hdr, pgs, shared in distinct[:max_refs]:
            dup = f"（另有 {len(pgs)-1} 页内容相同）" if len(pgs) > 1 else ""
            lines.append(f"    · 第{pgs[0]}页 {dup}｜共享线号 {', '.join(shared)}｜{hdr}")
        lines.append("\n说明：同一线号出现处即电气连通点。若只见到重复的同一面板、"
                     "未见'另一端'（如控制板卡），多半在其余未摄入的卷里。")
    else:
        lines.append("▶ 本卷内这些线号未见于其它页——另一端可能在其余 4 卷（尚未摄入）。")
    lines.append("（线号级连接，非像素走线；端子细节请点开原图。）")
    return "\n".join(lines)
