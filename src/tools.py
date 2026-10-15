# src/tools.py
# Nikon Expert — Agent 工具箱（P1）
# 把现有检索能力封装成 ReAct agent 可调用的 FunctionTool。
# 每个工具在返回给 LLM 文本的同时，把命中的资料记入 SourceCollector，
# 供最终答案统一引用（与 engine._build_context 的 citations_data 结构对齐）。

import functools
from pathlib import Path
from typing import List

from llama_index.core.tools import FunctionTool


# ── 来源收集器 ──────────────────────────────────────────────────
class SourceCollector:
    """一次 agent 运行期间累积所有工具命中的资料，去重后产出引用。"""

    def __init__(self):
        self._items = []          # 原始命中 dict
        self._seen = set()        # (doc_id, section/page) 去重

    def add(self, results: list, tool: str):
        for r in results:
            key = (r.get("doc_id", ""), r.get("section_title", "") or r.get("page_start", ""))
            if key in self._seen:
                continue
            self._seen.add(key)
            r = dict(r)
            r["_tool"] = tool
            self._items.append(r)

    def _cite_text(self, r: dict) -> str:
        doc_name = r.get("doc_name", "未知文档")
        doc_type = r.get("doc_type", "")
        page = r.get("page_start", "")
        section = r.get("section_title", "")
        date = r.get("fault_date", "")
        if doc_type == "manual" and page:
            return f"《{doc_name}》第 {page} 页"
        if doc_type == "manual" and section:
            return f"《{doc_name}》— {section}"
        if doc_type == "fault_history":
            codes = r.get("error_codes", [])
            code_str = f"[{', '.join(codes)}] " if codes else ""
            return f"故障履历 {code_str}{date or ''} — {doc_name}".strip()
        if doc_type == "checksheet":
            row = r.get("row_index", "")
            return f"《{doc_name}》第 {row} 行" if row else f"《{doc_name}》"
        fp = r.get("file_path", "")
        return f"{doc_name}" + (f"（{Path(fp).parent.name}）" if fp else "")

    def _groups(self) -> list:
        """按文档合并命中：同一份文件命中多段/多行/多页时只列一次（2026-10-14 用户反馈来源重复）。"""
        groups, by_name = [], {}
        for r in self._items:
            name = r.get("doc_name", "未知文档")
            if name not in by_name:
                by_name[name] = {"first": r, "items": []}
                groups.append(by_name[name])
            by_name[name]["items"].append(r)
        return groups

    @staticmethod
    def _join(vals) -> str:
        out = []
        for v in vals:
            v = str(v).strip()
            if v and v not in out:
                out.append(v)
        return "、".join(out)

    def _group_text(self, g: dict) -> str:
        items, r = g["items"], g["first"]
        if len(items) == 1:
            return self._cite_text(r)
        doc_name = r.get("doc_name", "未知文档")
        doc_type = r.get("doc_type", "")
        if doc_type == "manual":
            pages = self._join(i.get("page_start", "") for i in items)
            if pages:
                return f"《{doc_name}》第 {pages} 页"
            secs = self._join(i.get("section_title", "") for i in items)
            return f"《{doc_name}》— {secs}" if secs else f"《{doc_name}》"
        if doc_type == "fault_history":
            dates = self._join(i.get("fault_date", "") for i in items)
            return f"故障履历 — {doc_name}" + (f"（{dates}）" if dates else "")
        if doc_type == "checksheet":
            rows = self._join(i.get("row_index", "") for i in items)
            return f"《{doc_name}》第 {rows} 行" if rows else f"《{doc_name}》"
        return self._cite_text(r)

    def citations(self) -> list:
        """返回展示用引用字符串列表 [ '[1] xxx', ... ]（同一文件合并为一条）"""
        return [f"[{i}] {self._group_text(g)}" for i, g in enumerate(self._groups(), 1)]

    def citations_data(self) -> list:
        """返回结构化引用，供 UI 构建可点击按钮（与 engine 对齐；同一文件合并为一条，指向首个命中）。"""
        out = []
        for i, g in enumerate(self._groups(), 1):
            r = g["first"]
            page = r.get("page_start", "")
            out.append({
                "index": i,
                "doc_name": r.get("doc_name", ""),
                "doc_type": r.get("doc_type", ""),
                "file_path": r.get("file_path", ""),
                "page": int(page) if str(page).isdigit() else None,
                "section": r.get("section_title", ""),
                "source": r.get("_tool", ""),
                "cite_text": self._group_text(g),
            })
        return out

    def has_any(self) -> bool:
        return bool(self._items)


