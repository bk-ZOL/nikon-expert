#!/usr/bin/env python3
"""scripts/embed_bundle.py —— 在本机(Mac)嵌向量，产出可上云的 bundle。

本机干重活(BGE-M3 embedding，Apple Silicon/MPS 快)，产出一个 .nebundle(向量点 + FTS 行)，
scp 到服务器后由 import_bundle.py 秒导入(不重嵌) + 自动定级。用于批量把大手册预入库，
避开云端 2 核 CPU 慢。

    python scripts/embed_bundle.py 手册.pdf [输出.nebundle]
    python scripts/embed_bundle.py ~/手册目录/*.pdf        # 多个文件各产一个 bundle

原理：用临时 staging 的文件模式 Qdrant + FTS 跑一遍现有摄入(保证分块/向量/元数据与
服务器完全一致)，再把该文档的点(含向量)和 FTS 行导出成 bundle，最后清理 staging。
"""
import glob
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _embed_one(src: str, out: str) -> int:
    stage = tempfile.mkdtemp(prefix="nebundle_")
    # 强制文件模式 Qdrant + 独立 FTS，避免污染本地库、避免连云端
    os.environ.pop("QDRANT_URL", None)
    os.environ["QDRANT_PATH"] = os.path.join(stage, "qdrant")
    os.environ["FTS_DB_PATH"] = os.path.join(stage, "fts.db")
    os.environ.setdefault("COLLECTION_NAME", "nikon_expert_v1")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, ".env"))
        # .env 里可能有 QDRANT_URL，再强制清掉走文件模式
        os.environ.pop("QDRANT_URL", None)
        os.environ["QDRANT_PATH"] = os.path.join(stage, "qdrant")
        os.environ["FTS_DB_PATH"] = os.path.join(stage, "fts.db")

        doc_name = os.path.basename(src)
        print(f"⚙️  本机嵌向量：{doc_name}（BGE-M3 / MPS）…")
        # 先初始化引擎并在 staging 里建好空 collection，否则 ingest 的"先删旧点"会因
        # collection 不存在报错（服务器上 collection 常在所以没这问题）。
        from src.engine import _init_engine, ingest_file
        import src.engine as _E
        from qdrant_client import models as qm
        _init_engine()
        qc0, coll0 = _E._engine["client"], _E._engine["collection"]
        try:
            qc0.get_collection(coll0)
        except Exception:
            qc0.create_collection(coll0, vectors_config=qm.VectorParams(
                size=1024, distance=qm.Distance.COSINE))   # BGE-M3 dense = 1024
        r = ingest_file(src)
        print("   ", r.get("message") if isinstance(r, dict) else r)

        # 导出该文档的 Qdrant 点(含向量)——复用 ingest 那个 client（文件模式不能双开）
        from qdrant_client import models as qm
        import src.engine as _E
        qc = _E._engine["client"]
        coll = _E._engine["collection"]
        flt = qm.Filter(must=[qm.FieldCondition(key="doc_name", match=qm.MatchValue(value=doc_name))])
        points, offset = [], None
        while True:
            batch, offset = qc.scroll(coll, scroll_filter=flt, with_payload=True,
                                      with_vectors=True, limit=256, offset=offset)
            for p in batch:
                points.append({"id": str(p.id), "vector": p.vector, "payload": p.payload})
            if offset is None:
                break

        # 导出 FTS 行(doc_fts + doc_meta)
        import sqlite3
        conn = sqlite3.connect(os.environ["FTS_DB_PATH"])
        fts_rows = [list(x) for x in conn.execute(
            "SELECT doc_id,doc_name,doc_type,machine_model,section_title,text "
            "FROM doc_fts WHERE doc_name=?", (doc_name,))]
        meta_rows = [list(x) for x in conn.execute(
            "SELECT doc_id,doc_name,doc_type,file_path,machine_model "
            "FROM doc_meta WHERE doc_name=?", (doc_name,))]
        conn.close()

        if not points:
            print("   ✗ 未产出向量点，跳过"); return 1
        bundle = {"version": 1, "doc_name": doc_name, "collection": coll,
                  "qdrant_points": points, "fts_rows": fts_rows, "meta_rows": meta_rows}
        with open(out, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False)
        mb = os.path.getsize(out) / 1e6
        print(f"   ✓ bundle：{out}  ({len(points)} 点 / {len(fts_rows)} FTS 行 / {mb:.1f}MB)")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("用法：python scripts/embed_bundle.py <文件|通配符> [输出.nebundle]")
        return 1
    # 若第二个参数是输出名(单文件场景)
    out_arg = None
    files = []
    for a in args:
        expanded = glob.glob(os.path.expanduser(a))
        if expanded:
            files.extend(expanded)
        elif a.endswith(".nebundle"):
            out_arg = a
        else:
            print(f"   ⚠️ 找不到：{a}")
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        print("没有可处理的文件"); return 1
    rc = 0
    for i, src in enumerate(files, 1):
        src = os.path.abspath(src)
        out = out_arg if (out_arg and len(files) == 1) else src + ".nebundle"
        print(f"\n[{i}/{len(files)}] {os.path.basename(src)}")
        rc |= _embed_one(src, out)
    print(f"\n完成。把 *.nebundle scp 到服务器后跑 import_bundle.py 导入。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
