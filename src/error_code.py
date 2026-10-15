"""现场错误码写法 → 手册 / 原文换算。

现场（工作日志、故障履历）写 STG-29-80：控制器 STG、主码 29H、细分码 80H（都是十六进制）。
机台报警界面写「STG - FATAL - E Detail: 60730410 FFF0FE17 00000000 00000000」：第 1、2 串各取最后两位 = STG-10-17。对应：
  - STG 错误信息原文 e_st_0540（WPS「5.Error code」目录）：#29H 节下的「80H: …」行；
  - 《Error Code List》STG-xxxx-x (0B20xxxx) 章 (2) Unit Error Code List：主码「|29|」行、细分码「FFFF**80」行。
目前只做 STG：其他控制器的原文是 8 位码（#4000F007H），现场不会写成 XX-YY。

环境变量：
  ERROR_TEXT_DIR    错误信息原文所在目录（默认 data/kwiki_mirror/5.Error code）
  ERROR_CODE_LISTS  手册文档名关键字，按优先顺序，逗号分隔（默认先 S207D/S307E 再 S206D 系）
"""
import os
import re
from pathlib import Path

FIELD_RE = re.compile(r"(?<![A-Z0-9])(STG)[\s\-_]?([0-9A-F]{2})H?(?:[\s\-_/]([0-9A-F]{2})H?)?(?![0-9A-Z])", re.I)
TEXT_PREFIX = {"STG": "e_st_"}


# 机台报警界面的写法（2026-10-15 用户说明）：
#   STG - FATAL - E Detail:
#   60730410 FFF0FE17 00000000 00000000
# 第一段是 Unit（STG）；第 1、2 串 8 位码各取最后两位 → 主码 10、细分码 17 → STG-10-17。
# 目前只确认了 STG 的规则；其他 Unit 的报警界面写法还没找到统一标准，不换算（免得猜错带偏检索）。
SCREEN_RE = re.compile(
    r"(?<![A-Z0-9])(STG)\s*-\s*(?:FATAL|WARNING|WARN|ERROR|ALARM|INFO)?[^0-9A-Z]{0,6}[A-Z]?\s*"
    r"(?:DETAIL\s*:?)?\s*([0-9A-F]{8})\s+([0-9A-F]{8})", re.I | re.S)


def from_screen(text: str) -> list:
    """报警界面原文 → ["STG-10-17", …]。"""
    out = []
    for unit, s1, s2 in SCREEN_RE.findall(text or ""):
        code = f"{unit.upper()}-{s1[-2:].upper()}-{s2[-2:].upper()}"
        if code not in out:
            out.append(code)
    return out


def annotate(text: str) -> str:
    """问题里贴了报警界面原文时，在后面注明换算出的错误码，方便检索和模型理解。"""
    codes = from_screen(text)
    return f"{text}（报警界面换算：{'、'.join(codes)}）" if codes else text


def parse(text: str) -> list:
    """从文字里找现场写法的错误码，返回 [(控制器, 主码, 细分码或空), …]（大写、去重、保持顺序）。
    报警界面原文（STG - FATAL - E Detail: 60730410 FFF0FE17 …）先换算成 STG-10-17 再找。"""
    out = []
    text = " ".join(from_screen(text)) + " " + (text or "")
    for c, m, d in FIELD_RE.findall(text.upper()):
        k = (c, m, d or "")
        if k not in out:
            out.append(k)
    return out


def _clean(s: str) -> str:
    return " ".join(s.replace("<br>", " ").split())


# ── 原文文件（e_st_0540）──────────────────────────────────────────
def _text_file(ctrl: str):
    d = Path(os.getenv("ERROR_TEXT_DIR", "data/kwiki_mirror/5.Error code"))
    files = sorted(d.glob(TEXT_PREFIX.get(ctrl, "-") + "*")) if d.exists() else []
    return files[-1] if files else None


def from_text_file(ctrl: str, main: str, det: str = ""):
    """返回 {"file", "line", "title", "detail"}；找不到返回 None。"""
    f = _text_file(ctrl)
    if not f:
        return None
    lines = f.read_bytes().decode("euc_jp", errors="ignore").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().upper() == f"#{main}H"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"#[0-9A-F]{2}H\s*$", lines[i].strip())),
               len(lines))
    body = [l.rstrip() for l in lines[start + 1:end]]
    title = next((l.strip() for l in body if l.strip()), "")
    out = {"file": f.name, "line": start + 1, "title": title, "detail": ""}
    if det:
        for i, l in enumerate(body):
            m = re.match(rf"\s*{det}H\s*:\s*(.*)", l, re.I)
            if m:
                text, ind = [m.group(1).strip()], len(l) - len(l.lstrip())
                for nxt in body[i + 1:]:                     # 缩进更深的续行
                    if not nxt.strip() or re.match(r"\s*[0-9A-F]{2}H\s*:", nxt) or \
                            len(nxt) - len(nxt.lstrip()) <= ind:
                        break
                    text.append(nxt.strip())
                out["detail"] = " ".join(text)
                break
    return out


