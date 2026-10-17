"""src/kb_extract.py —— 夜间提炼（2026-10-17）：用 Mac 上的本地模型，从资料里提炼故障排查和 SOP 分类。

三件活，都可以中断，下次接着做（做过的窗口按内容哈希记在 ts_table.db）：
  1. 手册 / 日文 checksheet 里「异常 → 原因 / 处理 / 判定标准」的段落 → Troubleshooting 表的原厂条目（待确认）
  2. 现场履历 / 工作日志 / 客户报告 → Troubleshooting 表的现场案例（原来每天用 deepseek 抽，改成本地）
  3. SOP 目录里的流程按模块（Unit）分类，SOP 速查可以按模块筛
全部只在 Mac 在线且空闲时跑（src/local_llm.py），由 nikon-kb-extract.timer 每天夜里启动，到点就停。
"""
import hashlib
import json
import os
import re
import time

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from src import local_llm, ts_table

WINDOW = 2600
MIN_SCORE = int(os.getenv("KB_MIN_SCORE", "3"))          # 窗口里「异常 → 处理」写法出现几次以上才送去抽
SRC_TYPES = ("manual", "checksheet")

STRONG = re.compile(
    r"if [^.\n]{0,80}?(?:fail|does not|do not|cannot|can not|is not|are not|out of|exceed|error|abnormal|incorrect|not within)"
    r"|\bcause[sd]?\b|remedy|countermeasure|corrective|troubleshoot|contact nikon|error message"
    r"|規格外|異常|対策|要因|原因|不良|エラー|出ない場合|しない場合|できない場合|ない場合は|相談|連絡", re.I)
SKIP_SEC = ("ATTENTION", "IMPORTANT NOTICE", "Warning Conventions", "Safety", "SAFETY", "Preface", "Contents",
            "Warranty", "目次", "安全")

OEM_PROMPT = """你在整理 Nikon 光刻机的故障排查知识。下面是一段原厂资料原文（来源：{doc}，机型：{machine}，可能是英文或日文）。
找出原文里**明确写了**「某个异常 / 报警 / 检查不合格（規格外）→ 原因 和/或 处理办法」的内容，整理成 JSON 数组。

规则：
1. 只用原文内容，不补充常识；只有正常操作步骤、没写异常时怎么办的，不要抽，返回 []。
2. 「symptom」「causes」「actions」用中文写；按钮名、菜单名、参数名、报警原文、数值、单位保持原文。
3. actions 按原文顺序，一步一条；原文写「联系 Nikon / F・L / 担当技術」也照写。
4. unit 只能从这里选一个：{units}
5. 原文写的是前提条件（如「社内工程の場合」「納入時のみ」）放进 actions 那一步里，不要当成原因。
6. 处理办法只是一个编号或引用（如「Countermeasure 8」「参照 4.3 节」「→ Table 5」），而这段原文里没写具体做法的，**不要抽这一条**。
7. 这份资料主要讲：{hint}。拿不准 unit 时按这个选。

每个元素：{{"unit": "…", "symptom": "异常现象或报警，一句话", "error_code": "报警码（原文有才写）",
 "causes": ["原因1", …], "actions": ["处理/确认步骤1", …], "criteria": "判定标准/规格值（原文有才写）",
 "section": "原文章节标题或页码"}}
只输出 JSON 数组，不要解释。

原文：
{text}
"""

UNIT_PROMPT = """下面是 Nikon 光刻机（NSR-S207D 等）手册里的操作流程，给每个流程标出它属于哪个模块。
模块只能从这里选（原样写，不要组合、不要自造）：{units}。判断不了选「其他」。
只输出 JSON 对象：{{"编号": "模块", …}}（编号只写数字），不要解释。

{items}
"""


_UNIT_HINTS = [  # 资料名里的关键词 → 这份资料主要讲的模块（给模型一个默认）
    (r"laser|excimer|cymer|gigaphoton", "Laser"), (r"avis", "AVIS"), (r"wafer loader|\bwl\b", "WL"),
    (r"reticle loader|\brl\b|smif|pod", "RL"), (r"inline|foup|oht", "Inline"),
    (r"temperature controller|chamber|atc|ffu|空調", "Chamber"), (r"lens control|\blc\b", "LC"),
    (r"interferometer|干渉計", "干涉计"), (r"illumination|照明", "Illumination"), (r"\bbmu\b", "BMU"),
    (r"alignment|fia|lsa|\balg\b", "ALG"), (r"\bais\b", "AIS"), (r"focus|\baf\b", "AF"),
    (r"stage|\bws\b|\brs\b", "WS"), (r"mcsv|software|operation guide|command", "主机/PC"),
]


