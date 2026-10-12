"""给存量数据补机型标签（向量库 payload + 全文索引），不重新入库。
用法:
  .venv/bin/python scripts/retag_models.py          # 只预览
  .venv/bin/python scripts/retag_models.py --apply  # 真正写入
  加 --content：先读镜像里的文件内容判断机型（与入库同一套规则），判断不出来再按路径/文件名；
               预览时列出和现有标签不同的文档及判断依据。
故障履历(fault_history)每行自带机型，不动。
"""
import os, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
from src.machine_model import infer_model_multi, infer_model_for_file, normalize

try:
    import pysqlite3 as sq
except Exception:
    import sqlite3 as sq

APPLY = "--apply" in sys.argv
CONTENT = "--content" in sys.argv
MIRROR = Path("data/kwiki_mirror")
COL = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
tbl = "doc_fts"
names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
if tbl not in names:
    tbl = next(n for n in names if "fts" in n and not n.endswith(("_data", "_idx", "_content", "_docsize", "_config")))
docs = {}
for name, dtype, cur in con.execute(f"select doc_name, doc_type, max(machine_model) from {tbl} group by doc_name, doc_type"):
    docs.setdefault(name, (dtype, cur))

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
qc = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"), timeout=300)
try:   # doc_name 建索引：按文档名改/删不再全表扫描（同步删旧数据也受益）
    qc.create_payload_index(COL, field_name="doc_name", field_schema="keyword", wait=True)
    print("✓ doc_name 索引已就绪", flush=True)
except Exception as e:
    print(f"（doc_name 索引：{e}）", flush=True)
off = None
while True:
    pts, off = qc.scroll(COL, with_payload=["doc_name", "doc_type", "machine_model"],
                         with_vectors=False, limit=5000, offset=off)
    for pt in pts:
        pl = pt.payload or {}
        docs.setdefault(pl.get("doc_name"), (pl.get("doc_type"), pl.get("machine_model")))
    if off is None:
        break
docs.pop(None, None)

paths = {}
for f in MIRROR.rglob("*"):
    if f.is_file() and not f.name.startswith("."):
        paths.setdefault(f.name, []).append(str(f.relative_to(MIRROR)))

plan, why = {}, {}
for i, (name, (dtype, cur)) in enumerate(docs.items(), 1):
    if dtype == "fault_history":
        continue
    if CONTENT and name in paths:
        plan[name], why[name] = (lambda t: ((cur, t[0]), t[1]))(
            infer_model_for_file(MIRROR / paths[name][0], paths[name], name))
    else:
        plan[name] = (cur, infer_model_multi(paths.get(name, [""]), name))
    if CONTENT and i % 100 == 0:
        print(f"  …已读 {i}/{len(docs)}", flush=True)

if CONTENT:
    changed = {n: v for n, v in plan.items() if normalize(v[0] or "") != normalize(v[1])}
    print(f"\n按内容判断后，{len(changed)} 份文档的标签和现在不同：")
    for n, (cur, new) in sorted(changed.items(), key=lambda kv: (kv[1][1], kv[0])):
        print(f"  {cur or '(空)':14s} → {new:14s} {n[:70]}\n{'':34s}依据：{why.get(n, '无镜像文件，按路径/文件名')}")

cnt = Counter(new for _, new in plan.values())
print(f"共 {len(plan)} 份文档（不含故障履历）")
for tag, n in cnt.most_common():
    print(f"  {tag:16s} {n:4d} 份")
for tag in cnt:
    sample = [n for n, (_, new) in plan.items() if new == tag][:4]
    print(f"\n[{tag}] 例:")
    for n in sample:
        print("   ", n[:80])

if not APPLY:
    print("\n（预览模式，没写入。确认无误后加 --apply）")
    sys.exit(0)

print("\n写入中…")
for i, (name, (cur, new)) in enumerate(plan.items(), 1):
    con.execute(f"update {tbl} set machine_model=? where doc_name=?", (new, name))
    qc.set_payload(COL, payload={"machine_model": new},
                   points=FilterSelector(filter=Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=name))])))
    if i % 50 == 0:
        con.commit(); print(f"  …{i}/{len(plan)}", flush=True)
con.commit()
print(f"✓ 完成：{len(plan)} 份文档已补标签")
