"""重新入库电路图（带 OCR 补标签），然后全量重建图纸倒排索引。
用法: .venv/bin/python scripts/reingest_circuits.py [文件名关键字 ...]
"""
import sys, time
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dotenv import load_dotenv; load_dotenv(".env")
from pathlib import Path
from src.ingestor import ingest_circuit_pdf
from src.diagram_index import build_index
from src.machine_model import infer_model
import sync_kwiki as sk

SKIP = ("S207DS307E Electrical Circuit Diagram Rev. 1.0 (Volume",)  # 库里只留"已融合"版
files = [p for p in sorted(Path("data/kwiki_mirror").rglob("*.pdf"))
         if "circuit diagram" in p.name.lower() and "［Driver］" not in str(p)
         and not any(p.name.startswith(s) for s in SKIP)]
if sys.argv[1:]:
    files = [p for p in files if any(k in p.name for k in sys.argv[1:])]
print(f"共 {len(files)} 份电路图", flush=True)
for p in files:
    t = time.time()
    n = ingest_circuit_pdf(str(p), machine_model=infer_model(str(p.relative_to("data/kwiki_mirror")), p.name))
    print(f"  → {n} 页，用时 {(time.time()-t)/60:.1f} 分钟", flush=True)
print("重建倒排索引:", build_index(incremental=False), flush=True)
