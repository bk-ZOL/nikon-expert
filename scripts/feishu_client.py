#!/usr/bin/env python3
"""飞书云文档读取 + 工作日志解析。

A 路（灌库 sync_feishu.py）与 B 路（MCP 工具 nikon_query_worklog）共用：
  - 把 wiki / docx 链接解析成 document_id
  - 读文档纯文本
  - 按日期把日志切成每天一条（容错 2026/08/5、2026-08-03 等混合格式）

鉴权走 feishu_auth.get_user_token()（自动带 user_access_token，过期自动刷新/提示）。
"""
import os
import re
import sys
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import feishu_auth

DOMAIN = feishu_auth.DOMAIN.rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {feishu_auth.get_user_token()}"}


# ── 链接 / 节点解析 ────────────────────────────────────────────────
_WIKI_RE = re.compile(r"/wiki/(\w+)")
_DOCX_RE = re.compile(r"/docx/(\w+)")


def resolve_wiki_node(node_token: str):
    """wiki 节点 token -> (document_id, obj_type, title)。需要 wiki:wiki:readonly。"""
    r = requests.get(
        f"{DOMAIN}/open-apis/wiki/v2/spaces/get_node",
        params={"token": node_token, "obj_type": "wiki"},
        headers=_headers(), timeout=20,
    ).json()
    if r.get("code"):
        raise RuntimeError(f"解析 wiki 节点失败：{r.get('code')} {r.get('msg')}")
    n = r["data"]["node"]
    return n["obj_token"], n.get("obj_type"), n.get("title")


def resolve_url(url_or_token: str):
    """把 wiki/docx 链接或裸 token 解析成 (document_id, title, wiki_node)。

    wiki_node 为空表示不是知识库文档。
    """
    m = _WIKI_RE.search(url_or_token)
    if m:
        node = m.group(1)
        doc_id, _otype, title = resolve_wiki_node(node)
        return doc_id, title, node
    m = _DOCX_RE.search(url_or_token)
    if m:
        return m.group(1), None, ""
    # 认为直接给了 document_id
    return url_or_token.strip(), None, ""


# ── 读取 ──────────────────────────────────────────────────────────
def read_docx_raw(doc_id: str) -> str:
    """读 docx 纯文本。需要 docx:document:readonly。"""
    r = requests.get(
        f"{DOMAIN}/open-apis/docx/v1/documents/{doc_id}/raw_content",
        headers=_headers(), timeout=30,
    ).json()
    if r.get("code"):
        raise RuntimeError(f"读取文档失败：{r.get('code')} {r.get('msg')}")
    return r["data"]["content"]


def get_last_edit(doc_id: str):
    """尽力取文档最后编辑时间戳（秒）；取不到返回 None（不阻塞同步）。"""
    try:
        r = requests.post(
            f"{DOMAIN}/open-apis/drive/v1/metas/batch_query",
            headers=_headers(),
            json={"request_docs": [{"doc_token": doc_id, "doc_type": "docx"}]},
            timeout=20,
        ).json()
        metas = r.get("data", {}).get("metas") or []
        if metas:
            t = metas[0].get("latest_modify_time")
            return int(t) if t else None
    except Exception:
        pass
    return None


# ── 工作日志解析（按日期切块）─────────────────────────────────────
# 行首日期：2026/08/5、2026-08-03、2026.8.3 均可
_DATE_RE = re.compile(r"^[ \t]*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})[ \t]*$", re.M)


def parse_worklog(content: str):
    """按日期把日志切成 [{'date':'YYYY-MM-DD', 'text': 当天全文}], 最新在前。"""
    marks = [
        (m.start(), f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}")
        for m in _DATE_RE.finditer(content)
    ]
    days = []
    for i, (pos, date) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(content)
        text = content[pos:end].strip()
        if text:
            days.append({"date": date, "text": text})
    return days


def fetch_worklog(url_or_token: str):
    """高层：链接/节点 -> dict(doc_id, title, wiki_node, content, last_edit, days)。"""
    doc_id, title, node = resolve_url(url_or_token)
    content = read_docx_raw(doc_id)
    return {
        "doc_id": doc_id,
        "title": title,
        "wiki_node": node,
        "content": content,
        "last_edit": get_last_edit(doc_id),
        "days": parse_worklog(content),
    }


if __name__ == "__main__":
    # 自测： python scripts/feishu_client.py <wiki/docx 链接或 token>
    url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("FEISHU_WORKLOG_URL", "")
    if not url:
        sys.exit("用法：feishu_client.py <飞书文档链接>")
    wl = fetch_worklog(url)
    print(f"文档：{wl['title']} | doc_id={wl['doc_id']} | {len(wl['days'])} 天 | "
          f"{len(wl['content'])} 字")
    if wl["days"]:
        print(f"最新 {wl['days'][0]['date']} … 最早 {wl['days'][-1]['date']}")
