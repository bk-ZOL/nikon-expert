# src/router.py
# Nikon Expert — 智能路由层 (Karpathy + Gbrain + RAG 融合核心)
# 查询分类 → 多路召回 → 结果融合

import os
import re
from typing import Optional


# ── Nikon 常见 Error Code 模式 ──────────────────────────────────
# 注意：S 前缀单列——机型简写 S207/S307/S630 是「S+3位数字」，不能当错误码，
# 否则任何提到机型的问题都会被误判为 troubleshoot→走慢速多步 agent。
# 真 S 错误码要么带横杠(S-1234)要么 4-5 位数字(S12345)，均不与 3 位机型号冲突。
ERROR_CODE_RE = re.compile(
    r'\b([EWC]-?\d{3,5}|S-\d{3,5}|S\d{4,5}|P-\d{4,6}|ALM-\d{2,4}|SYS-\d{2,4})\b',
    re.IGNORECASE,
)

# ── 光刻机部件/子系统的常见名称 ─────────────────────────────────
COMPONENTS = [
    "stage", "reticle", "wafer", "lens", "projection",
    "alignment", "focus", "af", "illumination", "foc",
    "platen", "chuck", "robot", "loader", "coater",
    "developer", "barrier", "pellicle", "overlay", "cd",
    "scanner", "immersion", "ni", "wi", "lrl", "srl",
    "z-stage", "xy-stage", "tilt", "wafer-stage",
    "reticle-stage", "metrology", "sensor", "laser",
    "interferometer", "encoder", "motor", "servo",
    "vibration", "temperature", "flow", "vacuum",
    "gas", "slit", "aperture", "pupil", "aberration",
    "reduction", "stepper", "track", "esc", "ebr",
    "光刻", "对准", "聚焦", "镜头", "载台", "硅片", "掩膜",
    "投影", "照明", "曝光", "涂胶", "显影",
]

CONCEPT_KEYWORDS = [
    "原理", "为什么", "怎么", "如何", "什么是", "区别",
    "how", "why", "what", "explain", "difference",
    "机制", "作用", "功能", "含义", "意义",
]

# ── 材料/气体/介质词典 ───────────────────────────────────────────
# 这类"细节名词"常只在某份手册里一句话带过，整句检索时会被高频词（如 laser/stage）
# 淹没。抽出来做实体优先的定向 FTS，专治"资料里明明有却搜不到"。
MATERIALS = [
    "氦气", "氦", "helium",
    "氮气", "氮", "nitrogen",
    "氟气", "氟", "fluorine", "f2",
    "氩气", "氩", "argon",
    "氖", "neon",
    "氧气", "氧", "oxygen",
    "氢气", "氢", "hydrogen",
    "二氧化碳", "co2",
    "臭氧", "ozone",
    "cda", "压缩空气", "compressed air",
    "冷却水", "cooling water", "纯水", "di water",
    "真空", "vacuum",
    "氨", "ammonia",
    "润滑脂", "油脂", "grease", "lubrican",
    "冷媒", "制冷剂", "coolant",
]

# 电路图类查询关键词：命中则不降权 circuit_diagram（用户就是要查图）
CIRCUIT_KEYWORDS = [
    "电路", "接线", "线号", "端子", "配线", "结线", "板卡", "图纸", "连接器",
    "circuit", "wiring", "diagram", "schematic", "connector", "pin", "harness",
]


# 跨语言同义扩展：中文问法也去搜英文原文（三语库的系统性召回缺口）。
# 只列"完整词"，不列 he/n2/f2 这类超短词（trigram 会误召回一堆）。
MATERIAL_SYNONYMS = {
    "氦气": ["helium"], "氦": ["helium"], "helium": ["氦气", "氦"],
    "氮气": ["nitrogen"], "氮": ["nitrogen"], "nitrogen": ["氮气", "氮"],
    "氟气": ["fluorine"], "氟": ["fluorine"], "fluorine": ["氟气", "氟"],
    "氩气": ["argon"], "氩": ["argon"], "argon": ["氩气", "氩"],
    "氖": ["neon"], "neon": ["氖"],
    "氧气": ["oxygen"], "氧": ["oxygen"], "oxygen": ["氧气", "氧"],
    "氢气": ["hydrogen"], "氢": ["hydrogen"], "hydrogen": ["氢气", "氢"],
    "臭氧": ["ozone"], "ozone": ["臭氧"],
    "压缩空气": ["compressed air", "cda"], "cda": ["compressed air", "压缩空气"],
    "冷却水": ["cooling water"], "cooling water": ["冷却水"],
    "真空": ["vacuum"], "vacuum": ["真空"],
    "氨": ["ammonia"], "ammonia": ["氨"],
    "润滑脂": ["grease"], "油脂": ["grease"], "grease": ["润滑脂", "油脂"],
    "冷媒": ["coolant"], "制冷剂": ["coolant"], "coolant": ["冷媒", "制冷剂"],
}


