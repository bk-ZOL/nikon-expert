"""src/ovl_view.py —— 网页「OVL 分析」页，以及对话智能体的同一套分析。

解析、画图、10 参数拟合全部用 ovl-analyzer 的代码（同一套，不复制）：
  本机 ~/ovl-analyzer，服务器 /opt/ovl-analyzer；也可以用 OVL_ANALYZER_PATH 指定。
  用到的模块：mesr_reader（通用读取）、mesr_parser + ovl_model（10 参数）、plots（不依赖 Qt 的画图函数）。

上传的 MESR 只在这次登录里用，不入库、不进向量库、不发给向量模型。
对话里附上的文件和「OVL 分析」页上传的文件记在同一份会话列表里，智能体工具 ovl_analyze 读它。
"""
import os
import re
import secrets
import sys
import threading
from pathlib import Path

LIST_COLUMNS = ["文件名", "类型", "测量日期", "机台", "传感器", "备注", "数据行"]

_loaded = False
_load_lock = threading.Lock()

_upload_lock = threading.Lock()
_uploads: dict = {}          # session key → [{path, name}, ...]，最近的在最后
_MAX_UPLOADS = 8

_ARTIFACT_RE = re.compile(r"^[a-f0-9]{16}_(vector|curve|residual)\.png$")
_WAFER_RE = re.compile(r"(?:wafer|晶圆)\s*[#:：]?\s*(\d+)", re.I)
_LATEST = {"", "latest", "last", "最近", "最近一份", "最新", "最新一份", "这个", "这份", "当前", "刚才", "刚传的"}


def _ovl_path() -> str:
    for p in (os.getenv("OVL_ANALYZER_PATH", ""), "/opt/ovl-analyzer", os.path.expanduser("~/ovl-analyzer")):
        if p and os.path.isdir(os.path.join(p, "ovl_analyzer")):
            return p
    raise RuntimeError("找不到 ovl-analyzer 的代码（设 OVL_ANALYZER_PATH，或放到 /opt/ovl-analyzer）")


def _load():
    """第一次真正读 MESR 时才加载 ovl-analyzer。没装的时候，对话仍能打开，工具会说明原因。"""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        p = _ovl_path()
        if p not in sys.path:
            sys.path.insert(0, p)
        import numpy as _np
        import pandas as _pd
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure as _Figure
        from ovl_analyzer.mesr_reader import EVAL_MODE_DESC as _EVAL, read_mesr as _read
        from ovl_analyzer.mesr_parser import parse_mesr_file as _parse
        from ovl_analyzer.ovl_model import fit_10param_model as _fit
        from ovl_analyzer import plots as _plots
        g = globals()
        g["np"] = _np
        g["pd"] = _pd
        g["Figure"] = _Figure
        g["EVAL_MODE_DESC"] = _EVAL
        g["read_mesr"] = _read
        g["parse_mesr_file"] = _parse
        g["fit_10param_model"] = _fit
        g["plots"] = _plots
        _loaded = True


def _png(fig, dpi: int = 110):
    import io
    from PIL import Image
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    return Image.open(buf).copy()


def _name(path: str) -> str:
    return os.path.basename(path or "")


def file_table(paths) -> tuple:
    """上传的文件 → (列表 DataFrame, 下拉选项 [(显示名, 路径)])。读不了的标出来，不中断。"""
    _load()
    rows, choices = [], []
    for p in paths or []:
        try:
            mf = read_mesr(p)
            arr = mf.data_array()
            mode = mf.eval_mode
            rows.append([_name(p), f"{mode}（{EVAL_MODE_DESC.get(mode, '')}）".replace("（）", ""),
                         str(mf.get("MEAS_DATE")), str(mf.get("MEAS_MACHINE") or mf.get("EXPS_MACHINE")),
                         str(mf.get("MEAS_SENS")), str(mf.get("COMMENT")), 0 if arr is None else len(arr)])
            choices.append((f"{_name(p)}　[{mode}]", p))
        except Exception as e:
            rows.append([_name(p), f"读取失败：{type(e).__name__}", "", "", "", str(e)[:60], 0])
    return pd.DataFrame(rows, columns=LIST_COLUMNS), choices


