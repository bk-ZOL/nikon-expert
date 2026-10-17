"""src/glossary.py —— 缩写对照表（2026-10-17）。

问题：现场问「RR 怎么测」，库里的 RR 写的是 Reticle Rotation / レチクルローテーション / 掩模旋转，
而 Error Code List 里还有几百处机械手的「RR-axis」，原样检索 RR 找不到要的东西。

对照表来源（同一个缩写可以有几种意思）：
  1. 人工维护的 config/glossary.tsv（优先，可以写多语和说明）
  2. 资料里现成的对照表（「缩写 | 全称 | 中文」「Abbreviation | Description」这类 markdown 表格）
  3. 正文里的「Reticle Rotation (RR)」「RR (Reticle Rotation)」「レチクルローテーション（RR）」写法，
     英文全称要能和缩写的首字母对上才算；出现 2 次以上才收
建库：scripts/build_glossary.py（读全文索引，写 data/glossary.json），WPS / 飞书同步有变化时自动重建。
查询时：expand(问题) 找出问题里认识的缩写，给检索补上全称，给智能体一句术语提示。
"""
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv("GLOSSARY_DB", str(ROOT / "data" / "glossary.json")))
MANUAL = ROOT / "config" / "glossary.tsv"

ABBR = r"[A-Z][A-Z0-9]{1,6}"
_STOP = {"of", "the", "and", "for", "to", "in", "on", "by", "with", "a", "an", "&", "-"}
# 太常见、展开没意义或容易误配的
_SKIP = {"NO", "OK", "NG", "ON", "OFF", "PC", "ID", "IP", "OS", "PP", "SW", "UP", "PDF", "USB", "LED", "AC", "DC",
         "NOTE", "STEP", "MAX", "MIN", "TBD", "NA", "VS", "FIG", "TBL", "OR", "AND", "THE", "IN", "IS", "IT"}
_COMMON_WORDS = {"set", "run", "log", "get", "put", "add", "end", "top", "low", "out", "all", "can", "not", "use", "via",
                 "per", "new", "old", "one", "two", "how", "why", "who", "and", "the", "for", "you", "are", "was"}
_KANA = r"゠-ヿｦ-ﾟ"
_CJK = r"一-鿿"


# ── 1. 挖掘 ─────────────────────────────────────────────────────────
def _initials_ok(abbr: str, words: list) -> bool:
    """全称的首字母（跳过 of/the…，一个词可以贡献开头连续几个字母）能拼出缩写。"""
    a = abbr.lower()
    ws = [w.lower() for w in words if w.lower() not in _STOP and w]

    def match(i, j):
        if i == len(a):
            return j == len(ws)
        if j == len(ws):
            return False
        w = ws[j]
        # 这个词贡献 1..k 个字母（如 FIA = Field Image Alignment，ILM = Interferometric Lens Measurement）
        for k in range(1, min(len(w), len(a) - i) + 1):
            if w[:k] == a[i:i + k] and match(i + k, j + 1):
                return True
        # 数字缩写里的数字可以对应一个词（如 L2 …）—— 不处理，宁缺毋滥
        return False
    return match(0, 0)


def _best_suffix(abbr: str, phrase: str):
    """「… Reticle Rotation (RR)」：从括号前的词里找最短的、首字母能对上的结尾。"""
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", phrase)[-8:]
    for k in range(1, len(words) + 1):
        cand = words[-k:]
        if cand[0].lower() in _STOP:
            continue
        if _initials_ok(abbr, cand):
            return " ".join(cand)
    return None


