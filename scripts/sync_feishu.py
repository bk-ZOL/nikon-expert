#!/usr/bin/env python3
"""飞书知识库 -> nikon-expert 增量同步（systemd: nikon-feishu-sync.timer，每 30 分钟）。

2026-10-16 起同步整个知识库（原来只同步一篇工作日志）。分工：自己写、持续更新的放飞书，其他文件放 WPS。
  - 标题带「履历 / 日志 / 日报」、能按日期行拆开的 docx → 工作日志（每天一条，doc_type=feishu_worklog）
  - 其余 docx → 转 markdown（标题、列表保留，截图 OCR 放回原位）按标题切块入库；
    标题或所在目录带 SOP 的算 sop（进 SOP 速查），其余算 manual
  - 直接上传的文件（obj_type=file）→ 下载后走 WPS 同步同一套入库（ingest_dispatch）
  - 目录节点自己正文很短（只是个目录）就不入库
增量：按节点的最后编辑时间判断，变了才重读；内容哈希也没变就不重灌。
知识库里删掉的文档同步删库（单轮上限 FEISHU_MAX_DELETE）；改名先入新名再删旧名。
有变化时最后刷新 SOP 目录。

配置（.feishu_env / .env）：
  FEISHU_WORKLOG_URL   知识库里任意一篇文档的 wiki 链接，用来定位知识库（必填，沿用原来的工作日志链接）
  FEISHU_SPACE_ID      可选，直接给知识库 id
  FEISHU_EXCLUDE       不同步的标题，逗号分隔（默认「个人业务量化」；目录被排除时下面的文档一起排除）
  MACHINE_MODEL        推断不出机型时的默认标签（默认 NSR-S207D）

  python scripts/sync_feishu.py            # 增量同步
  python scripts/sync_feishu.py --dry-run  # 只列出会做什么
  python scripts/sync_feishu.py --force    # 全部重读重灌
"""
import os
import re
import sys
import json
import hashlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(ROOT / ".feishu_env")
load_dotenv(ROOT / ".env", override=False)

import feishu_client as fc

STATE_FILE = ROOT / "data" / "feishu_sync_state.json"
MIRROR = ROOT / "data" / "feishu_mirror"          # 直接上传的文件下载到这里
MAX_DELETE = int(os.getenv("FEISHU_MAX_DELETE", "10"))
MIN_DOC_CHARS = int(os.getenv("FEISHU_MIN_DOC_CHARS", "150"))   # 目录节点：正文比这短、又有下级的不入库
OCR_MAX_TEXT = int(os.getenv("OFFICE_IMAGE_OCR_MAX_TEXT", "2000"))
SOP_RE = re.compile(r"SOP|作业指导|操作规程|作業手順", re.I)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def _excluded(n: dict, words) -> bool:
    parts = [p for p in n["path"].split("/") if p] + [n["title"]]
    return any(w and w in p for w in words for p in parts)


def _model_for(n: dict) -> str:
    from src.machine_model import infer_model
    try:
        tag = infer_model(n["path"], n["title"])
    except Exception:
        tag = ""
    return tag or os.getenv("MACHINE_MODEL", "NSR-S207D")


def _acl(doc_name: str):
    try:
        from src.acl_classify import apply_doc_acl
        apply_doc_acl(doc_name)
    except Exception as e:
        print(f"    ⚠️  定级失败（不影响入库）：{e}")


def _delete(doc_name: str):
    from qdrant_client import QdrantClient
    from src.ingestor import _delete_existing
    qc = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    try:
        _delete_existing(qc, os.getenv("COLLECTION_NAME", "nikon_expert_v1"), doc_name)
    finally:
        qc.close()