def _hint(doc: str) -> str:
    for pat, unit in _UNIT_HINTS:
        if re.search(pat, doc, re.I):
            return unit
    return "看原文判断"


def _conn():
    c = ts_table._conn()
    c.execute("CREATE TABLE IF NOT EXISTS kb_windows (hash TEXT PRIMARY KEY, kind TEXT, src_doc TEXT, n INTEGER, done_at TEXT)")
    return c


def _models(fts):
    out = {}
    for name, models in fts.execute("SELECT filename, MAX(models) FROM doc_acl GROUP BY filename"):
        fam = next((m for m in (models or "").split(",") if m and m not in ("ALL", "COMMON")), "")
        out[name] = fam or "S207D"
    return out


_PRIO = {"S207D": 0, "COMMON": 1, "S206D": 2}
# 专门讲故障的资料（エラー要因分析、REPAIR MANUAL、トラブル要因シート、Troubleshooting Guide）最有用，不分机型最先做
_TROUBLE_DOC = re.compile(r"トラブル|要因|repair|troubleshoot|故障|エラー|error handling", re.I)


def oem_windows():
    """[(doc, machine, text, section, score)]：先做专门讲故障的资料（不分机型），再按 S207D → 通用 → S206D → 其他机型；
    同一档里「异常 → 处理」写法越密越先做。"""
    wins = list(_oem_windows_raw())
    wins.sort(key=lambda w: (0 if _TROUBLE_DOC.search(w[0]) else 1, _PRIO.get(w[1], 3), -w[4]))
    return wins


def _oem_windows_raw():
    fts = sqlite3.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
    models = _models(fts)
    names = [r[0] for r in fts.execute(
        f"SELECT DISTINCT doc_name FROM doc_fts WHERE doc_type IN ({','.join('?' * len(SRC_TYPES))})", SRC_TYPES)]
    for name in names:
        rows = fts.execute("SELECT section_title, text FROM doc_fts WHERE doc_name=? ORDER BY rowid", (name,)).fetchall()
        skip_head = int(len(rows) * 0.05)                 # 前 5% 是封面、目录、声明
        buf, first = "", ""
        for i, (sec, text) in enumerate(rows):
            if i < skip_head or any(k in (sec or "") for k in SKIP_SEC):
                continue
            piece = f"\n## {sec}\n{text}"
            if buf and len(buf) + len(piece) > WINDOW:
                sc = len(STRONG.findall(buf))
                if sc >= MIN_SCORE:
                    yield name, models.get(name, "S207D"), buf[:WINDOW], first, sc
                buf = ""
            if not buf:
                first = (sec or "").strip("# *")[:60]
            buf += piece
        if buf and len(STRONG.findall(buf)) >= MIN_SCORE:
            yield name, models.get(name, "S207D"), buf[:WINDOW], first, len(STRONG.findall(buf))
    fts.close()


_REF_RE = re.compile(r"^\s*(?:countermeasure|対策|処置|remedy|action)\s*(?:no\.?\s*)?(\d{1,3})\s*$", re.I)
_ONLY_REF_RE = re.compile(r"^\s*(?:see|refer to|参照|参考|→)?\s*(?:countermeasure|対策|section|table|表|chapter|第)?\s*"
                          r"[\d.\-]+\s*(?:节|章|節|項)?\s*(?:を?参照)?\s*$", re.I)
_DEF_RE = re.compile(r"(?:Countermeasure|対策|処置)\s*(?:No\.?\s*)?(\d{1,3})\**\s*\|\s*\**([^|*]{0,80}?)\**\s*\|"
                     r"[\s|]*\|?\s*([^|]{8,600})\|", re.I)
_REFS = {}