_EN_BEFORE = re.compile(rf"((?:[A-Za-z][A-Za-z\-]*[ \t]+){{0,7}}[A-Za-z][A-Za-z\-]*)[ \t]*[(（][ \t]*({ABBR})[ \t]*[)）]")
_EN_AFTER = re.compile(rf"(?<![A-Za-z0-9])({ABBR})[ \t]*[(（][ \t]*([A-Za-z][A-Za-z \-]{{3,70}}?)[ \t]*[)）]")
# 日文只收「片假名全称（缩写）」：括号前紧挨着的一整串片假名才像译名；「FIA（キャリブレーション）」这种括号里是说明，不收
_KANA_BEFORE = re.compile(rf"(?<![{_KANA}ー・])([{_KANA}ー・]{{4,24}})[ \t]*[(（][ \t]*({ABBR})[ \t]*[)）]")
_TABLE_HEAD = re.compile(r"缩写|略語|略称|abbr|acronym|術語|用語", re.I)


def _trim(full: str) -> str:
    words = full.replace("- ", "-").split()
    while words and words[-1].lower() in _STOP:
        words.pop()
    return " ".join(words)


def _plausible(ab: str, words: list) -> bool:
    """像个全称：2~8 个词；单个词的话要比缩写长不少，而且不能本身就是全大写（WLREP (WLREPSIDE) 这种不算）。"""
    if not 1 <= len(words) <= 8:
        return False
    if len(words) == 1:
        w = words[0]
        return len(w) >= len(ab) + 4 and not w.isupper()
    return not all(w.isupper() for w in words)


def _lang(s: str) -> str:
    if re.search(rf"[{_KANA}]", s):
        return "ja"
    if re.search(rf"[{_CJK}]", s):
        return "zh"   # 汉字词：中日都可能，按中文算（日文资料里的汉字词多半也通用）
    return "en"


def _tables(text: str):
    """markdown 表格里表头有「缩写/略語/Abbreviation」的，逐行取 (缩写, [其余格子])。"""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if "|" not in ln or not _TABLE_HEAD.search(ln):
            continue
        heads = [h.strip(" *_") for h in ln.strip().strip("|").split("|")]
        col = next((k for k, h in enumerate(heads) if _TABLE_HEAD.search(h)), 0)
        for row in lines[i + 1:i + 80]:
            if "|" not in row:
                break
            cells = [c.strip(" *_`") for c in row.strip().strip("|").split("|")]
            if len(cells) <= col or set("".join(cells)) <= set("-: "):
                continue
            ab = re.sub(r"<br>.*", "", cells[col]).strip()
            if re.fullmatch(ABBR, ab):
                yield ab, [re.sub(r"<br>", " ", c).strip() for k, c in enumerate(cells) if k != col and c.strip()]


