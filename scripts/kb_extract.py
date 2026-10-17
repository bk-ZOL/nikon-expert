#!/usr/bin/env python3
"""夜间提炼（src/kb_extract.py）：用 Mac 上的本地模型提炼故障排查、给 SOP 分模块。由 nikon-kb-extract.timer 每晚启动。

  .venv/bin/python scripts/kb_extract.py                 # 跑到北京时间 KB_UNTIL（默认 06:30）为止
  .venv/bin/python scripts/kb_extract.py --minutes 20    # 只跑 20 分钟（试跑）
  .venv/bin/python scripts/kb_extract.py --only oem|field|units
  .venv/bin/python scripts/kb_extract.py --status
Mac 不在线或正在用就停，下次接着做（做过的不重做）。
"""
import argparse
import datetime as dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from src import kb_extract, local_llm

ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=0)
ap.add_argument("--only", default="")
ap.add_argument("--status", action="store_true")
a = ap.parse_args()

if a.status:
    print(kb_extract.status())
    sys.exit(0)

now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
if a.minutes:
    deadline = time.time() + a.minutes * 60
else:
    hh, mm = (int(x) for x in os.getenv("KB_UNTIL", "06:30").split(":"))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= now:
        end += dt.timedelta(days=1)
    deadline = end.timestamp()
print(f"开始 {now:%F %T}（北京），最晚到 {dt.datetime.fromtimestamp(deadline, now.tzinfo):%F %T}")

ok, why = local_llm.available()
if not ok:
    print(f"⏸  {why}，今晚不跑")
    sys.exit(0)
local_llm.keep_awake(int(deadline - time.time()) + 600)    # 跑的这段时间不让 Mac 闲置睡眠

order = [x for x in ("field", "oem", "units") if not a.only or x == a.only]
for kind in order:
    if time.time() > deadline:
        break
    print(f"\n== {kind}")
    fn = {"field": kb_extract.run_field, "oem": kb_extract.run_oem, "units": kb_extract.run_units}[kind]
    r = fn(deadline)
    print(f"   {r}")
    if r.get("paused"):
        break
print("\n", kb_extract.status())
