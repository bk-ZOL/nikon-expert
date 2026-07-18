# src/figures.py
# Nikon Expert — 电路图/接线图并入知识库（按需渲染 + VLM 看图）
#
# 设计（呼应"只抽图存索引、描述按需生成"）：
#   - 不改摄入：知识库 chunk 本就存了 file_path + page_start，"页↔文档"索引已现成。
#   - 按需：查询命中相关手册页时，从源 PDF 渲染那一页 → 交视觉大模型解读。
#   - 图纸常是矢量绘制（get_images 抽不到），故用整页渲染，通用可靠。

import os
import hashlib
from pathlib import Path

FIG_DIR = Path(os.getenv("FIGURES_DIR", "./data/figures"))
RENDER_DPI = int(os.getenv("FIGURE_DPI", "150"))

DIAGRAM_PROMPT = (
    "围绕问题「{q}」看这一页光刻机资料：\n"
    "1) 这页是否含电路图/接线图/信号连接示意/机械装配图？\n"
    "2) 若有图：识别图中元件与标注、说明连接关系、指出可能的检查点或故障点；\n"
    "3) 若只是文字/表格，直接说明本页无图。"
)


def render_pdf_page(pdf_path: str, page: int, dpi: int = RENDER_DPI) -> str:
    """把 PDF 指定页（1-based）渲染成 PNG，带缓存，返回图片路径。"""
    import fitz  # pymupdf

    pdf_path = str(pdf_path)
    key = hashlib.sha256(f"{pdf_path}:{page}:{dpi}".encode()).hexdigest()[:16]
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = FIG_DIR / f"{key}_p{page}.png"
    if out.exists():
        return str(out)

    doc = fitz.open(pdf_path)
    try:
        idx = max(0, min(page - 1, len(doc) - 1))
        pix = doc[idx].get_pixmap(dpi=dpi)
        pix.save(str(out))
    finally:
        doc.close()
    return str(out)


DATA_ROOT = Path(os.getenv("DATA_ROOT", "./data"))
_pdf_cache = {}


def _locate_pdf(doc_name: str) -> str:
    """按文件名在 data/ 下定位真实 PDF（现有 KB 未存 file_path，故按名找）。"""
    if doc_name in _pdf_cache:
        return _pdf_cache[doc_name]
    name = doc_name if doc_name.lower().endswith(".pdf") else doc_name + ".pdf"
    hit = ""
    for p in DATA_ROOT.rglob(name):
        hit = str(p); break
    if not hit:  # 宽松匹配：去扩展名的 stem
        stem = Path(name).stem
        for p in DATA_ROOT.rglob("*.pdf"):
            if p.stem == stem:
                hit = str(p); break
    _pdf_cache[doc_name] = hit
    return hit


def _norm(s: str) -> str:
    return "".join(s.split())


def _find_page(pdf_path: str, snippet: str) -> int:
    """用 chunk 文本片段在 PDF 里搜出真实页码（1-based）。找不到返回 1。"""
    import fitz
    probe = _norm(snippet)[:40]
    if not probe:
        return 1
    doc = fitz.open(pdf_path)
    try:
        # 逐段缩短探针，提高命中率
        for plen in (40, 24, 14):
            key = probe[:plen]
            for i in range(len(doc)):
                if key and key in _norm(doc[i].get_text()):
                    return i + 1
    finally:
        doc.close()
    return 1


MULTI_PROMPT = (
    "围绕问题「{q}」，下面是知识库手册里 {n} 页可能相关的内容（含相邻页，因为"
    "电路图/接线图常跨页或分处不同页）。请：\n"
    "1) 找出其中哪些页真的含电路图/接线图/信号图/装配图（指明是第几张图）；\n"
    "2) 跨页/关联的图请**综合起来**识读：元件、标注、连接关系；\n"
    "3) 结合问题指出可能的检查点或故障点；无图的页忽略即可。"
)