def mine(rows) -> dict:
    """rows: (doc_name, text) 迭代。返回 {缩写: [{en, ja, zh, n, docs}]}（未合并人工表）。"""
    en = defaultdict(Counter)                       # 缩写 → 英文全称计数
    loc = defaultdict(Counter)                      # 缩写 → 日文/中文计数
    docs = defaultdict(set)
    table_hits = defaultdict(Counter)
    table_pairs = defaultdict(lambda: defaultdict(set))   # 缩写 → 同一行的英文全称 → 同一行的日文 / 中文
    for name, text in rows:
        t = re.sub(r"<br>", " ", text or "")
        for m in _EN_BEFORE.finditer(t):
            ab = m.group(2)
            full = _best_suffix(ab, m.group(1))
            full = _trim(full) if full else full
            if full and ab not in _SKIP and _plausible(ab, full.split()):
                en[ab][full.title() if full.islower() else full] += 1
                docs[ab].add(name)
        for m in _EN_AFTER.finditer(t):
            ab, full = m.group(1), _trim(m.group(2))
            words = full.split()
            if ab not in _SKIP and _plausible(ab, words) and _initials_ok(ab, words):
                en[ab][full] += 1
                docs[ab].add(name)
        # 日文 / 中文不从正文自动配：试过「片假名（缩写）」，括号前的片假名多半是旁边的说明（FIA ↔ キャリブレーション），
        # 只信资料里现成的对照表和人工表
        for ab, cells in _tables(t):
            if ab in _SKIP:
                continue
            row_en = next((c.strip() for c in cells[:3] if _lang(c) == "en" and c.strip()), "")
            for c in cells[:2]:
                if _lang(c) in ("ja", "zh") and len(c.strip()) <= 20:
                    table_pairs[ab][row_en.lower()].add(c.strip())
            for c in cells[:3]:
                c = c.strip()
                if not c or len(c) > 60:
                    continue
                table_hits[ab][c] += 3            # 对照表里写的分量重
                docs[ab].add(name)
    out = {}
    for ab in set(en) | set(loc) | set(table_hits):
        senses_en = [(f, n) for f, n in en[ab].most_common() if n >= 2]
        for f, n in table_hits[ab].items():
            if _lang(f) == "en" and re.fullmatch(r"[A-Za-z][A-Za-z \-/&]{2,60}", f) and \
                    _initials_ok(ab, re.findall(r"[A-Za-z][A-Za-z\-]*", f)):
                senses_en.append((f, n))
        # 同一全称大小写不同合并；最多留 3 种英文意思
        merged = Counter()
        for f, n in senses_en:
            norm = lambda x: re.sub(r"[\s\-]+", " ", x.lower()).strip()
            key = next((k for k in merged if norm(k) == norm(f)), f)
            merged[key] += n
        if not merged and not table_pairs[ab]:
            continue
        senses = [{"en": f, "n": n, "ja": [], "zh": []} for f, n in merged.most_common(3)]
        # 对照表里的日文 / 中文只挂到同一行的那个英文意思上；那一行没写英文的，单独算一个意思
        for row_en, names in table_pairs[ab].items():
            target = next((s for s in senses if s["en"].lower() == row_en and row_en), None)
            if target is None and row_en:
                continue
            if target is None:
                target = {"en": "", "n": 3, "ja": [], "zh": []}
                senses.append(target)
            for x in sorted(names):
                target["ja" if _lang(x) == "ja" else "zh"].append(x)
        for s in senses:
            s["src"] = "auto"
        out[ab] = {"senses": senses, "docs": sorted(docs[ab])[:5]}
    return out


def load_manual() -> dict:
    """config/glossary.tsv：缩写<TAB>英文全称<TAB>日文（可多个，用 / 分隔）<TAB>中文<TAB>说明。
    同一缩写写几行就是几种意思；人工的排在自动挖到的前面。"""
    out = defaultdict(list)
    if not MANUAL.exists():
        return {}
    for ln in MANUAL.read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = (ln.split("\t") + [""] * 5)[:5]
        ab, en, ja, zh, note = (p.strip() for p in parts)
        if not ab:
            continue
        split = lambda s: [x.strip() for x in s.split("/") if x.strip()]
        out[ab.upper()].append({"en": en, "ja": split(ja), "zh": split(zh), "note": note, "src": "manual"})
    return dict(out)


def build(fts_path: str = None, log=print) -> dict:
    """读全文索引（不含电路图、错误码原文）挖对照表，合并人工表，写 data/glossary.json。"""
    try:
        import pysqlite3 as sqlite3
    except ImportError:
        import sqlite3
    from src.fulltext import _db_path
    c = sqlite3.connect(fts_path or _db_path)
    tables = {r[0] for r in c.execute("select name from sqlite_master")}
    tbl = "doc_fts" if "doc_fts" in tables else "rows"
    rows = c.execute(f"select doc_name, text from {tbl} where doc_type not in ('circuit_diagram', 'error_code')")
    auto = mine(rows)
    manual = load_manual()
    merged = {}
    for ab in set(auto) | set(manual):
        senses = list(manual.get(ab, []))
        for s in (auto.get(ab) or {}).get("senses", []):
            dup = next((m for m in senses if m["en"] and s["en"] and m["en"].lower() == s["en"].lower()), None)
            if dup:                            # 人工写了同一个意思：补上自动挖到的日文/中文
                for k in ("ja", "zh"):
                    dup[k] = dup[k] + [x for x in s.get(k, []) if x not in dup[k]]
            else:
                senses.append(s)
        merged[ab] = {"senses": senses, "docs": (auto.get(ab) or {}).get("docs", [])}
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    _CACHE.clear()
    log(f"缩写对照表：{len(merged)} 个缩写（人工 {len(manual)}，自动 {len(auto)}）→ {DB}")
    return {"abbrs": len(merged), "manual": len(manual), "auto": len(auto)}


