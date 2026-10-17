"""修正库里失效的文件路径（全文索引 doc_meta + 向量库 payload 的 file_path），不重新入库。

失效的常见原因：在 Mac 上入库后导入服务器（路径是 /Users/...）、网页上传时记的是临时目录、
WPS 里文件夹改了名（同步按文件 id 判断没变，不会重新入库，路径就留在旧文件夹）。
做法：按文件名在 data/ 下找唯一的同名文件，换成 data/... 相对路径；找不到或同名多个的不动，列出来。

用法（在 /opt/nikon-expert 下）:
  .venv/bin/python scripts/fix_file_paths.py          # 只预览
  .venv/bin/python scripts/fix_file_paths.py --apply  # 写入；旧路径先备份到 data/file_path_backup_<时间>.json
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")

try:
    import pysqlite3 as sq
except Exception:
    import sqlite3 as sq

APPLY = "--apply" in sys.argv
DATA = Path("data")
COL = os.getenv("COLLECTION_NAME", "nikon_expert_v1")

con = sq.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
rows = con.execute("select doc_name, file_path, count(*) from doc_meta "
                   "where coalesce(file_path,'')!='' group by doc_name, file_path").fetchall()
bad = [(n, f, c) for n, f, c in rows if not os.path.exists(f)]
if not bad:
    print("所有文档的路径都有效，不用改。")
    sys.exit(0)

by_name = {}
for p in DATA.rglob("*"):
    if p.is_file() and not p.name.startswith("."):
        by_name.setdefault(p.name, []).append(str(p))

plan, skip = [], []
for name, old, n in bad:
    hits = by_name.get(Path(old).name) or by_name.get(name) or []
    if len(hits) == 1:
        plan.append((name, old, hits[0], n))
    else:
        skip.append((name, old, "找不到同名文件" if not hits else f"有 {len(hits)} 个同名文件：{hits[:3]}"))

print(f"路径失效的文档 {len(bad)} 份：")
for name, old, new, n in plan:
    print(f"  ✓ {name}（{n} 条）\n      旧：{old}\n      新：{new}")
for name, old, why in skip:
    print(f"  ✗ {name}：{why}，不改\n      旧：{old}")

if not APPLY:
    print("\n（预览模式，没写入。确认无误后加 --apply）")
    sys.exit(0)
if not plan:
    sys.exit(0)

backup = DATA / f"file_path_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
backup.write_text(json.dumps([{"doc_name": n, "old": o, "new": w} for n, o, w, _ in plan],
                             ensure_ascii=False, indent=1))
print(f"\n旧路径已备份：{backup}")

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
qc = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"), timeout=300)
for name, old, new, _ in plan:
    con.execute("update doc_meta set file_path=? where doc_name=? and file_path=?", (new, name, old))
    qc.set_payload(COL, payload={"file_path": new}, points=FilterSelector(filter=Filter(must=[
        FieldCondition(key="doc_name", match=MatchValue(value=name)),
        FieldCondition(key="file_path", match=MatchValue(value=old))])))
    print(f"  ✓ 已改：{name}")
con.commit()
print("完成。")