def _circuit_pages(query: str, k: int = 4):
    """在电路图里按元件/信号定位，返回 [(doc_name, pdf_path, page, text), ...]。
    FTS（精确：线号/日英术语）+ 语义向量（中文跨语言，若已向量化）两路合并。"""
    import re as _re
    out, seen = [], set()

    def _add(doc_name, fp, page, text):
        if not fp or not Path(fp).exists():
            return
        key = (fp, page)
        if key in seen:
            return
        seen.add(key)
        out.append((doc_name or Path(fp).name, fp, page, text or ""))

    # 1) FTS 精确（线号/日英术语）
    from src.fulltext import search_fts
    try:
        for r in search_fts(query.strip(), doc_type="circuit_diagram", limit=k * 2):
            m = _re.search(r"第(\d+)页", str(r.get("section_title", "")))
            if m:
                _add(r.get("doc_name"), str(r.get("file_path", "")),
                     int(m.group(1)), r.get("text", ""))
    except Exception:
        pass

    # 2) 语义向量（中文跨语言）——电路图页向量元数据含 page_start + file_path
    if len(out) < k:
        try:
            from src.engine import _init_engine
            for n in _init_engine()["retriever"].retrieve(query):
                mm = n.metadata or {}
                if mm.get("doc_type") == "circuit_diagram" and mm.get("page_start"):
                    _add(mm.get("doc_name"), str(mm.get("file_path", "")),
                         int(mm["page_start"]), n.get_content())
        except Exception:
            pass

    return out[:k]


def _manual_hits(query: str, k: int = 3):
    """语义检索最相关的手册 chunk，返回去重的 [(doc_name, chunk_text), ...]。"""
    from src.engine import _init_engine
    eng = _init_engine()
    try:
        nodes = eng["retriever"].retrieve(query)
    except Exception:
        nodes = []
    out, seen = [], set()
    for n in nodes:
        m = n.metadata or {}
        if m.get("doc_type") != "manual" or not m.get("doc_name"):
            continue
        txt = n.get_content()
        key = (m["doc_name"], txt[:30])
        if key in seen:
            continue
        seen.add(key)
        out.append((m["doc_name"], txt))
        if len(out) >= k:
            break
    return out


def inspect_diagram(query: str, provider_id: str = None, model: str = None,
                    max_pages: int = 4) -> str:
    """按内容在知识库里定位相关手册页（含相邻页，覆盖跨页/关联图）→ 渲染 →
    视觉大模型**一次多图综合识读**。现有 KB 无 file_path/正确页码，故按 doc_name
    定位 PDF + 用 chunk 文本反查真实页码，无需重新摄入。
    """
    from src.vision import read_images

    # ── A. 电路图命中（密集图）：文字层准确 → 返回真实文字 + 渲染图供查看，
    #        不跑本地小模型整页识读（实测会幻觉编元件号）。
    circuit = _circuit_pages(query, k=max_pages)
    if circuit:
        from urllib.parse import quote
        blocks, srcs = [], []
        for doc_name, pdf, pg, text in circuit:
            excerpt = " ".join((text or "").split())[:300]
            url = f"/serve-pdf?path={quote(pdf, safe='')}#page={pg}"
            srcs.append(f"《{doc_name}》第{pg}页")
            blocks.append(f"● 第 {pg} 页 [查看原图]({url})\n  文字层内容：{excerpt}")
        note = ("（以上为电路图对应页的**文字层准确内容**与原图链接。密集电路图的实际走线"
                "请点开原图查看/缩放；本地小模型不做整页识读以免误判，需视觉细读可切 Claude。）")
        return ("【电路图定位命中】\n" + "\n".join(blocks) + "\n" + note +
                f"\n[来源：{'；'.join(srcs)}]")

    # ── B. 普通手册示意图：按名定位 + 文本反查页码（含续页）→ VLM 识读
    pages, seen = [], set()
    for i, (doc_name, chunk_text) in enumerate(_manual_hits(query)):
        pdf = _locate_pdf(doc_name)
        if not pdf:
            continue
        pg = _find_page(pdf, chunk_text)
        wanted = [pg, pg + 1] if i == 0 else [pg]
        for pp in wanted:
            keyp = (pdf, pp)
            if pp < 1 or keyp in seen:
                continue
            seen.add(keyp)
            pages.append((doc_name, pdf, pp))
            if len(pages) >= max_pages:
                break
        if len(pages) >= max_pages:
            break

    if not pages:
        return f"知识库中未找到与「{query}」相关的电路图/手册页。"

    imgs, srcs = [], []
    for doc_name, pdf, pp in pages:
        try:
            imgs.append(render_pdf_page(pdf, pp))
            srcs.append(f"《{doc_name}》第{pp}页")
        except Exception:
            continue
    if not imgs:
        return f"找到相关手册，但渲染页面失败或未定位到源 PDF。"

    desc = read_images(imgs, MULTI_PROMPT.format(q=query, n=len(imgs)),
                       provider_id=provider_id, model=model)
    return f"【看图 · {len(imgs)}页】{'、'.join(srcs)}\n{desc}\n[来源：{'；'.join(srcs)}]"