# ── 2. 查询 ─────────────────────────────────────────────────────────
_CACHE = {}


def table() -> dict:
    mtime = DB.stat().st_mtime if DB.exists() else 0
    mt2 = MANUAL.stat().st_mtime if MANUAL.exists() else 0
    if _CACHE.get("mtime") != (mtime, mt2):
        data = json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else {}
        for ab, senses in load_manual().items():   # 人工表改了不用重建也能生效
            have = data.setdefault(ab, {"senses": [], "docs": []})["senses"]
            for s in senses:
                if not any(h.get("src") == "manual" and h["en"] == s["en"] for h in have):
                    have.insert(0, s)
        _CACHE.update(mtime=(mtime, mt2), data=data)
    return _CACHE["data"]


def lookup(question: str) -> list:
    """问题里认识的缩写：[(缩写, [意思…])]。只认独立的大写词（RR、RR4 不算；STG-29 这种报警码不算）。"""
    t = table()
    out, seen = [], set()
    for m in re.finditer(r"(?<![A-Za-z0-9\-$])([A-Za-z][A-Za-z0-9]{1,6})(?![A-Za-z0-9])(?!-\d)", question or ""):
        tok = m.group(1)
        ab = tok.upper()
        if ab in seen or ab not in t:
            continue
        if tok != ab and (len(tok) > 3 or tok.lower() in _COMMON_WORDS):
            continue                               # 小写的只认 rr / fia 这种短的，set / run / mode 这类英文单词不当缩写
        senses = [s for s in t[ab]["senses"] if s.get("en") or s.get("ja") or s.get("zh")]
        if senses:
            seen.add(ab)
            out.append((ab, senses))
    return out


def expansions(question: str, max_terms: int = 6) -> list:
    """给检索用的展开词：每个缩写的英文全称、日文、中文（人工表的意思排前面）。"""
    terms = []
    for ab, senses in lookup(question):
        for s in senses[:3]:
            for x in [s.get("en")] + list(s.get("ja") or [])[:1] + list(s.get("zh") or [])[:1]:
                if x and x not in terms:
                    terms.append(x)
    return terms[:max_terms]


def augment(question: str) -> str:
    """语义检索用：问题后面补一句「（RR = Reticle Rotation / レチクルローテーション / 掩模旋转）」。"""
    notes = []
    for ab, senses in lookup(question):
        s = senses[0]
        names = [x for x in [s.get("en")] + list(s.get("ja") or [])[:1] + list(s.get("zh") or [])[:1] if x]
        if names:
            notes.append(f"{ab} = {' / '.join(names)}")
    return f"{question}（{'；'.join(notes)}）" if notes else question


def hint(question: str) -> str:
    """给智能体的术语提示：列出问题里缩写的各种意思，让它按上下文选、检索时用全称。"""
    lines = []
    for ab, senses in lookup(question):
        parts = []
        for s in senses[:4]:
            names = " / ".join(x for x in [s.get("en")] + list(s.get("ja") or [])[:2] + list(s.get("zh") or [])[:2] if x)
            parts.append(names + (f"（{s['note']}）" if s.get("note") else ""))
        lines.append(f"- {ab}：" + "；或 ".join(parts))
    if not lines:
        return ""
    return ("【术语提示】问题里的缩写在资料里可能写成全称，检索时请同时用全称（英文/日文/中文）去查；"
            "有几种意思时按问题的上下文选：\n" + "\n".join(lines))