def show(path: str, scale_factor: float = 1.0, with_table: bool = True) -> dict:
    """一个文件的全部视图：说明、图、数据表、文件头、原文，以及能不能做 10 参数拆分。

    with_table=False 时不组装整张数据表（智能体只需要摘要，避免把大表送进模型）。
    """
    _load()
    mf = read_mesr(path)
    arr = mf.data_array()
    px, py = mf.step_pitch
    info = [f"**{_name(path)}**　类型 {mf.eval_mode}　{EVAL_MODE_DESC.get(mf.eval_mode, '')}",
            f"测量日期 {mf.get('MEAS_DATE')}　机台 {mf.get('MEAS_MACHINE') or mf.get('EXPS_MACHINE')}　"
            f"传感器 {mf.get('MEAS_SENS')}　Step pitch {px / 1000:g} × {py / 1000:g} mm　晶圆 {mf.wafer_size:g} mm"]
    if mf.get("COMMENT"):
        info.append(f"备注：{mf.get('COMMENT')}")
    img, table = None, pd.DataFrame()
    if arr is not None and len(arr):
        info.append(plots.summarize(mf, arr).replace("\n", "　｜　"))
        fig = Figure(figsize=(13, 6.2))
        if mf.is_vector_type:
            n = mf.n_fields
            scale = plots._auto_scale(mf, np.hypot(arr[:, n - 2], arr[:, n - 1])) * float(scale_factor or 1)
            plots.draw_vector(fig, mf, arr, scale)
        else:
            plots.draw_curves(fig, mf, arr, mf.column_names())
        img = _png(fig)
        if with_table:
            table = pd.DataFrame(arr, columns=mf.column_names() + ["Wafer"])
            table = table.replace({np.nan: None})
    else:
        info.append("没有可读的测量数据。")
    header = "\n".join(f"{k} = {v}" for k, v in mf.header.items())
    return {"info": "\n\n".join(info), "image": img, "table": table, "header": header,
            "raw": mf.pretty_text(), "can_fit": bool(mf.is_vector_type and arr is not None and len(arr))}


def wafers(path: str) -> list:
    _load()
    d = parse_mesr_file(path)
    if d.meas_df is None or d.meas_df.empty:
        return []
    return [int(w) for w in sorted(d.meas_df["wafer_id"].unique())]


def _residual_scale(d, res) -> float:
    """残差图箭头倍率：典型箭头约为 shot 尺寸的 30%（和浏览器的自动倍率同一规则）。"""
    mag = np.hypot(res.raw_dx, res.raw_dy) if res.raw_dx is not None else np.array([])
    mag = mag[np.isfinite(mag) & (mag > 0)]
    if not mag.size:
        return 1000.0
    pitch_mm = min(d.step_pitch_x, d.step_pitch_y) / 1000.0
    return max(pitch_mm * 0.3 / np.percentile(mag, 75), 1e-3)


def ten_param(path: str, wafer_id: int, scale_factor: float = 1.0) -> dict:
    """10 参数（晶圆级 Tx/Ty/Sx/Sy/Rw/Ow + Shot 内 tx/ty/sx/sy/rs/os）拆分：参数表、残差统计、原始 vs 残差矢量图。"""
    _load()
    d = parse_mesr_file(path)
    res = fit_10param_model(d, int(wafer_id))
    df = res.to_dataframe()
    df["Value"] = df["Value"].map(lambda v: round(float(v), 4))
    df = df.rename(columns={"Parameter": "参数", "Value": "数值", "Unit": "单位", "Type": "层级"})
    df["层级"] = df["层级"].map({"Grid": "晶圆级", "Shot": "Shot 内"})
    st = res.residual_stats()
    stats = ""
    if st:
        stats = ("| | X 均值 | X 3σ | Y 均值 | Y 3σ |\n|---|---|---|---|---|\n"
                 f"| 原始 | {st['Raw Xave (μm)']:+.4f} | {st['Raw X3σ (μm)']:.4f} | {st['Raw Yave (μm)']:+.4f} | {st['Raw Y3σ (μm)']:.4f} |\n"
                 f"| 去掉 10 参数后的残差 | {st['Residual Xave (μm)']:+.4f} | {st['Residual X3σ (μm)']:.4f} | "
                 f"{st['Residual Yave (μm)']:+.4f} | {st['Residual Y3σ (μm)']:.4f} |\n\n"
                 f"单位 μm。拟合优度 R²：X {st['R² (X)']:.3f}，Y {st['R² (Y)']:.3f}（越接近 1，说明误差越能被这 10 项解释，"
                 "剩下的残差是模型解释不了的部分）")
    fig = Figure(figsize=(13, 6.4))
    plots.draw_residual_map_fig(fig, d, int(wafer_id), res, _residual_scale(d, res) * float(scale_factor or 1))
    return {"params": df, "stats": stats, "image": _png(fig)}


