"""src/ovl_view.py —— 网页「OVL 分析」页的处理逻辑（2026-10-17）。

解析、画图、10 参数拟合全部用 ovl-analyzer 的代码（同一套，不复制）：
  本机 ~/ovl-analyzer，服务器 /opt/ovl-analyzer；也可以用 OVL_ANALYZER_PATH 指定。
  用到的模块：mesr_reader（通用读取）、mesr_parser + ovl_model（10 参数）、plots（不依赖 Qt 的画图函数）。
上传的 MESR 只在本次会话里用，不入库、不发给任何模型。
"""
import io
import os
import sys

import numpy as np
import pandas as pd


def _ovl_path() -> str:
    for p in (os.getenv("OVL_ANALYZER_PATH", ""), "/opt/ovl-analyzer", os.path.expanduser("~/ovl-analyzer")):
        if p and os.path.isdir(os.path.join(p, "ovl_analyzer")):
            return p
    raise RuntimeError("找不到 ovl-analyzer 的代码（设 OVL_ANALYZER_PATH，或放到 /opt/ovl-analyzer）")


_P = _ovl_path()
if _P not in sys.path:
    sys.path.insert(0, _P)

import matplotlib  # noqa: E402
matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402

from ovl_analyzer.mesr_reader import EVAL_MODE_DESC, read_mesr  # noqa: E402
from ovl_analyzer.mesr_parser import parse_mesr_file  # noqa: E402
from ovl_analyzer.ovl_model import fit_10param_model  # noqa: E402
from ovl_analyzer import plots  # noqa: E402

LIST_COLUMNS = ["文件名", "类型", "测量日期", "机台", "传感器", "备注", "数据行"]


def _png(fig: Figure, dpi: int = 110):
    from PIL import Image
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    return Image.open(buf).copy()


def _name(path: str) -> str:
    return os.path.basename(path or "")


def file_table(paths) -> tuple:
    """上传的文件 → (列表 DataFrame, 下拉选项 [(显示名, 路径)])。读不了的标出来，不中断。"""
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


def show(path: str, scale_factor: float = 1.0) -> dict:
    """一个文件的全部视图：说明、图、数据表、文件头、原文，以及能不能做 10 参数拆分。"""
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
        table = pd.DataFrame(arr, columns=mf.column_names() + ["Wafer"])
        table = table.replace({np.nan: None})
    else:
        info.append("没有可读的测量数据。")
    header = "\n".join(f"{k} = {v}" for k, v in mf.header.items())
    return {"info": "\n\n".join(info), "image": img, "table": table, "header": header,
            "raw": mf.pretty_text(), "can_fit": bool(mf.is_vector_type and arr is not None and len(arr))}


def wafers(path: str) -> list:
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
