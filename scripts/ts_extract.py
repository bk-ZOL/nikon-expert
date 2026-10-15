"""Troubleshooting 积累表：从故障履历 / 工作日志 / 客户交付物里抽取「解过的 trouble」并归并（src/ts_table.py）。
已处理过的原文段落会跳过，可以反复跑（定时任务每天跑一次，只处理新增/改过的内容）。

用法（在 /opt/nikon-expert 下）：
  .venv/bin/python scripts/ts_extract.py                  # 处理所有新内容
  .venv/bin/python scripts/ts_extract.py --limit 3        # 只处理 3 段（试跑）
  .venv/bin/python scripts/ts_extract.py --doc 北京厂内    # 只处理文件名含这个词的资料
  .venv/bin/python scripts/ts_extract.py --stats          # 只看统计
  .venv/bin/python scripts/ts_extract.py --import 记录.xlsx --customer xingyue   # 导入人工整理好的「报警处理记录」表
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--doc", default=None)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--import", dest="xlsx", default=None)
    ap.add_argument("--customer", default="none")
    ap.add_argument("--import-oem", dest="oem", default=None, help="导入 ts_parse_oem_guide.py 生成的 JSON")
    a = ap.parse_args()
    from src import ts_table
    if (a.xlsx or not a.stats) and not a.oem:
        from src.engine import _init_engine
        _init_engine()                      # 初始化 LLM（deepseek）
    if a.oem:
        ts_table.import_oem(a.oem, log=lambda m: print(m, flush=True))
    elif a.xlsx:
        st = ts_table.import_xlsx(a.xlsx, customer=a.customer, log=lambda m: print(m, flush=True))
        print(f"导入案例 {st['cases']} 条，归并到 {st['entries']} 个条目（已确认）")
    elif not a.stats:
        st = ts_table.run(limit=a.limit, only_doc=a.doc, log=lambda m: print(m, flush=True))
        print(f"\n处理原文 {st['windows']} 段（跳过已处理 {st['skipped']}），新增案例 {st['cases']}，更新条目 {st['entries']}")
    c = ts_table._conn()
    print("条目：", dict(c.execute("SELECT status, count(*) FROM ts_entries GROUP BY status").fetchall()),
          " 案例：", c.execute("SELECT count(*) FROM ts_cases").fetchone()[0])
    for r in c.execute("SELECT machine, unit, count(*), sum(occ_count) FROM ts_entries GROUP BY 1,2 ORDER BY 3 DESC"):
        print(f"  {r[0]:6s} {r[1]:14s} 条目 {r[2]:3d}  发生 {r[3] or 0} 次")


if __name__ == "__main__":
    main()