# ── 这次登录里记住的 MESR（对话附件 + OVL 页上传，同一份列表）──────────────

def session_key(request=None) -> str:
    """对话和 OVL 页用同一把钥匙，智能体才读得到刚传的文件。

    开了登录时，网页事件用登录名；工具跑在线程里，用已经绑上的用户。
    没开登录（单机）时大家共用 shared。
    """
    from src import acl
    if request is not None and acl.security_enabled():
        uid = getattr(request, "username", None)
        if uid:
            return str(uid)
    user = acl.get_current_user()
    if user is not None and getattr(user, "uid", None) and not getattr(user, "is_superuser", False):
        return str(user.uid)
    if not acl.security_enabled():
        return "shared"
    uid = getattr(request, "username", None) if request is not None else None
    return uid or "shared"


def clear_uploads(key: str | None = None) -> None:
    with _upload_lock:
        if key is None:
            _uploads.clear()
        else:
            _uploads.pop(key, None)


def _items(key: str) -> list:
    with _upload_lock:
        return list(_uploads.get(key, []))


def remember_uploads(key: str, paths) -> list:
    """记下刚上传的 MESR。空列表不清空（避免页面初始化把已记住的文件冲掉）。不存在的路径跳过。"""
    fresh = []
    for p in paths or []:
        if not isinstance(p, str) or not p:
            continue
        ap = os.path.abspath(p)
        if os.path.isfile(ap):
            fresh.append({"path": ap, "name": os.path.basename(ap)})
    if not fresh:
        return pending_names(key)
    with _upload_lock:
        seen = {i["path"] for i in fresh}
        cur = [x for x in _uploads.get(key, []) if x["path"] not in seen]
        cur.extend(fresh)
        _uploads[key] = cur[-_MAX_UPLOADS:]
    return pending_names(key)


def pending_names(key: str | None = None) -> list:
    return [x["name"] for x in _items(key or session_key())]


def remembered_paths(key: str | None = None) -> list:
    return [x["path"] for x in _items(key or session_key())]


def has_pending(key: str | None = None) -> bool:
    return bool(_items(key or session_key()))


def attach_message(key: str | None = None) -> str:
    names = pending_names(key)
    if not names:
        return "还没有 MESR。可以在对话里附上，或在「OVL 分析」页上传。文件只在这次使用，不入库。"
    return "已记住（不入库）：" + "、".join(names) + "。对话里问叠对 / overlay / MESR / 10 参数即可，默认分析最近一份。"


def parse_which(which: str) -> tuple:
    """「文件名 wafer 2」→ (文件名, 2)。认不出晶圆号时第二个是 None。"""
    which = (which or "").strip()
    m = _WAFER_RE.search(which)
    wafer = int(m.group(1)) if m else None
    if m:
        which = (which[:m.start()] + " " + which[m.end():]).strip()
    which = re.sub(r"\s+", " ", which).strip(" ，,。.")
    return which, wafer


def resolve_remembered(which: str = "", key: str | None = None) -> tuple:
    """在这次登录记住的文件里找。(path, wafer, error)。error 非空就不要再去读盘。

    智能体只能点已上传的文件名，不能拿任意磁盘路径来读。
    """
    key = key or session_key()
    hint, wafer = parse_which(which)
    if hint.lower() in _LATEST:
        hint = ""
    items = _items(key)
    if not items:
        return "", wafer, (
            "还没有 MESR。请在对话输入框上方「附上 MESR」，或先打开「OVL 分析」页上传。"
            "文件只在这次登录里用，不进知识库。"
        )
    if not hint:
        return items[-1]["path"], wafer, ""
    if os.path.isabs(hint) or hint.startswith("~"):
        abs_hint = os.path.abspath(os.path.expanduser(hint))
        for it in items:
            if it["path"] == abs_hint:
                return it["path"], wafer, ""
        return "", wafer, "不能按磁盘路径读文件。请用已上传的文件名，或留空用最近一份。"
    hint_l = hint.lower()
    base = os.path.basename(hint).lower()
    exact = [it for it in items if it["name"].lower() in (hint_l, base)]
    if exact:
        return exact[-1]["path"], wafer, ""
    sub = [it for it in items if hint_l in it["name"].lower()]
    if len(sub) == 1:
        return sub[0]["path"], wafer, ""
    if len(sub) > 1:
        names = "、".join(it["name"] for it in sub)
        return "", wafer, f"有多份对得上「{hint}」：{names}。请写完整文件名。"
    names = "、".join(it["name"] for it in items)
    return "", wafer, f"没有对上「{hint}」。当前已记住：{names}。留空则用最近一份。"


