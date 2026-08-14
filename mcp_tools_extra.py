"""Extra MCP tools for nikon-expert (imported by mcp_http_server).

Registers on the shared FastMCP instance from mcp_server. Kept separate so core
mcp_server.py (also used by the local stdio connector) stays untouched.

nikon_view_diagram routing (all generic / config-driven; see src/diagram_config):
  1. exact page-number channel      (query contains a page number)
  2. identifier inverted-index      (query contains wire/board/connector ids)
  3. FTS + cross-lingual vector      (fallback, unchanged behavior)
  then: optional unit filter, connection-intent kind re-ranking,
        full (untruncated) text layer + higher-DPI render returned as image.
"""
from mcp_server import mcp, _ensure_init

RENDER_DPI = 200          # higher than before so small labels/traces are legible
MAX_TEXT_CHARS = 6000     # effectively full page text layer (pages are ~3-4k)


def _dedup(cands):
    seen, out = set(), []
    for c in cands:
        k = (c["doc_name"], c["page"])
        if c["doc_name"] and c["page"] and k not in seen:
            seen.add(k)
            out.append(c)
    return out


@mcp.tool()
def nikon_view_diagram(query: str, unit: str = "", max_pages: int = 2) -> list:
    """Locate and RENDER Nikon diagram pages, returning them as IMAGES so you
    (Claude) can visually read the schematic/wiring, plus each page's FULL text
    layer (accurate component / wire / connector / board numbers).

    Precise-locate is automatic:
      - a page number in the query  -> that exact page
      - a wire/board/connector/mount id (e.g. 7214A2W-E, 4S018-922, A-712,
        IU-DRV1-X4P) -> pages containing that id (inverted index, deterministic)
      - otherwise -> full-text + cross-lingual vector search (Chinese ok)

    For connection questions ("how does A connect to B / 接哪块板 / 走线"),
    block / wiring diagrams are ranked above board-circuit diagrams automatically.

    Args:
        query: id / page number / component / signal / Japanese-English-Chinese term.
        unit:  optional unit/section filter to disambiguate colliding prefixes
               (e.g. WF vs WS vs WL). Substring, case-insensitive.
        max_pages: how many pages to render (default 2; keep small).
    """
    _ensure_init()
    from mcp.server.fastmcp import Image
    from src.figures import _locate_pdf, render_pdf_page
    from src.fulltext import search_fts
    from src import diagram_index as DI

    # auto-incremental: pick up any newly-ingested docs (cheap no-op if unchanged)
    try:
        DI.build_index(incremental=True, verbose=False)
    except Exception:
        pass

    def mk(doc, page, why):
        return {"doc_name": doc, "page": int(page), "why": why}

    cands = []
    # 1) exact page channel
    for dn, dt, pg in DI.locate_by_page(query):
        cands.append(mk(dn, pg, "page-exact"))
    # 2) identifier inverted index
    if len(cands) < max_pages:
        for dn, dt, pg, hits in DI.lookup_identifiers(query, limit=max_pages * 4):
            cands.append(mk(dn, pg, f"id-match×{hits}"))
    # 3) fallback: FTS exact then cross-lingual vector
    if len(cands) < max_pages:
        try:
            for r in search_fts(query.strip(), doc_type="circuit_diagram",
                                limit=max_pages * 3):
                pg = DI.page_from_section(r.get("section_title"))
                if pg:
                    cands.append(mk(r.get("doc_name"), pg, "fts"))
        except Exception:
            pass
    if len(cands) < max_pages:
        try:
            from src.engine import _init_engine
            for n in _init_engine()["retriever"].retrieve(query):
                mm = n.metadata or {}
                if mm.get("doc_type") == "circuit_diagram" and mm.get("page_start"):
                    cands.append(mk(mm.get("doc_name"), int(mm["page_start"]), "vector"))
        except Exception:
            pass

    cands = _dedup(cands)
    if not cands:
        return ["未定位到相关页。可用：线号/板号/连接器号（如 7214A2W-E、4S018-922、A-712）、"
                "页号（如 第34页）、或元件/信号名（中日英均可）。"]

    # attach tags
    for c in cands:
        c["unit"], c["kind"] = DI.get_page_tag(c["doc_name"], c["page"])

    # unit filter (disambiguation)
    if unit:
        u = unit.strip().lower()
        filt = [c for c in cands
                if (c.get("unit") and u in c["unit"].lower())
                or (u in (DI.get_page_text(c["doc_name"], c["page"]) or "").lower())]
        if filt:
            cands = filt

    # connection-intent re-ranking (block/wiring first, board-circuit last)
    if DI.is_connection_query(query):
        cands = DI.rank_for_connection(cands)

    out = []
    for c in cands[:max_pages]:
        doc, page = c["doc_name"], c["page"]
        text = DI.get_page_text(doc, page)
        pdf = _locate_pdf(doc)
        tag = " | ".join(x for x in [c.get("kind"), c.get("unit")] if x)
        head = f"【{doc} · 第{page}页{(' · ' + tag) if tag else ''} · {c['why']}】"
        if not pdf:
            out.append(head + f"\n（服务器无此 PDF 原件，无法渲染）\n文字层：\n{text[:MAX_TEXT_CHARS]}")
            continue
        try:
            png = render_pdf_page(pdf, page, dpi=RENDER_DPI)
        except Exception as e:
            out.append(head + f"\n（渲染失败：{e}）\n文字层：\n{text[:MAX_TEXT_CHARS]}")
            continue
        out.append(head + f"\n完整文字层（准确元件/线号）：\n{text[:MAX_TEXT_CHARS]}")
        out.append(Image(path=png))
    return out


