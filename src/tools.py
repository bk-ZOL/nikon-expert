# src/tools.py
# Nikon Expert — Agent 工具箱（P1）
# 把现有检索能力封装成 ReAct agent 可调用的 FunctionTool。
# 每个工具在返回给 LLM 文本的同时，把命中的资料记入 SourceCollector，
# 供最终答案统一引用（与 engine._build_context 的 citations_data 结构对齐）。

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

    def citations(self) -> list:
        """返回展示用引用字符串列表 [ '[1] xxx', ... ]"""
        return [f"[{i}] {self._cite_text(r)}" for i, r in enumerate(self._items, 1)]

    def citations_data(self) -> list:
        """返回结构化引用，供 UI 构建可点击按钮（与 engine 对齐）。"""
        out = []
        for i, r in enumerate(self._items, 1):
            page = r.get("page_start", "")
            out.append({
                "index": i,
                "doc_name": r.get("doc_name", ""),
                "doc_type": r.get("doc_type", ""),
                "file_path": r.get("file_path", ""),
                "page": int(page) if str(page).isdigit() else None,
                "section": r.get("section_title", ""),
                "source": r.get("_tool", ""),
                "cite_text": self._cite_text(r),
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

    def _memo(name, arg, run):
        key = (name, (arg or "").strip().lower())
        if key in _call_cache:
            return (f"（你本轮已用相同参数调用过 {name}，结果同上，请不要重复调用；"
                    f"若信息已足够就直接给出最终结论。）")
        result = run()
        _call_cache[key] = True
        return result

    def error_code_lookup(code: str) -> str:
        """精确查询某个 Nikon Error Code（如 E-5301、P-12345、ALM-04）的原厂记录与含义。
        当故障描述中出现明确的报警码 / error code 时，优先用本工具。
        参数 code: 报警码字符串。"""
        def run():
            results = search_fts_exact(code.strip(), limit=6)
            collector.add(results, tool="error_code_lookup")
            return _format(results, empty=f"知识库中未找到 {code} 的记录。")
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
                nodes = retriever.retrieve(query)
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
        return _memo("inspect_diagram", query, run)

    def trace_connection(query: str) -> str:
        """连接追踪：确认某元件/传感器/信号"连到哪、用哪根线、哪个 pin、到哪个面板/板卡"。
        它定位接线表页、抽出线号(如 2613C)与 pin(如 PS1)，并按线号交叉引用到其它页。
        当需要确认元件之间的电气连接关系时用本工具。
        参数 query: 元件/信号名或线号（用图纸术语，如「YF軸ソフトリミット」「2613C」）。"""
        def run():
            from src.net_trace import trace_connection as _trace
            return _trace(query)
        return _memo("trace_connection", query, run)

    return [
        FunctionTool.from_defaults(fn=error_code_lookup),
        FunctionTool.from_defaults(fn=keyword_search),
        FunctionTool.from_defaults(fn=semantic_search),
        FunctionTool.from_defaults(fn=fault_history_lookup),
        FunctionTool.from_defaults(fn=inspect_diagram),
        FunctionTool.from_defaults(fn=trace_connection),
    ]