def _doc_markdown(obj_token: str) -> str:
    """docx → markdown。正文少（内容主要是截图）的才对截图做 OCR，和 WPS 文档同一规则。"""
    from src.ingestor import ocr_image_bytes
    blocks = fc.read_docx_blocks(obj_token)
    plain = fc.blocks_to_markdown(blocks)
    if len(plain) > OCR_MAX_TEXT or not any(b.get("block_type") == 27 for b in blocks):
        return plain
    cache = {}

    def image_text(tok):
        if tok not in cache:
            try:
                cache[tok] = ocr_image_bytes(fc.download_media(tok))
            except Exception as e:
                print(f"    ⚠️  截图下载/识别失败：{e}")
                cache[tok] = ""
        return cache[tok]
    md = fc.blocks_to_markdown(blocks, image_text=image_text)
    print(f"    🖼  截图 OCR：{len(cache)} 张，{sum(1 for v in cache.values() if v)} 张认出文字")
    return md


def _sync_docx(n: dict, has_child: bool, dry: bool) -> dict:
    """返回写进 state 的记录（kind / doc_name / hash / chunks），不入库的 kind=skip。"""
    title = n["title"]
    if fc.WORKLOG_TITLE_RE.search(title):
        content = fc.read_docx_raw(n["obj_token"])
        days = fc.parse_worklog(content)
        if days:
            h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            rec = {"kind": "worklog", "doc_name": title, "hash": h, "days": len(days),
                   "date_range": f"{days[-1]['date']}…{days[0]['date']}"}
            if dry or n.get("_same_hash") == h:
                return rec
            from src.ingest_worklog import ingest_worklog_days
            rec["chunks"] = ingest_worklog_days(days, title, n["obj_token"], wiki_node=n["node_token"],
                                                last_edited=n["edit_time"], machine_model=_model_for(n))
            return rec
    md = _doc_markdown(n["obj_token"])
    h = hashlib.sha256(md.encode("utf-8")).hexdigest()[:16]
    if has_child and len(md) < MIN_DOC_CHARS:
        return {"kind": "skip", "doc_name": title, "hash": h, "why": "目录页"}
    doc_type = "sop" if SOP_RE.search(title) or SOP_RE.search(n["path"]) else "manual"
    rec = {"kind": doc_type, "doc_name": title, "hash": h}
    if dry or n.get("_same_hash") == h:
        return rec
    from src.ingest_docx import ingest_markdown
    print(f"\n📄 摄入飞书文档：{title}  [{doc_type}]")
    rec["chunks"] = ingest_markdown(md, title, n["url"], doc_type=doc_type, machine_model=_model_for(n),
                                    doc_key="feishu:" + n["node_token"])
    _acl(title)
    return rec


def _sync_file(n: dict, dry: bool) -> dict:
    from sync_kwiki import ingest_dispatch
    name = n["title"]
    rec = {"kind": "file", "doc_name": name}
    if dry:
        return rec
    data = fc.download_file(n["obj_token"])
    rec["hash"] = hashlib.sha256(data).hexdigest()[:16]
    if n.get("_same_hash") == rec["hash"]:
        return rec
    dest = MIRROR / (n["path"] or ".") / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"\n📎 摄入飞书附件：{name}")
    doc_type = "sop" if SOP_RE.search(name) or SOP_RE.search(n["path"]) else "manual"
    ingest_dispatch(dest, doc_type)
    return rec


