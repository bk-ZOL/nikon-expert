"""错误信息原文入库：WPS「5.Error code」下的 e_st_0540、j_wl_0307 这类文件（e_ 英文、j_ 日文，没有扩展名）。

文件是各控制器的错误信息表，按 "#29H"（2 位，STG/LC/AL/OPD）或 "#4000F007H"（8 位，WL/RL/MAC）分节，
节内还有 "80H: …" 这种细分码。每个错误码存成一条全文记录，标题写明控制器和代码；正文里补上现场写法
（STG-29、STG-29-80 …），直接搜现场写法就能命中这一节。只进全文索引（错误码检索靠精确匹配）。

用法：.venv/bin/python -m src.ingest_error_text "data/kwiki_mirror/5.Error code/e_st_0540" [...]
"""
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^([ej])_([a-z]+)_(\d{4})$")
CTRL = {"st": "STG", "lc": "LC", "al": "AL", "opd": "OPD", "wl": "WL", "rl": "RL", "mac": "MAC",
        "bsa": "BSA", "chmbr": "CHMBR"}
SECTION_RE = re.compile(r"^#([0-9A-F]{2}|[0-9A-F]{8})H\s*$")
DETAIL_RE = re.compile(r"^\s*([0-9A-F]{2})H\s*:", re.M)


def is_error_text(path) -> bool:
    return bool(NAME_RE.match(Path(path).name))


def sections(path):
    """[(代码, 标题, 正文)]，跳过文件头的 "!" 注释。"""
    lines = Path(path).read_bytes().decode("euc_jp", errors="ignore").splitlines()   # 老 Unix 系统导出，EUC-JP 编码
    out, cur, body = [], None, []
    for line in lines:
        m = SECTION_RE.match(line.strip())
        if m:
            if cur:
                out.append((cur, body))
            cur, body = m.group(1), []
        elif cur and not line.startswith("!"):
            body.append(line.rstrip())
    if cur:
        out.append((cur, body))
    res = []
    for code, body in out:
        title = next((b.strip() for b in body if b.strip()), "")
        res.append((code, title, "\n".join(body).strip()))
    return res


def ingest_error_text(path, machine_model: str = "") -> int:
    from src.fulltext import init_fts, ingest_fts
    from src.ingestor import _doc_id, _fts_delete_by_name
    path = Path(path)
    m = NAME_RE.match(path.name)
    if not m:
        raise ValueError(f"不是错误信息原文文件：{path.name}")
    lang, ctrl_key, ver = m.groups()
    ctrl = CTRL.get(ctrl_key, ctrl_key.upper())
    init_fts(os.getenv("FTS_DB_PATH", "./data/fts.db"))
    doc_name = path.name
    _fts_delete_by_name(doc_name)
    rid = _doc_id(str(path))
    n = 0
    for code, title, body in sections(path):
        alias = [f"{ctrl}-{code}"] if len(code) == 2 else [code]
        if len(code) == 2:
            alias += [f"{ctrl}-{code}-{d}" for d in dict.fromkeys(DETAIL_RE.findall(body))]
        head = (f"{ctrl} 错误码 #{code}H（{'英文' if lang == 'e' else '日文'}原文 {doc_name}，版本 {ver}）\n"
                f"现场写法：{' '.join(alias)}\n")
        ingest_fts(f"{rid}_{code}", doc_name, "error_code", machine_model,
                   f"{ctrl} #{code}H {title}"[:200], head + body, str(path))
        n += 1
    print(f"  ✅ {doc_name}：{n} 个错误码入库（{ctrl}，{'英文' if lang == 'e' else '日文'}）")
    return n


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from dotenv import load_dotenv
    load_dotenv(".env")
    total = 0
    for f in sys.argv[1:]:
        total += ingest_error_text(f, os.getenv("DEFAULT_MACHINE_MODEL", "S207D"))
    print(f"共 {total} 条")