def _doc_refs(doc: str) -> dict:
    """这份资料里「Countermeasure N → 标题、内容」的对照（如激光器手册的 Table 4.4-2 Countermeasures）。"""
    if doc not in _REFS:
        fts = sqlite3.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
        refs = {}
        for (t,) in fts.execute("SELECT text FROM doc_fts WHERE doc_name=? AND (text LIKE '%ountermeasure%' "
                                "OR text LIKE '%対策%' OR text LIKE '%処置%')", (doc,)):
            for m in _DEF_RE.finditer((t or "").replace("<br>", " ")):
                body = re.sub(r"\s+", " ", m.group(3)).strip()
                if len(body) >= 8 and m.group(1) not in refs:
                    refs[m.group(1)] = (m.group(2).strip(), body)
        fts.close()
        _REFS[doc] = refs
    return _REFS[doc]


def _resolve_actions(doc: str, actions: list) -> list:
    """只写了「Countermeasure 8」的步骤换成资料里写的具体内容；换不了、又只是个引用的，丢掉。"""
    refs, out = _doc_refs(doc), []
    for a in actions:
        m = _REF_RE.match(a)
        if m and m.group(1) in refs:
            title, body = refs[m.group(1)]
            out.append(f"{a}（{title}）：{body}" if title else f"{a}：{body}")
        elif not _ONLY_REF_RE.match(a):
            out.append(a)
    return out


def _add_oem(c, doc, machine, item) -> bool:
    """一条抽取结果 → 原厂条目（status=pending，管理员确认后才给别人看、才被智能体引用）。
    处理步骤只是引用编号、又查不到内容，而且没写原因的，不收（空壳条目没用）。"""
    sym = (item.get("symptom") or "").strip()
    if not sym:
        return False
    unit = item.get("unit") if item.get("unit") in ts_table.UNITS else "其他"
    if unit == "其他" and _hint(doc) in ts_table.UNITS:
        unit = _hint(doc)                  # 模型拿不准时按资料名定（激光器手册 → Laser）
    code = ts_table.norm_code(item.get("error_code") or "")
    causes = [str(x).strip() for x in item.get("causes") or [] if str(x).strip()]
    actions = _resolve_actions(doc, [str(x).strip() for x in item.get("actions") or [] if str(x).strip()])
    crit = (item.get("criteria") or "").strip()
    if not causes and not actions and not crit:
        return False
    section = (item.get("section") or "").strip()
    steps = [{"check": a, "details": ""} for a in actions]
    if crit:
        steps.append({"check": f"判定标准：{crit}", "details": ""})
    detail = [{"rate": i + 1, "cause": cz, "steps": steps if i == 0 else []} for i, cz in enumerate(causes)] \
        or [{"rate": 1, "cause": "（资料里没写原因）", "steps": steps}]
    rcs = [{"cause": cz, "category": "", "count": i + 1} for i, cz in enumerate(causes)]
    sols = [{"action": a, "count": 0} for a in actions]
    key = "oemx|" + hashlib.md5(f"{doc}|{machine}|{unit}|{code}|{ts_table._norm_text(sym)[:60]}".encode()).hexdigest()
    src = f"{doc}" + (f" ｜ {section}" if section else "")
    c.execute("""INSERT INTO ts_entries(entry_key, machine, unit, error_code, title, root_causes, solutions,
                     origin, oem_detail, src, match_key, status, occ_count, updated_at)
                 VALUES (?,?,?,?,?,?,?, 'oem', ?,?,?, 'pending', 0, ?)
                 ON CONFLICT(entry_key) DO UPDATE SET root_causes=excluded.root_causes, solutions=excluded.solutions,
                     oem_detail=excluded.oem_detail, src=excluded.src, updated_at=excluded.updated_at
                 WHERE ts_entries.edited = 0""",
              (key, machine, unit, code, sym, json.dumps(rcs, ensure_ascii=False), json.dumps(sols, ensure_ascii=False),
               json.dumps(detail, ensure_ascii=False), src, ts_table.match_key(code), ts_table._now()))
    return True


