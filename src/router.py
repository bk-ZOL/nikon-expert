# src/router.py
# Nikon Expert — 智能路由层 (Karpathy + Gbrain + RAG 融合核心)
# 查询分类 → 多路召回 → 结果融合

import os
import re
from typing import Optional


# ── Nikon 常见 Error Code 模式 ──────────────────────────────────
ERROR_CODE_RE = re.compile(
    r'\b([EWCS]-?\d{3,5}|P-\d{4,6}|ALM-\d{2,4}|SYS-\d{2,4})\b',
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


def should_use_agent(question: str, mode: Optional[str] = None) -> bool:
    """判断该走多步 Agent 排障，还是走单次 RAG 快路径。

    Agent 慢但会自主多步取证，适合故障排查 / 复合问题；
    简单概念/事实查询走快路径即可。可用环境变量覆盖：
      AGENT_ENABLED=false  → 一律走快路径（关闭 agent）
      AGENT_ALWAYS=true    → 一律走 agent（调试用）
    """
    if os.getenv("AGENT_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    if os.getenv("AGENT_ALWAYS", "false").lower() in ("1", "true", "yes"):
        return True

    m = mode or auto_mode(question)
    # 故障排查类：多步取证收益最大
    if m == "troubleshoot":
        return True
    # 复合问题（多个子问句）：需要跨资料综合
    if (question.count("？") + question.count("?")) >= 2:
        return True
    return False


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
) -> list:
    """
    融合 FTS 和语义检索结果。

    两者结果格式各自为:
    - FTS: [{"doc_id", "doc_name", "text", "score", ...}]
    - Semantic: llama-index NodeWithScore objects (有 .score, .metadata, .get_content())

    返回: 合并排序后的列表，每个元素是 dict:
    {"doc_id", "doc_name", "doc_type", "machine_model", "section_title",
     "text", "score", "source", "file_path", "page_start", ...}
    """
    merged = {}

    # ── 归一化 FTS 分数（BM25 负值，取绝对值后归一化）
    fts_scores = [abs(r.get("score", 0)) for r in fts_results]
    fts_max = max(fts_scores) if fts_scores else 1.0

    for r in fts_results:
        key = (r.get("doc_id", ""), r.get("section_title", "") or r.get("page_start", ""))
        norm_score = (abs(r.get("score", 0)) / fts_max) * fts_weight if fts_max > 0 else 0
        entry = {
            "doc_id": r.get("doc_id", ""),
            "doc_name": r.get("doc_name", ""),
            "doc_type": r.get("doc_type", ""),
            "machine_model": r.get("machine_model", ""),
            "section_title": r.get("section_title", ""),
            "text": r.get("text", ""),
            "file_path": r.get("file_path", ""),
            "page_start": r.get("page_start", ""),
            "source": "fts",
        }
        if key in merged:
            merged[key]["score"] += norm_score
            if "semantic" not in merged[key]["source"]:
                merged[key]["source"] = "fts+semantic"
        else:
            entry["score"] = norm_score
            merged[key] = entry

    # ── 归一化语义分数
    sem_scores = [n.score or 0.0 for n in semantic_results]
    sem_max = max(sem_scores) if sem_scores else 1.0

    for node in semantic_results:
        m = node.metadata or {}
        key = (m.get("doc_id", ""), m.get("section_title", "") or m.get("page_start", ""))
        norm_score = ((node.score or 0.0) / sem_max) * semantic_weight if sem_max > 0 else 0
        entry = {
            "doc_id": m.get("doc_id", ""),
            "doc_name": m.get("doc_name", ""),
            "doc_type": m.get("doc_type", ""),
            "machine_model": m.get("machine_model", ""),
            "section_title": m.get("section_title", ""),
            "text": node.get_content(),
            "file_path": m.get("file_path", ""),
            "page_start": m.get("page_start", ""),
            "source": "semantic",
        }
        if key in merged:
            merged[key]["score"] += norm_score
            if "fts" not in merged[key]["source"]:
                merged[key]["source"] = "fts+semantic"
        else:
            entry["score"] = norm_score
            merged[key] = entry

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