def main():
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    url = os.getenv("FEISHU_WORKLOG_URL", "").strip()
    space = os.getenv("FEISHU_SPACE_ID", "").strip() or (fc.space_of(url) if url else "")
    if not space:
        sys.exit("缺少 FEISHU_WORKLOG_URL / FEISHU_SPACE_ID（在 .feishu_env 配）")
    exclude = [w.strip() for w in os.getenv("FEISHU_EXCLUDE", "个人业务量化").split(",")]

    state = _load_state()
    nodes_state = state.get("nodes", {})
    # 老格式（只有一篇工作日志，按 document_id 记）：hash 搬过来，避免第一次全量重灌
    legacy = {k: v for k, v in state.items() if k not in ("nodes", "worklogs", "space_id", "synced_at")}

    if not dry:
        from sync_kwiki import _ensure_embed
        _ensure_embed()                            # 云端 bge-m3（和 WPS 同步一样），不在服务器 CPU 上算向量
    nodes = fc.list_space(space)
    parents = {n["path"] for n in nodes}
    print(f"知识库 {space}：{len(nodes)} 个节点")

    stats = {"ingested": 0, "unchanged": 0, "skipped": 0, "deleted": 0, "renamed": 0, "failed": 0}
    seen = set()
    for n in nodes:
        full = f"{n['path']}/{n['title']}".strip("/")
        if _excluded(n, exclude):
            print(f"  ⏭  排除：{full}")
            stats["skipped"] += 1
            continue
        if n["obj_type"] not in ("docx", "file"):
            print(f"  ⏭  暂不支持的类型 {n['obj_type']}：{full}")
            stats["skipped"] += 1
            continue
        seen.add(n["node_token"])
        old = nodes_state.get(n["node_token"]) or {}
        if not old and n["obj_token"] in legacy:
            old = {"hash": legacy[n["obj_token"]].get("hash"), "doc_name": legacy[n["obj_token"]].get("title"),
                   "kind": "worklog"}
        if not force and old.get("edit_time") == n["edit_time"] and old.get("doc_name") == n["title"]:
            stats["unchanged"] += 1
            continue
        n["_same_hash"] = None if force or old.get("doc_name") != n["title"] else old.get("hash")
        has_child = full in parents
        try:
            rec = _sync_docx(n, has_child, dry) if n["obj_type"] == "docx" else _sync_file(n, dry)
        except Exception as e:
            print(f"  ❌ {full}：{e}")
            stats["failed"] += 1
            continue
        same = rec.get("hash") and rec.get("hash") == n["_same_hash"]
        tag = {"worklog": "工作日志", "sop": "SOP", "manual": "资料", "file": "附件", "skip": "不入库"}[rec["kind"]]
        print(f"  {'·' if same else ('🔎' if dry else '✅')} [{tag}] {full}" +
              (f"（{rec.get('why')}）" if rec.get("why") else "") + ("（内容没变）" if same else ""))
        if dry:
            continue
        if rec["kind"] == "skip" or same:
            stats["unchanged" if same else "skipped"] += 1
        else:
            stats["ingested"] += 1
        if old.get("doc_name") and old["doc_name"] != rec["doc_name"] and old.get("kind") != "skip":
            _delete(old["doc_name"])               # 改名：新名已入库，删旧名
            print(f"    ↻ 改名，删掉旧名：{old['doc_name']}")
            stats["renamed"] += 1
        rec.update(edit_time=n["edit_time"], title=n["title"], path=n["path"], url=n["url"],
                   synced_at=dt.datetime.now().isoformat(timespec="seconds"))
        nodes_state[n["node_token"]] = rec
        if not dry:
            state["nodes"] = nodes_state
            _save_state(state)

    if dry:
        print(f"\n（--dry-run，没有改动）")
        return

    # 知识库里删掉 / 排除掉的 → 库里删除
    gone = [k for k in nodes_state if k not in seen]
    if gone and len(gone) > MAX_DELETE:
        print(f"\n⚠️  有 {len(gone)} 篇在知识库里找不到，超过单轮上限 {MAX_DELETE}，本轮不删。")
    else:
        for k in gone:
            v = nodes_state.pop(k)
            if v.get("kind") != "skip" and v.get("doc_name"):
                _delete(v["doc_name"])
                print(f"  🗑  知识库已删除，库里同步删除：{v['doc_name']}")
            stats["deleted"] += 1

    for k in list(legacy):                          # 老格式记录已经搬进 nodes
        state.pop(k, None)
    state["nodes"] = nodes_state
    state["space_id"] = space
    state["worklogs"] = [{"title": v["doc_name"], "url": v["url"]}
                         for v in nodes_state.values() if v.get("kind") == "worklog"]
    state["synced_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _save_state(state)

    print("\n" + "=" * 50)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["ingested"] or stats["deleted"] or stats["renamed"]:
        try:
            from src import sop_index
            print("\n刷新 SOP 目录 ...")
            print(" ", sop_index.build(log=lambda m: print("  " + m)))
        except Exception as e:
            print(f"  ⚠️  SOP 目录刷新失败（不影响同步）: {e}")


if __name__ == "__main__":
    main()