def extract_key_terms(question: str) -> list:
    """抽取问题里的显著领域名词（材料/气体在前、部件在后）+ 跨语言同义词，
    用于实体优先的定向 FTS。去重保序；材料词优先，保证细节名词一定被单独检索到。"""
    q = question.lower()
    out, seen = [], set()

    def _add(t):
        tl = t.lower()
        if len(t) >= 2 and tl not in seen:
            seen.add(tl)
            out.append(t)

    for t in MATERIALS + COMPONENTS:
        if len(t) >= 2 and t.lower() in q:
            _add(t)
            for syn in MATERIAL_SYNONYMS.get(t.lower(), []):
                _add(syn)
    # 英文缩写/子系统名（SMIF/OHT/FOUP/RSP/AIS/WLL…）：问题里的全大写 token 多是
    # 关键技术名，整句检索易被稀释，单独定向检索。排除已单独处理的 Error Code。
    for tok in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", question):
        if not ERROR_CODE_RE.match(tok):
            _add(tok)
    return out


def is_circuit_query(question: str) -> bool:
    """是否明确在查电路/接线图（此时不压制 circuit_diagram）。"""
    q = question.lower()
    return any(k in q for k in CIRCUIT_KEYWORDS)


def classify_query(question: str) -> str:
    """
    分类查询类型：
    - exact: 含 Error Code，优先精确匹配
    - component: 含部件名称
    - concept: 概念性问题
    - hybrid: 默认混合
    """
    q = question.lower()

    if ERROR_CODE_RE.search(q):
        return "exact"

    for comp in COMPONENTS:
        if comp.lower() in q:
            return "component"

    for kw in CONCEPT_KEYWORDS:
        if kw in q:
            return "concept"

    return "hybrid"


TROUBLESHOOT_KEYWORDS = [
    "报警", "故障", "报错", "异常", "错误", "报警",
    "排查", "修复", "处理", "解决", "怎么处理", "怎么办",
    "报警", "停机", "卡死", "超时", "失败",
    "alarm", "fault", "error", "fail", "troubleshoot",
    "warning", "timeout", "crash", "broken",
]


def auto_mode(question: str) -> str:
    """自动判断使用 qa 还是 troubleshoot 模式"""
    q = question.lower()
    if ERROR_CODE_RE.search(q):
        return "troubleshoot"
    for kw in TROUBLESHOOT_KEYWORDS:
        if kw in q:
            return "troubleshoot"
    return "qa"


_INTENT_PROMPT = """你是光刻机知识库的查询分流器。判断工程师的这个问题属于哪一类，只回一个英文单词，不要解释：

- lookup：查一个事实/参数/规格/定义/名称，一步检索就能答（如"S207 高度是多少""GOCS 是什么""传送是不是 SMIF""某参数标准值"）。
- troubleshoot：描述了故障/报警/异常/失败现象，需要多步推理排查（如"报 E-5301 怎么办""对准精度不达标""WL 传送失败""某动作卡住"），或需要跨多份资料综合的复合问题。

问题：{q}

答（只回 lookup 或 troubleshoot）："""


def _semantic_use_agent(question: str) -> Optional[bool]:
    """用 LLM 语义判断是否该走多步 agent。返回 True/False；不可用则 None（交给兜底）。"""
    try:
        from llama_index.core import Settings
        if Settings.llm is None:
            return None
        resp = str(Settings.llm.complete(_INTENT_PROMPT.format(q=question[:300]))).strip().lower()
        if "troubleshoot" in resp or "排查" in resp:
            return True
        if "lookup" in resp or "查" in resp:
            return False
        return None
    except Exception:
        return None


def _keyword_use_agent(question: str, mode: Optional[str] = None) -> bool:
    """关键词/规则兜底（语义不可用时）。"""
    m = mode or auto_mode(question)
    if m == "troubleshoot":
        return True
    if (question.count("？") + question.count("?")) >= 2:
        return True
    return False


