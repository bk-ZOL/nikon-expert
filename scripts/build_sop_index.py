#!/usr/bin/env python3
"""重建 SOP 目录（src/sop_index.py）：把手册 / 检查表 / 自编 SOP 里的操作流程拆出来，算向量。

  .venv/bin/python scripts/build_sop_index.py                # 全部重建（向量有缓存，只算变了的）
  .venv/bin/python scripts/build_sop_index.py --doc 文件名     # 只重建一份
  .venv/bin/python scripts/build_sop_index.py --no-embed     # 只拆分不算向量（看拆分效果用）
  .venv/bin/python scripts/build_sop_index.py --status
  .venv/bin/python scripts/build_sop_index.py --show "查询词"  # 看检索结果
WPS 同步有资料变化时会自动跑一遍（scripts/sync_kwiki.py 末尾）。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from src import sop_index

ap = argparse.ArgumentParser()
ap.add_argument("--doc", default=None)
ap.add_argument("--no-embed", action="store_true")
ap.add_argument("--status", action="store_true")
ap.add_argument("--show", default=None)
a = ap.parse_args()

if a.status:
    print(sop_index.status())
elif a.show:
    for p in sop_index.search(a.show, k=8):
        print(f"{p['score']:.3f} [{p['doc_type']}] {p['title'][:70]} | {p['doc_name'][:50]} | {p['n_steps']}步")
else:
    print(sop_index.build(only_doc=a.doc, embed=not a.no_embed))
