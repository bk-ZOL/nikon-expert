#!/usr/bin/env python3
"""把用户 Mac 上「~/Documents/02_设备技术资料」里库里没有的资料直接入库（2026-10-17）。

本地文件先 rsync 到服务器 data/local_docs/（保留原来的「机型/分类/…」目录），清单 manifest.json 由 Mac 上去重后生成：
  [{"rel": "NSR-S205C/1_原厂手册/xxx.pdf", "sha": "...", "size": 123}, ...]
这里再按内容指纹跳过已入库的（data/local_ingest_state.json），可以中断、接着跑。

  .venv/bin/python scripts/ingest_local.py data/local_docs/manifest.json            # 入库
  .venv/bin/python scripts/ingest_local.py data/local_docs/manifest.json --dry-run  # 只列出会做什么
  .venv/bin/python scripts/ingest_local.py data/local_docs/manifest.json --limit 20

分类 → doc_type：1_原厂手册 / 4_知识整理 / 5_Troubleshooting / 6_培训资料 / 7_光刻胶 → manual，3_SOP → sop，2_电路图 → 电路图流程。
老格式 .xls / .doc 先用 LibreOffice 转成 .xlsx / .docx；.pptx 现有入库流程不支持，记下来跳过。
WPS / 飞书同步正在跑时先等它跑完（两边都写全文索引和向量库）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

STATE = ROOT / "data" / "local_ingest_state.json"
DOC_TYPE = {"3_SOP": "sop"}
CONVERT = {".xls": "xlsx", ".doc": "docx"}


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def _wait_syncs():
    for unit in ("nikon-wps-sync.service", "nikon-feishu-sync.service"):
        while subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
            print(f"  … {unit} 正在跑，等 60 秒")
            time.sleep(60)


def _convert(path: Path, fmt: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="local_conv_"))
    r = subprocess.run(["soffice", "--headless", "--convert-to", fmt, "--outdir", str(tmp), str(path)],
                       capture_output=True, text=True, timeout=600)
    out = tmp / (path.stem + "." + fmt)
    if not out.exists():
        raise RuntimeError("LibreOffice 转换失败: " + (r.stderr or r.stdout).strip()[:200])
    return out


def ingest_one(base: Path, rel: str) -> str:
    from sync_kwiki import ingest_dispatch, _ensure_embed
    from src.machine_model import infer_model_for_file
    src = base / rel
    parts = Path(rel).parts
    category = parts[1] if len(parts) > 2 else ""
    suffix = src.suffix.lower()
    if suffix == ".pptx":
        return "跳过：pptx 暂不支持"
    path = src
    if suffix in CONVERT:
        path = _convert(src, CONVERT[suffix])
    if category == "2_电路图" and path.suffix.lower() == ".pdf":
        _ensure_embed()
        from src.ingestor import ingest_circuit_pdf
        tag, why = infer_model_for_file(path, [rel], path.name)
        ingest_circuit_pdf(str(path), machine_model=tag)
        subprocess.run(["systemctl", "start", "--no-block", "nikon-circuit-recognize.service"], check=False)
        try:
            from src.acl_classify import apply_doc_acl
            apply_doc_acl(path.name)
        except Exception:
            pass
        return f"电路图（机型 {tag}，{why}），已启动后台识图"
    # 机型：本地目录名就写着机型（NSR-S205C/…），让按内容判断时也能参考路径
    import sync_kwiki
    sync_kwiki._model_for = lambda p, _rel=rel: _model(p, _rel)
    ingest_dispatch(path, DOC_TYPE.get(category, "manual"))
    return "ok"


def _model(path, rel):
    from src.machine_model import infer_model_for_file
    tag, why = infer_model_for_file(path, [rel], Path(path).name)
    print(f"    机型 {tag}（{why}）")
    return tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    manifest = Path(a.manifest)
    base = manifest.parent
    items = json.loads(manifest.read_text())
    st = _load()
    todo = [x for x in items if x["sha"] not in st]
    print(f"清单 {len(items)} 份，已入库 {len(items) - len(todo)} 份，这次要做 {len(todo)} 份")
    if a.dry_run:
        for x in todo[:50]:
            print("  ", x["rel"])
        return
    n = 0
    for x in todo:
        if a.limit and n >= a.limit:
            break
        _wait_syncs()
        print(f"\n[{n + 1}/{len(todo)}] {x['rel']}")
        try:
            msg = ingest_one(base, x["rel"])
        except Exception as e:
            msg = f"失败：{type(e).__name__}: {e}"
        print("   →", msg)
        if not msg.startswith("失败"):          # 失败的不记，下次再试
            st[x["sha"]] = {"rel": x["rel"], "result": msg, "at": time.strftime("%F %T")}
            _save(st)
        n += 1
    if n:
        from src import sop_index, glossary
        print("\n刷新 SOP 目录和缩写对照表 ...")
        print(" ", sop_index.build(log=lambda m: print("  " + m)))
        glossary.build(log=lambda m: print("  " + m))


if __name__ == "__main__":
    main()