def should_use_agent(question: str, mode: Optional[str] = None) -> bool:
    """判断走多步 Agent 排障还是单次 RAG 快路径。

    默认**语义路由**（LLM 按应用场景判断，避免关键词误判——如机型号 S207 被当错误码）；
    语义不可用时回退关键词规则。环境变量：
      AGENT_ENABLED=false  → 一律快路径（关闭 agent）
      AGENT_ALWAYS=true    → 一律 agent（调试用）
      AGENT_ROUTER=keyword → 强制用关键词规则（不调 LLM，省一次调用）
    """
    if os.getenv("AGENT_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    if os.getenv("AGENT_ALWAYS", "false").lower() in ("1", "true", "yes"):
        return True

    if os.getenv("AGENT_ROUTER", "semantic").lower() != "keyword":
        sem = _semantic_use_agent(question)
        if sem is not None:
            return sem
    return _keyword_use_agent(question, mode)


def extract_error_codes(question: str) -> list:
    """提取查询中的 Error Code"""
    return ERROR_CODE_RE.findall(question)


def extract_machine_models(text: str) -> list:
    """从文本中提取 Nikon 机台型号"""
    patterns = [
        r'\b(NSR-S[6]\d{2}[CDL]?|NSR-SF[6]?\d{2}|NSR-T[6]?\d{2})\b',
        r'\b(FX-\d{3,4}[A-Z]?)\b',
        r'\b(ARF[i]?|KrF|EUV|i[line]?)\b',
    ]
    models = []
    for pat in patterns:
        models.extend(re.findall(pat, text, re.IGNORECASE))
    return list(set(m.upper() for m in models))


def get_route_weights(query_type: str) -> tuple:
    """返回 (FTS权重, 语义权重)"""
    weights = {
        "exact":     (0.7, 0.3),
        "component": (0.5, 0.5),
        "concept":   (0.3, 0.7),
        "hybrid":    (0.5, 0.5),
    }
    return weights.get(query_type, (0.5, 0.5))


def merge_results(
    fts_results: list,
    semantic_results: list,
    fts_weight: float = 0.5,
    semantic_weight: float = 0.5,
    top_n: int = 6,
    demote_circuit: bool = False,
    circuit_cap: int = 2,
    circuit_penalty: float = 0.25,
    rrf_k: int = 60,
) -> list:
    """
    Hybrid 融合：Reciprocal Rank Fusion (RRF)。

    比"分数归一化加权"更稳——只看两路各自的**排名**，与 BM25/余弦分数的尺度无关，
    不会因某一路分数量纲大而压倒另一路。RRF 贡献 = weight × 1/(rrf_k + rank)，
    同一文档两路都命中则相加（自然加权）。rrf_k 越大，头部名次差异越平缓（标准取 60）。

    入参两路均已按相关性**从好到差**排好（FTS 按 bm25 ORDER BY、语义按检索器分数）。
    返回: [{"doc_id","doc_name","doc_type","machine_model","section_title",
            "text","score","source","file_path","page_start"}, ...]
    """
    merged = {}

    def _key(doc_id, section, page):
        return (doc_id, section or page)

    # ── FTS 路：按名次给 RRF 分（列表内去重，取最佳名次；实体多路检索可能重复）
    seen = set()
    rank = 0
    for r in fts_results:
        key = _key(r.get("doc_id", ""), r.get("section_title", ""), r.get("page_start", ""))
        if key in seen:
            continue
        seen.add(key)
        contrib = fts_weight * (1.0 / (rrf_k + rank))
        rank += 1
        if key in merged:
            merged[key]["score"] += contrib
            merged[key]["source"] = "fts+semantic"
        else:
            merged[key] = {
                "doc_id": r.get("doc_id", ""), "doc_name": r.get("doc_name", ""),
                "doc_type": r.get("doc_type", ""), "machine_model": r.get("machine_model", ""),
                "section_title": r.get("section_title", ""), "text": r.get("text", ""),
                "file_path": r.get("file_path", ""), "page_start": r.get("page_start", ""),
                "source": "fts", "score": contrib,
            }

    # ── 语义路：同理
    seen = set()
    rank = 0
    for node in semantic_results:
        m = node.metadata or {}
        key = _key(m.get("doc_id", ""), m.get("section_title", ""), m.get("page_start", ""))
        if key in seen:
            continue
        seen.add(key)
        contrib = semantic_weight * (1.0 / (rrf_k + rank))
        rank += 1
        if key in merged:
            merged[key]["score"] += contrib
            merged[key]["source"] = "fts+semantic"
        else:
            merged[key] = {
                "doc_id": m.get("doc_id", ""), "doc_name": m.get("doc_name", ""),
                "doc_type": m.get("doc_type", ""), "machine_model": m.get("machine_model", ""),
                "section_title": m.get("section_title", ""), "text": node.get_content(),
                "file_path": m.get("file_path", ""), "page_start": m.get("page_start", ""),
                "source": "semantic", "score": contrib,
            }

    # ── 非电路类问题：压制逐页电路图（否则含 laser/stage 的问题会被图纸刷屏，
    #    把只在手册里一句话的答案挤出候选集）。降权 + 硬限额双保险。
    if demote_circuit:
        for e in merged.values():
            if e.get("doc_type") == "circuit_diagram":
                e["score"] *= circuit_penalty

    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

    if demote_circuit:
        out, n_circ = [], 0
        for e in ranked:
            if e.get("doc_type") == "circuit_diagram":
                if n_circ >= circuit_cap:
                    continue          # top_n 里最多留 circuit_cap 个电路图
                n_circ += 1
            out.append(e)
            if len(out) >= top_n:
                break
        return out

    return ranked[:top_n]