# ── 手册《Error Code List》──────────────────────────────────────
_manual_cache = {}


def _manual_chapter(ctrl: str):
    """按文档顺序拼出某本手册的 ctrl 章 Unit Error Code List 部分。返回 (手册名, [(文字, 章内页码), …])。"""
    db = os.getenv("FTS_DB_PATH", "./data/fts.db")
    key = (ctrl, os.path.getmtime(db) if os.path.exists(db) else 0)
    if key in _manual_cache:
        return _manual_cache[key]
    try:
        import pysqlite3 as sq
    except Exception:
        import sqlite3 as sq
    con = sq.connect(db)
    res = (None, [])
    for kw in os.getenv("ERROR_CODE_LISTS", "S207D S307E Error Code List,S206D S305B S306C S306D Error Code List").split(","):
        rows = con.execute("select rowid, section_title, text from doc_fts where doc_name like ? "
                           "and doc_name like '%Error Code List%' order by rowid", (f"%{kw.strip()}%",)).fetchall()
        mark = f"■ {ctrl} - "
        idx = [i for i, r in enumerate(rows) if mark in r[2]]
        if not idx:
            continue
        name = con.execute("select doc_name from doc_fts where rowid=?", (rows[idx[0]][0],)).fetchone()[0]
        chunks = []
        for r in rows[max(idx[0] - 1, 0): idx[-1] + 1]:
            if "Unit Error Code List" not in (r[1] or ""):
                continue
            pg = re.search(rf"■ {ctrl} - (\d+) ■", r[2])
            chunks.append((r[2], pg.group(1) if pg else ""))
        res = (name, chunks)
        break
    con.close()
    _manual_cache.clear()
    _manual_cache[key] = res
    return res


def from_manual(ctrl: str, main: str, det: str = ""):
    """返回 {"doc", "page", "title", "detail"}；找不到返回 None。"""
    name, chunks = _manual_chapter(ctrl)
    if not chunks:
        return None
    lines, pages = [], []
    for text, pg in chunks:
        for l in text.splitlines():
            lines.append(l)
            pages.append(pg)
    start = next((i for i, l in enumerate(lines) if re.match(rf"\|\s*{main}\s*\|", l.strip(), re.I)), None)
    if start is None:
        return None
    cells = lines[start].strip().strip("|").split("|")
    out = {"doc": name, "page": pages[start], "title": _clean(cells[1]) if len(cells) > 1 else "", "detail": ""}
    if det:
        for i in range(start + 1, len(lines)):
            l = lines[i].strip()
            if re.match(r"\|\s*[0-9A-F]{2}\s*\|", l):            # 到了下一个主码
                break
            m = re.match(rf"\|\|\s*[^|]*?{det}\s*\|([^|]*)\|", l, re.I)   # FFFF**80、FFFFNN70 都以细分码结尾
            if m:
                out["detail"] = _clean(m.group(1))
                out["page"] = pages[i] or out["page"]
                break
    return out


def lookup(code_text: str) -> str:
    """把现场写法换算成手册 / 原文里的条目；文字里没有现场写法时返回空串。"""
    parts = []
    for ctrl, main, det in parse(code_text):
        name = f"{ctrl}-{main}" + (f"-{det}" if det else "")
        lines = [f"【{name}】= {ctrl} 控制器，主码 #{main}H" + (f"，细分码 {det}H" if det else "")]
        t = from_text_file(ctrl, main, det)
        if t:
            lines.append(f"  · 原文 {t['file']}（第 {t['line']} 行 #{main}H）：{t['title']}")
            if det:
                lines.append(f"      {det}H：{t['detail'] or '（这一节里没有这个细分码）'}")
        m = from_manual(ctrl, main, det)
        if m:
            pg = f"{ctrl} 章第 {m['page']} 页" if m["page"] else f"{ctrl} 章"
            lines.append(f"  · 手册《{m['doc']}》{pg}，Unit Error Code List 主码 {main}：{m['title'][:300]}")
            if det:
                lines.append(f"      细分码 {det}：{m['detail'] or '（表里没有这个细分码）'}")
        if not t and not m:
            lines.append("  · 原文和手册里都没找到这个主码。")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