def run_oem(deadline: float, log=print) -> dict:
    c = _conn()
    st = {"windows": 0, "items": 0, "skipped": 0}
    for doc, machine, text, first, score in oem_windows():
        if time.time() > deadline:
            break
        h = hashlib.md5(("oem" + doc + text).encode()).hexdigest()
        if c.execute("SELECT 1 FROM kb_windows WHERE hash=?", (h,)).fetchone():
            st["skipped"] += 1
            continue
        ok, why = local_llm.available()
        if not ok:
            log(f"  ⏸  {why}，先停")
            st["paused"] = why
            break
        try:
            out = ts_table._parse_json(local_llm.chat(OEM_PROMPT.format(
                doc=doc, machine=machine, units="、".join(ts_table.UNITS), hint=_hint(doc), text=text)))
        except Exception as ex:
            log(f"  ⚠️ {doc[:40]} 抽取失败：{type(ex).__name__}: {ex}")
            continue
        n = sum(1 for it in (out if isinstance(out, list) else []) if isinstance(it, dict) and _add_oem(c, doc, machine, it))
        c.execute("INSERT OR REPLACE INTO kb_windows VALUES (?,?,?,?,?)", (h, "oem", doc, n, ts_table._now()))
        c.commit()
        st["windows"] += 1
        st["items"] += n
        log(f"  {doc[:40]:40s} {first[:24]:24s} 得分 {score:2d} → {n} 条（{local_llm.chat.last.get('sec')}s）")
    c.close()
    return st


def run_field(deadline: float, log=print) -> dict:
    """现场履历 / 日志 / 客户报告：沿用 ts_table 的抽取（提示词、去重、归并都不变），只是模型换成本地。"""
    os.environ["TS_LLM"] = "local"
    st = {"windows": 0, "cases": 0}
    while time.time() < deadline:
        ok, why = local_llm.available()
        if not ok:
            log(f"  ⏸  {why}，先停")
            st["paused"] = why
            break
        r = ts_table.run(limit=5, log=log)               # 一次 5 个窗口，做完再看一眼 Mac 还空不空
        st["windows"] += r["windows"]
        st["cases"] += r["cases"]
        if not r["windows"]:
            break
    return st


def _unit_table(sc):
    sc.execute("CREATE TABLE IF NOT EXISTS proc_units (group_key TEXT PRIMARY KEY, unit TEXT, src TEXT, done_at TEXT)")


def run_units(deadline: float, batch: int = 30, log=print) -> dict:
    """SOP 目录的流程按模块分类，按 group_key 记（重建目录也不丢；同一流程的多个版本只分一次）。"""
    from src import sop_index
    sc = sqlite3.connect(sop_index.DB)
    sc.row_factory = sqlite3.Row
    _unit_table(sc)
    st = {"batches": 0, "classified": 0}
    while time.time() < deadline:
        rows = sc.execute("""SELECT p.id, p.group_key, p.title, p.breadcrumb, p.steps FROM procedures p
                             LEFT JOIN proc_units u ON u.group_key = p.group_key
                             WHERE u.group_key IS NULL GROUP BY p.group_key LIMIT ?""", (batch,)).fetchall()
        if not rows:
            break
        ok, why = local_llm.available()
        if not ok:
            log(f"  ⏸  {why}，先停")
            st["paused"] = why
            break
        items = "\n".join(f"[{r['id']}] {(r['breadcrumb'] + ' › ') if r['breadcrumb'] else ''}{r['title']}："
                          + "；".join(x["text"] for x in json.loads(r["steps"])[:3])[:200] for r in rows)
        try:
            raw = local_llm.chat(UNIT_PROMPT.format(units="、".join(ts_table.UNITS), items=items))
        except Exception as ex:
            log(f"  ⚠️ 分类失败：{type(ex).__name__}: {ex}")
            break
        got = {m.group(1): m.group(2) for m in re.finditer(r'"\[?(\d+)\]?[^"]*"\s*:\s*"([^"]+)"', raw)}
        for r in rows:
            u = got.get(str(r["id"]), "")
            u = u if u in ts_table.UNITS else "其他"
            sc.execute("INSERT OR REPLACE INTO proc_units VALUES (?,?,?,?)", (r["group_key"], u, "local", ts_table._now()))
        sc.commit()
        st["batches"] += 1
        st["classified"] += len(rows)
        log(f"  模块分类 {len(rows)} 个（{local_llm.chat.last.get('sec')}s）")
    sc.close()
    return st


def status() -> dict:
    c = _conn()
    done = dict(c.execute("SELECT kind, COUNT(*) FROM kb_windows GROUP BY kind").fetchall())
    pend = c.execute("SELECT COUNT(*) FROM ts_entries WHERE origin='oem' AND status='pending' AND entry_key LIKE 'oemx|%'").fetchone()[0]
    c.close()
    return {"oem_windows_done": done.get("oem", 0), "oem_items_pending": pend}