# ── 命中结果 → 给 LLM 的文本 ─────────────────────────────────────
def _format(results: list, empty: str, max_chars: int = 500) -> str:
    if not results:
        return empty
    lines = []
    for i, r in enumerate(results, 1):
        name = r.get("doc_name", "?")
        section = r.get("section_title", "")
        dtype = r.get("doc_type", "")
        head = f"[{i}] {name}" + (f" — {section}" if section else "") + (f" ({dtype})" if dtype else "")
        text = (r.get("text", "") or "").strip().replace("\n", " ")[:max_chars]
        lines.append(f"{head}\n{text}")
    return "\n\n".join(lines)


# ── 工具工厂 ────────────────────────────────────────────────────
def build_tools(collector: SourceCollector, retriever=None) -> List[FunctionTool]:
    """构造一批绑定到本次 collector 的 FunctionTool。

    retriever: 语义检索器（engine 的 VectorIndexRetriever）。为空则语义工具降级为关键词搜索。
    """
    from src.fulltext import search_fts, search_fts_exact

    # 本次运行的工具调用记忆：同一 (工具,参数) 只真正执行一次，
    # 重复调用直接返回提示，避免小模型 ReAct 在工具间空转、无谓消耗迭代。
    _call_cache = {}

    from src.util_timeout import call_with_timeout

    import re as _re

    def _norm_arg(arg):
        # 归一化去重键：去掉分隔符/空白，让 E-5301 / E5301 / e 5301 视为同一次调用，
        # 避免小模型换着写法把同一个码反复搜、空转步数。
        return _re.sub(r"[\s\-_./]", "", (arg or "").strip().lower())

    def _memo(name, arg, run, timeout=30):
        key = (name, _norm_arg(arg))
        if key in _call_cache:
            return (f"（你本轮已用相同参数调用过 {name}，结果同上，请不要重复调用；"
                    f"若信息已足够就直接给出最终结论。）")
        # 软超时：单个工具卡住不拖垮整个 ReAct 循环，超时返回提示让 LLM 继续。
        result = call_with_timeout(
            run, timeout,
            on_timeout=lambda: (f"（{name} 超时（>{timeout}s）已跳过。请基于已取得的信息作答，"
                                f"或改用其它工具/换个参数，不要重试同一调用。）"))
        _call_cache[key] = True
        return result

    def error_code_lookup(code: str) -> str:
        """精确查询某个 Nikon Error Code（如 E-5301、P-12345、ALM-04）的原厂记录与含义。
        现场写法 STG-29-80（控制器-主码-细分码）会自动换算成手册里的主码 29H / 细分码 80H 并给出原文。
        当故障描述中出现明确的报警码 / error code 时，优先用本工具。
        参数 code: 报警码字符串。"""
        def run():
            from src import acl
            from src.error_code import lookup as _conv
            # 换算读的是 207 的错误信息原文（data/kwiki_mirror/5.Error code），没有对应权限就不给
            conv = _conv(code) if acl.can_see(None, "oem_manual", "S207D") else ""

            results = search_fts_exact(code.strip(), limit=6)
            collector.add(results, tool="error_code_lookup")
            found = _format(results, empty=f"知识库中未找到 {code} 的记录。")
            return (f"{conv}\n\n【知识库里提到 {code.strip()} 的记录（履历/日志等）】\n{found}") if conv else found
        return _memo("error_code_lookup", code, run)

    def keyword_search(query: str) -> str:
        """按关键词做全文精确搜索（手册、Checksheet、笔记）。
        适合查具体部件名、参数名、操作步骤等确定性关键词。
        参数 query: 空格分隔的关键词。"""
        def run():
            results = search_fts(query.strip(), limit=6)
            collector.add(results, tool="keyword_search")
            return _format(results, empty=f"未搜索到与「{query}」相关的内容。")
        return _memo("keyword_search", query, run)

    def semantic_search(query: str) -> str:
        """按语义检索原理、机制、概念类内容（理解意思而非仅匹配关键词）。
        适合「为什么」「原理是什么」「和X的区别」这类概念性问题。
        参数 query: 自然语言问题。"""
        def run():
            if retriever is None:
                return keyword_search(query)
            try:
                # 走 engine._retrieve_semantic：security 开时按当前用户注入权限过滤。
                # 以前直接用全局 retriever，绕过了权限（待审核的客户资料也会被查出来）。
                from src import acl
                from src.engine import _init_engine, _retrieve_semantic
                eng = _init_engine()
                nodes = _retrieve_semantic(eng, query, acl.get_current_user(), eng.get("top_k", 8))
            except acl.PermissionDenied:
                return "（没有检索权限）"
            except Exception as e:
                return f"语义检索失败：{e}"
            results = []
            for n in nodes:
                m = n.metadata or {}
                results.append({
                    "doc_id": m.get("doc_id", ""),
                    "doc_name": m.get("doc_name", ""),
                    "doc_type": m.get("doc_type", ""),
                    "section_title": m.get("section_title", ""),
                    "page_start": m.get("page_start", ""),
                    "file_path": m.get("file_path", ""),
                    "text": n.get_content(),
                })
            collector.add(results, tool="semantic_search")
            return _format(results, empty=f"未检索到与「{query}」语义相关的内容。")
        return _memo("semantic_search", query, run)

    def fault_history_lookup(query: str) -> str:
        """在厂内故障履历中查历史相似案例（曾经怎么处理、换了什么、结果如何）。
        定位根因或想找处置先例时使用。
        参数 query: 故障现象或报警码关键词。"""
        def run():
            results = search_fts(query.strip(), doc_type="fault_history", limit=6)
            collector.add(results, tool="fault_history_lookup")
            return _format(results, empty=f"故障履历中未找到与「{query}」相似的案例。")
        return _memo("fault_history_lookup", query, run)

    def inspect_diagram(query: str) -> str:
        """看图：当故障涉及接线/电路/信号/机械装配，需要看图纸时用本工具。
        它会在知识库/电路图里定位最相关的页，返回文字层内容与原图链接。
        参数 query: 要看的图的主题（如「reticle 载台伺服接线」「AIS 信号连接」）。"""
        def run():
            from src.figures import inspect_diagram as _inspect
            return _inspect(query)
        return _memo("inspect_diagram", query, run, timeout=90)

    def trace_connection(query: str) -> str:
        """连接追踪：确认某元件/传感器/信号"连到哪、用哪根线、哪个 pin、到哪个面板/板卡"。
        它定位接线表页、抽出线号(如 2613C)与 pin(如 PS1)，并按线号交叉引用到其它页。
        当需要确认元件之间的电气连接关系时用本工具。
        参数 query: 元件/信号名或线号（用图纸术语，如「YF軸ソフトリミット」「2613C」）。"""
        def run():
            from src.net_trace import trace_connection as _trace
            return _trace(query)
        return _memo("trace_connection", query, run, timeout=90)

    def netlist_trace(query: str) -> str:
        """线表追线：查"某个接插件/线缆两头接到哪、经过哪根线缆、哪几针、在第几页"，并往外追 2 跳。
        数据是从 S207D 电路图（含 SMIF Loader）配线图抽出来的线缆级连接表，J 与 P 同号按对插处理。
        问题里有接插件号（J7554A、P1394A）或线缆号（4S095-884）时优先用本工具；查不到再用 trace_connection。
        参数 query: 一个接插件号、线缆号或设备/基板名（如「J7544B」「4S095-884」「Robot Controller」）。"""
        def run():
            from src import acl
            if not acl.can_see(None, "circuit", "S207D"):   # 线表只有 S207D 的（data/netlist.db）
                return "（当前账号没有 S207D 电路图的查看权限，无法追线。）"
            from src.netlist_trace import trace as _trace
            return _trace(query, max_hops=2, max_lines=40)
        return _memo("netlist_trace", query, run)

    # LlamaIndex 用 loop.run_in_executor 跑同步工具，线程池不带 contextvars，
    # ACL 登录身份会丢（工具里报 no valid identity）。建工具时记下当前用户，执行时绑回去。
    from src import acl
    _user = acl.get_current_user()

    def _with_user(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            tok = acl.set_current_user(_user)
            try:
                return fn(*args, **kwargs)
            finally:
                acl.reset_current_user(tok)
        return wrapped

    fns = [error_code_lookup, keyword_search, semantic_search, fault_history_lookup,
           inspect_diagram, trace_connection, netlist_trace]
    if not _vision_available():
        fns.remove(inspect_diagram)   # 看图模型连不上时不给智能体，免得每次白白失败一步
    return [FunctionTool.from_defaults(fn=_with_user(f)) for f in fns]


_vision_state = {"t": 0.0, "ok": True}


def _vision_available() -> bool:
    """看图模型是否可用（结果缓存 5 分钟）。默认 ollama_local 指向本机 11434，服务器上没装就是不可用。"""
    import os, time
    if os.getenv("VISION_PROVIDER", "ollama_local") != "ollama_local":
        return True
    if time.time() - _vision_state["t"] < 300:
        return _vision_state["ok"]
    ok = False
    try:
        import httpx
        base = os.getenv("LLM_BASE_URL", "http://localhost:11434").rstrip("/")
        ok = httpx.get(f"{base}/api/tags", timeout=1.5, trust_env=False).status_code == 200
    except Exception:
        ok = False
    _vision_state.update(t=time.time(), ok=ok)
    return ok
