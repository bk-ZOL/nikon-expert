"""把 Nikon 原厂《Troubleshooting Guide》（老 .xls）解析成 JSON，供 Troubleshooting 表导入成「原厂」条目。

Guide 的通用格式：错误码 → 推定原因（按发生频度编号）→ 处理顺序 → 检查/对策 → 详细做法，空行表示沿用上一行。
在 Mac 上跑（需要 xlrd 读 .xls），输出 JSON 传到服务器后用 ts_extract.py --import-oem 导入。

用法：
  .venv/bin/python scripts/ts_parse_oem_guide.py "<Troubleshooting Guide 目录>" out.json
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

WS_AXES = {"WX", "WY", "TC", "IHS", "WT", "WS"}
RS_AXES = {"MSC", "CM", "RF", "RS", "RX", "RY", "MSCL", "MSCR"}


def _s(v) -> str:
    s = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
    return s.replace("　", " ").strip()


def _num(v):
    s = _s(v)
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_sheet(df, cols, machines, unit_of, src, code_re, group_re=None, name_only=False):
    """cols: dict(axis, code, name, rate, cause, step, check, details) → 列号（没有的为 None）。
    返回条目列表：{machine, unit, error_code, title, causes:[{rate, cause, steps:[{check, details}]}], src}"""
    out, cur, cause, axis, group = [], None, None, "", ""

    def cell(row, k):
        j = cols.get(k)
        return _s(row[j]) if j is not None and j < len(row) else ""

    for _, r in df.iterrows():
        row = r.tolist()
        a = cell(row, "axis")
        if a and not code_re.match(a):
            axis = a.split()[0]
        code = cell(row, "code")
        name = cell(row, "name")
        if code and group_re and group_re.match(code) and not code_re.match(code):
            group = code             # 「40: Reset error」「#90H」这类分组标题
            continue
        if name_only:                # 无错误码的表：以「名称」列开新条目
            if name and _num(row[cols["idx"]]) is not None:
                cur = {"error_code": "", "title": name, "causes": [], "group": ""}
                out.append(cur)
                cause = None
            elif name and cur:
                cur["title"] += " " + name
        elif code and code_re.match(code):
            cur = {"error_code": code, "title": name or code, "causes": [], "group": group, "axis": axis}
            out.append(cur)
            cause = None
        if cur is None:
            continue
        ctext = cell(row, "cause")
        rate = _num(row[cols["rate"]]) if cols.get("rate") is not None else None
        if ctext:
            cause = {"rate": rate or len(cur["causes"]) + 1, "cause": ctext, "steps": []}
            cur["causes"].append(cause)
        check, det = cell(row, "check"), cell(row, "details")
        if check or det:
            if cause is None:
                cause = {"rate": len(cur["causes"]) + 1, "cause": "（原因未写明）", "steps": []}
                cur["causes"].append(cause)
            if check:
                cause["steps"].append({"check": check, "details": det})
            elif cause["steps"]:
                cause["steps"][-1]["details"] = (cause["steps"][-1]["details"] + "；" + det).strip("；")
            else:
                cause["steps"].append({"check": "", "details": det})
    entries = []
    for e in out:
        if not e["causes"] and not e["error_code"]:
            continue                 # 无错误码又无原因的才丢；只有名称的错误码也收（Guide 没写原因，至少能查到名称）
        unit = unit_of(e)
        title = e["title"]
        if e.get("group"):
            title = f"{title}（{e['group']}）"
        for m in machines:
            entries.append({"machine": m, "unit": unit, "error_code": e["error_code"], "title": title,
                            "axis": e.get("axis", ""), "causes": e["causes"], "src": src})
    return entries


def main():
    d, outp = Path(sys.argv[1]), Path(sys.argv[2])
    allx = []
    code_ws = re.compile(r"^[0-9A-F]{2}-[0-9A-FX]{2}$", re.I)

    # 1) WS/RS Error Cause Analysis（Sx05/06/07）
    f = d / "WS RS Troubleshooting Info.xls"
    df = pd.read_excel(f, sheet_name="WSRS Unit Errors 2004-09-20", header=None)
    allx += parse_sheet(df, dict(axis=0, code=1, name=2, rate=3, cause=4, step=5, check=6, details=7),
                        ["S206D", "S207D"],
                        lambda e: "RS" if e.get("axis", "").upper() in RS_AXES else "WS",
                        f"{f.name}｜WSRS Unit Errors", code_ws, group_re=re.compile(r"^[0-9A-F]{2}\s*:"))

    # 2) AFWTALG-AF Errors（Sx05/06，v1.20）
    f = d / "SX05 06 07 Alg AF etc.xls"
    df = pd.read_excel(f, sheet_name="Causes of AFWTALG-AF Errors", header=None)
    allx += parse_sheet(df, dict(axis=None, code=1, name=2, rate=3, cause=4, step=5, check=6, details=7),
                        ["S206D"], lambda e: "AF", f"{f.name}｜Causes of AFWTALG-AF Errors",
                        code_ws, group_re=re.compile(r"^#[0-9A-F]{2}H", re.I))

    # 3) Chamber（S305/S306 AV0303）
    f = d / "Chamber Troubleshooting Guide.xls"
    df = pd.read_excel(f, sheet_name="Error Code AV0303", header=None)
    allx += parse_sheet(df, dict(axis=None, name=1, code=2, rate=3, cause=4, step=5, check=6, details=7),
                        ["S206D"], lambda e: "Chamber", f"{f.name}｜Error Code AV0303",
                        re.compile(r"^[0-9A-F]{8}$", re.I))
    df = pd.read_excel(f, sheet_name="No Error Code", header=None)
    allx += parse_sheet(df, dict(idx=0, axis=None, name=1, code=None, rate=2, cause=3, step=4, check=5, details=6),
                        ["S206D"], lambda e: "Chamber", f"{f.name}｜No Error Code",
                        re.compile(r"^$"), name_only=True)

    outp.write_text(json.dumps(allx, ensure_ascii=False, indent=0))
    from collections import Counter
    print(f"共 {len(allx)} 个条目 →", outp)
    for k, v in Counter((x["src"].split("｜")[0], x["machine"], x["unit"]) for x in allx).items():
        print(f"  {v:5d}  {k}")
    sample = next(x for x in allx if x["error_code"] == "41-07")
    print(json.dumps(sample, ensure_ascii=False, indent=1)[:1500])


if __name__ == "__main__":
    main()
