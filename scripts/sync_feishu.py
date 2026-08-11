#!/usr/bin/env python3
"""飞书工作日志 -> nikon-expert 增量同步（可挂 systemd timer）。

增量判据：整篇 raw_content 的内容哈希。变了才「按 doc_name 删旧向量+FTS，再全量重灌」，
避免日志被编辑后残留重复向量。加 --force 可强制重灌。

配置（.feishu_env / .env）：
  FEISHU_WORKLOG_URL   工作日志的 wiki/docx 链接或 document_id（必填）
  FEISHU_WORKLOG_NAME  可选，覆盖 doc_name（默认取文档标题）
  MACHINE_MODEL        可选机型标签（默认 NSR-S207D）
"""
import os
import sys
import json
import hashlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".feishu_env")
load_dotenv(ROOT / ".env", override=False)

import feishu_client
from src.ingest_worklog import ingest_worklog_days

STATE_FILE = ROOT / "data" / "feishu_sync_state.json"


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


def main():
    force = "--force" in sys.argv
    url = os.getenv("FEISHU_WORKLOG_URL", "").strip()
    if not url:
        sys.exit("缺少 FEISHU_WORKLOG_URL（在 .feishu_env 配工作日志链接）")

    doc_id, title, node = feishu_client.resolve_url(url)
    content = feishu_client.read_docx_raw(doc_id)
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    state = _load_state()
    prev = state.get(doc_id, {})
    if prev.get("hash") == h and not force:
        print(f"⏭  内容未变（hash {h}），跳过。用 --force 可强制重灌。")
        return

    days = feishu_client.parse_worklog(content)
    if not days:
        sys.exit("未从文档解析出任何按日期的日报，检查文档格式/日期行")

    last_edit = feishu_client.get_last_edit(doc_id)
    le = dt.datetime.fromtimestamp(last_edit).isoformat() if last_edit else ""
    doc_name = os.getenv("FEISHU_WORKLOG_NAME") or title or "飞书工作日志"
    machine_model = os.getenv("MACHINE_MODEL", "NSR-S207D")

    n = ingest_worklog_days(days, doc_name, doc_id, wiki_node=node,
                            last_edited=le, machine_model=machine_model)

    state[doc_id] = {
        "hash": h,
        "title": doc_name,
        "wiki_node": node,
        "days": len(days),
        "chunks": n,
        "last_edited": le,
        "date_range": f"{days[-1]['date']}…{days[0]['date']}",
        "synced_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _save_state(state)
    print(f"✅ 同步完成：{doc_name} — {len(days)} 天 / {n} chunk"
          f"（范围 {days[-1]['date']}…{days[0]['date']}，hash {h}）")


if __name__ == "__main__":
    main()