def artifact_dir() -> Path:
    p = Path(os.getenv("OVL_ARTIFACT_DIR", os.path.join("data", "ovl_artifacts")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifact_file(name: str):
    """只认本模块存出去的图，拒绝路径穿越。"""
    if not name or not _ARTIFACT_RE.match(name):
        return None
    root = artifact_dir().resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        return None
    return path


def _store_image(img, kind: str, label: str) -> dict:
    root = artifact_dir()
    token = secrets.token_hex(8) + f"_{kind}.png"
    path = root / token
    img.save(path, format="PNG")
    files = sorted(root.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for old in files[:-200]:
        try:
            old.unlink()
        except OSError:
            pass
    return {"kind": kind, "label": label, "file": str(path), "url": f"/serve-ovl?f={token}"}


def _md_table(df) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        cells = ["" if v is None else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _images_md(images: list) -> str:
    if not images:
        return ""
    blocks = ["**图**（与「OVL 分析」页同一套画图，点开可看大图）"]
    for im in images:
        blocks.append(f"[{im['label']}]({im['url']})")
        blocks.append(f"![{im['label']}]({im['url']})")
    return "\n\n".join(blocks)


def analyze_for_agent(path: str, wafer: int | None = None) -> dict:
    """对一个已经允许读取的 MESR 做和网页相同的分析。

    返回 {"text": 给模型的摘要, "note": 附在回答后面给现场看的摘要和图}。
    不读整张数据表进摘要，完整表仍在「OVL 分析」页。
    """
    view = show(path, 1.0, with_table=False)
    lines = [view["info"], "", "完整数据表、文件头和原文在网页「OVL 分析」页（同一份上传已记住）。原始文件不入库。"]
    images = []
    if view.get("image") is not None:
        if view.get("can_fit"):
            images.append(_store_image(view["image"], "vector", "晶圆矢量图"))
        else:
            images.append(_store_image(view["image"], "curve", "测量曲线"))
    if view.get("can_fit"):
        ws = wafers(path)
        if not ws:
            lines += ["", "没有可拟合的晶圆数据，未做 10 参数拆分。"]
        else:
            picked = ws[0] if wafer is None else int(wafer)
            if picked not in ws:
                lines += ["", f"没有 Wafer {picked}。这份文件里有：{'、'.join(str(w) for w in ws)}。"]
            else:
                try:
                    tp = ten_param(path, picked, 1.0)
                except Exception as e:
                    lines += ["", f"10 参数拟合失败：{type(e).__name__}: {e}"]
                else:
                    lines += ["", f"### 10 参数（Wafer {picked}）", "", _md_table(tp["params"])]
                    if tp.get("stats"):
                        lines += ["", tp["stats"]]
                    if tp.get("image") is not None:
                        images.append(_store_image(tp["image"], "residual", "原始 vs 残差"))
                    others = [str(w) for w in ws if w != picked]
                    if others:
                        lines += ["", f"其他晶圆：{'、'.join(others)}。要看的话再问「wafer N」。"]
    else:
        lines += ["", "这份不是叠对矢量数据，没有做 10 参数拆分。"]
    if images:
        lines += ["", "图会由网页附在回答末尾，回答里不用再贴链接："]
        lines += [f"- {im['label']} {im['url']}" for im in images]
    text = "\n".join(lines).strip()
    note = text
    extra = _images_md(images)
    if extra:
        note = text + "\n\n" + extra
    return {"text": text, "note": note}


def analyze_remembered(which: str = "", key: str | None = None) -> dict:
    """智能体入口：只分析这次登录记住的文件。失败也返回文字，不抛给对话。"""
    path, wafer, err = resolve_remembered(which, key)
    if err:
        return {"text": err, "note": err}
    try:
        return analyze_for_agent(path, wafer)
    except Exception as e:
        msg = f"OVL 分析失败：{type(e).__name__}: {e}"
        return {"text": msg, "note": msg}


def analyze_path(path: str, wafer: int | None = None) -> str:
    """MCP / 服务器上已经有的文件。调用方给出路径；不入库。"""
    if not path or not os.path.isfile(os.path.expanduser(path)):
        return f"找不到文件：{path}"
    try:
        return analyze_for_agent(os.path.abspath(os.path.expanduser(path)), wafer)["text"]
    except Exception as e:
        return f"OVL 分析失败：{type(e).__name__}: {e}"