@mcp.tool()
def nikon_reindex_diagrams(full: bool = False) -> str:
    """Rebuild the diagram identifier inverted-index + page tags. Incremental by
    default (only new/changed docs); pass full=true to rebuild everything.
    Normally unnecessary — nikon_view_diagram refreshes incrementally on its own."""
    _ensure_init()
    from src import diagram_index as DI
    stats = DI.build_index(incremental=not full, verbose=False)
    return (f"diagram index rebuilt ({'full' if full else 'incremental'}): "
            f"docs={stats['docs']} pages={stats['pages']} tokens={stats['tokens']}")


@mcp.tool()
def nikon_ask_wps(question: str, kuid: str = "") -> str:
    """Ask the WPS / 金山 kwiki knowledge base LIVE (its own RAG). Use for content
    that lives ONLY in WPS online documents and cannot be synced locally — large
    reference manuals, spec sheets, error-code lists, etc. Complements
    nikon_query (which searches the locally-ingested library).

    Args:
        question: natural-language question (Chinese/English/Japanese).
        kuid: optional WPS KB kuid (0s...). Empty = the configured default KB
              (KWIKI_KB_KUID), or all knowledge bases if unset.
    """
    import os
    import sys as _sys
    _sp = "/opt/nikon-expert/scripts"
    if _sp not in _sys.path:
        _sys.path.insert(0, _sp)
    # ensure WPS token is available even if not in the service env
    if not os.getenv("X_KWIKI_AUTH"):
        try:
            from dotenv import load_dotenv
            load_dotenv("/opt/nikon-expert/.kwiki_env", override=False)
        except Exception:
            pass
    try:
        import wps_api
    except Exception as e:
        return f"WPS 客户端加载失败：{e}"

    kuids = None
    k = (kuid or os.getenv("KWIKI_KB_KUID", "")).strip()
    if k:
        kuids = [k]
    try:
        r = wps_api.ask(question, kuids=kuids)
    except Exception as e:
        return f"WPS 问答失败：{e}"

    ans = r.get("answer") or ""
    if not ans:
        return (r.get("caution") or "WPS 知识库未找到相关内容。") + \
               ("\n(可换用文档里的术语/编号再试)" if r.get("caution") else "")
    out = [ans]
    if r.get("sources"):
        out.append("\n---\n来源(WPS 文档): " + " | ".join(r["sources"][:6]))
    return "\n".join(out)


@mcp.tool()
def nikon_query_worklog(recent_days: int = 7, date_from: str = "", date_to: str = "") -> str:
    """Read the field-engineering WORKLOG (工作日志) LIVE from Feishu — always the
    newest version, fetched on demand (NOT from the vector KB).

    分工 / when to use which:
      • nikon_query_worklog (this): 「最近日报写了啥 / 这几天进展/待协助事项」——
        永远返回飞书里的最新原文，按天精确取，适合近期动态、今日/本周计划。
      • nikon_query: 语义检索**历史**日志与全部手册/故障库，适合「之前哪天提过 X /
        某问题历史上怎么处理」这类跨时间的模糊查询（走向量库，可能不含刚写的今天）。

    Args:
        recent_days: 返回最近 N 天日报（默认 7）。若填了 date_from 则忽略本参数。
        date_from:  起始日期 'YYYY-MM-DD'（含）。
        date_to:    截止日期 'YYYY-MM-DD'（含），默认今天。
    """
    import os
    import sys as _sys
    _sp = "/opt/nikon-expert/scripts"
    if _sp not in _sys.path:
        _sys.path.insert(0, _sp)
    # 确保飞书配置/token 可见（即使不在 service env）
    try:
        from dotenv import load_dotenv
        load_dotenv("/opt/nikon-expert/.feishu_env", override=False)
        load_dotenv("/opt/nikon-expert/.env", override=False)
    except Exception:
        pass

    url = os.getenv("FEISHU_WORKLOG_URL", "").strip()
    if not url:
        return "未配置 FEISHU_WORKLOG_URL（在 .feishu_env 里配工作日志链接）。"
    try:
        import feishu_client
    except Exception as e:
        return f"飞书客户端加载失败：{e}"
    try:
        wl = feishu_client.fetch_worklog(url)
    except Exception as e:
        return (f"读取飞书工作日志失败：{e}\n"
                "（若提示 token 过期/未授权，请在本机重跑 "
                "`python scripts/feishu_auth.py login` 再试。）")

    days = wl.get("days") or []          # 最新在前
    if not days:
        return "飞书日志里没解析出按日期的日报，检查文档日期行格式。"

    if date_from:
        end = date_to or date_from[:4] + "-12-31"
        sel = [d for d in days if date_from <= d["date"] <= (date_to or "9999-12-31")]
        scope = f"{date_from}…{date_to or '今'}"
    else:
        n = max(1, int(recent_days))
        sel = days[:n]
        scope = f"最近 {len(sel)} 天"

    if not sel:
        return f"指定范围（{scope}）内没有日报。可用日期：{days[0]['date']} … {days[-1]['date']}。"

    title = wl.get("title") or "飞书工作日志"
    head = f"【{title} · {scope} · 实时读取自飞书】\n可用日期范围 {days[-1]['date']} … {days[0]['date']}"
    body = "\n\n────────────\n".join(d["text"] for d in sel)
    return head + "\n\n" + body
