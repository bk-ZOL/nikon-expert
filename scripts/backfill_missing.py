"""只补镜像里有、库里没有的文件；逐个回查 chunk 数，分类报告。"""
import sys
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dotenv import load_dotenv; load_dotenv(".env")
from pathlib import Path
import fitz
from qdrant_client.models import Filter, FieldCondition, MatchValue
from src.ingestor import _get_storage
import sync_kwiki as sk

COL, MIRROR = "nikon_expert_v1", Path("data/kwiki_mirror")
EXT = {".pdf", ".docx", ".xlsx"}
_, _, c = _get_storage()

def count(name):
    f = Filter(must=[FieldCondition(key="doc_name", match=MatchValue(value=name))])
    return c.count(COL, count_filter=f, exact=True).count

names, off = set(), None
while True:
    pts, off = c.scroll(COL, with_payload=["doc_name"], with_vectors=False,
                        limit=2000, offset=off)
    names |= {p.payload.get("doc_name") for p in pts}
    if off is None: break

todo = [p for p in sorted(MIRROR.rglob("*"))
        if p.is_file() and p.suffix.lower() in EXT
        and "［Driver］" not in str(p) and p.name not in names]
print(f"待补 {len(todo)} 个\n", flush=True)

res = {"成功": [], "0个chunk": [], "报错": [], "跳过": []}
for p in todo:
    rel = str(p.relative_to(MIRROR))
    if p.suffix.lower() == ".pdf":
        try:
            d = fitz.open(p); n = d.page_count
            t = sum(len(d[i].get_text().strip()) for i in range(min(5, n)))
        except Exception as e:
            res["跳过"].append((rel, f"打不开: {e}")); continue
        if t <= 100:
            res["跳过"].append((rel, "无文字层，需 OCR")); continue
    doc_type, _ = sk.map_dir(rel)
    print(f"⚙ {rel} [{doc_type}]", flush=True)
    try:
        sk.ingest_dispatch(p, doc_type)
    except Exception as e:
        res["报错"].append((rel, repr(e)[:200])); continue
    n = count(p.name)
    res["成功" if n else "0个chunk"].append((rel, f"{n} chunks"))

print("\n" + "=" * 60)
for k, items in res.items():
    print(f"\n【{k}】{len(items)} 个")
    for rel, why in items:
        print(f"  {why:24s} {rel}")
