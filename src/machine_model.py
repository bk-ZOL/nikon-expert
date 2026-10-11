"""机型标签：入库时按 WPS 路径/文件名推断；检索时按机型加权（只降权、不过滤）。

标签用机型"族"：
  S206D 族：S205C / S206D / S305B / S306C / S306D
  S207D 族：S207D / S307E
  SF100 族：SF100 ~ SF140 / SF200
通用文档可带多个标签，逗号分隔，如 "S206D,S207D"。
环境变量：
  DEFAULT_MACHINE_MODEL  无法推断时的默认标签（S207D）
  PRIMARY_MODELS         问题没点名机型时的优先顺序（S207D,S206D），依次乘 1.0 / 0.8 …
  OTHER_MODEL_FACTOR     非优先机型 / 与问题点名机型不符时的系数（0.5）
"""
import os
import re

# 文件名、标签用的严格规则
STRICT = [
    ("S206D", re.compile(r"S20[56][CD]|S30[56][BCD]", re.I)),
    ("S207D", re.compile(r"S207D|S307E", re.I)),
    ("SF100", re.compile(r"SF1[0-4]0|SF200", re.I)),
]
# 用户提问用的宽松规则（"207""206 的""S307" 这类说法）
LOOSE = [
    ("S206D", re.compile(r"(?<![0-9A-Z])(?:NSR-?)?S?20[56][CD]?(?![0-9])|(?<![0-9A-Z])S?30[56][BCD]?(?![0-9])", re.I)),
    ("S207D", re.compile(r"(?<![0-9A-Z])(?:NSR-?)?S?207D?(?![0-9])|(?<![0-9A-Z])S?307E?(?![0-9])", re.I)),
    ("SF100", re.compile(r"SF\s?1[0-4]0|SF\s?200", re.I)),
]
DIR_FAMILY = {"206": "S206D", "207": "S207D", "307": "S207D", "SF100": "SF100"}
ORDER = ["COMMON", "S207D", "S206D", "SF100"]          # 标签排列顺序


def _common_dirs():
    """跟机型无关的通用资料所在目录（关键字，逗号分隔）。带 COMMON 标签的文档任何问题都不降权。"""
    return [d.strip() for d in os.getenv("COMMON_DIRS", "光刻原理").split(",") if d.strip()]


def _sort_tags(tags):
    tags = [t for t in dict.fromkeys(tags) if t]
    return sorted(tags, key=lambda t: ORDER.index(t) if t in ORDER else len(ORDER))


def _default():
    return os.getenv("DEFAULT_MACHINE_MODEL", "S207D")


def _primary():
    return [m.strip() for m in os.getenv("PRIMARY_MODELS", "S207D,S206D").split(",") if m.strip()]


def _families(text, rules=STRICT):
    return [tag for tag, pat in rules if pat.search(text or "")]


def infer_model(rel_path: str = "", name: str = "") -> str:
    """WPS 路径上最近的机型目录优先；其次文件名；都没有给默认值。"""
    if any(d in (rel_path or "") for d in _common_dirs()):
        return "COMMON"
    parts = [p for p in re.split(r"[\\/]", rel_path or "") if p]
    for p in reversed(parts[:-1] if parts else []):
        if p in DIR_FAMILY:
            return DIR_FAMILY[p]
    found = _sort_tags(_families(name))
    return ",".join(found) if found else _default()


def normalize(tags: str) -> list:
    """把库里的标签（含旧写法 NSR-S207D）归一成族名列表。"""
    out = []
    for raw in re.split(r"[,，;/ ]+", tags or ""):
        if not raw:
            continue
        fams = _families(raw)
        out.extend(fams if fams else [raw.upper()])
    return list(dict.fromkeys(out))


def models_in_query(query: str) -> list:
    return _families(query, LOOSE)


def model_factor(tags: str, query: str = "") -> float:
    fams = normalize(tags)
    if not fams or "COMMON" in fams:
        return 1.0                      # 没标签的、通用资料不动
    other = float(os.getenv("OTHER_MODEL_FACTOR", "0.5"))
    want = models_in_query(query)
    if want:
        return 1.0 if any(f in want for f in fams) else other
    prim = _primary()
    best = other
    for f in fams:
        if f in prim:
            best = max(best, 1.0 - 0.2 * prim.index(f))
    return best


def infer_model_multi(rel_paths, name: str = "") -> str:
    """同一文件出现在多个机型目录（如 206、307、SF100 各放一份）时，合并各路径的标签。"""
    tags = []
    for rp in (rel_paths or [""]):
        tags.extend(infer_model(rp, name).split(","))
    tags = _sort_tags(tags)
    return ",".join(tags) if tags else _default()


def mirror_paths(mirror, name: str) -> list:
    """镜像目录里所有同名文件的相对路径。"""
    import os
    root = str(mirror)
    out = []
    for d, _, files in os.walk(root):
        if name in files:
            out.append(os.path.relpath(os.path.join(d, name), root))
    return out
