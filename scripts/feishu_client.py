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
# 行首日期：2026/08/5、2026-08-03、2026.8.3 均可；
# 兼容「日期后同一行跟标题」的新写法，如「2026/10/03 苏州星钥光子 S207D（8632018）」
_DATE_RE = re.compile(r"^[ \t　]*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:[ \t　].*)?$", re.M)


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


# ── 整个知识库（2026-10-16：从一篇工作日志扩大到整个 wiki 空间）────────────
def space_of(url_or_token: str) -> str:
    """wiki 链接所在知识库的 space_id。"""
    m = _WIKI_RE.search(url_or_token)
    node = m.group(1) if m else url_or_token.strip()
    r = requests.get(f"{DOMAIN}/open-apis/wiki/v2/spaces/get_node", params={"token": node},
                     headers=_headers(), timeout=20).json()
    if r.get("code"):
        raise RuntimeError(f"解析 wiki 节点失败：{r.get('code')} {r.get('msg')}")
    return r["data"]["node"]["space_id"]


def list_space(space_id: str, parent: str = None, path: str = "", depth: int = 0, max_depth: int = 8):
    """深度优先列出知识库全部节点：[{node_token, obj_token, obj_type, title, path, edit_time, url}]。
    path 是上级标题用「/」连起来（不含自己）。"""
    out, tok = [], ""
    while True:
        p = {"page_size": 50}
        if parent:
            p["parent_node_token"] = parent
        if tok:
            p["page_token"] = tok
        r = requests.get(f"{DOMAIN}/open-apis/wiki/v2/spaces/{space_id}/nodes", params=p,
                         headers=_headers(), timeout=30).json()
        if r.get("code"):
            raise RuntimeError(f"列知识库节点失败：{r.get('code')} {r.get('msg')}")
        for it in r["data"].get("items", []):
            out.append({"node_token": it["node_token"], "obj_token": it["obj_token"],
                        "obj_type": it["obj_type"], "title": (it.get("title") or "").strip(),
                        "path": path, "edit_time": str(it.get("obj_edit_time") or ""),
                        "url": it.get("url") or ""})
            if it.get("has_child") and depth < max_depth:
                sub = f"{path}/{it.get('title', '')}".strip("/")
                out += list_space(space_id, it["node_token"], sub, depth + 1, max_depth)
        if not r["data"].get("has_more"):
            return out
        tok = r["data"]["page_token"]


def read_docx_blocks(doc_id: str) -> list:
    blocks, tok = [], ""
    while True:
        p = {"page_size": 500}
        if tok:
            p["page_token"] = tok
        r = requests.get(f"{DOMAIN}/open-apis/docx/v1/documents/{doc_id}/blocks", params=p,
                         headers=_headers(), timeout=30).json()
        if r.get("code"):
            raise RuntimeError(f"读取文档块失败：{r.get('code')} {r.get('msg')}")
        blocks += r["data"].get("items", [])
        if not r["data"].get("has_more"):
            return blocks
        tok = r["data"]["page_token"]


def download_media(token: str) -> bytes:
    """文档里的图片 / 附件素材。"""
    r = requests.get(f"{DOMAIN}/open-apis/drive/v1/medias/{token}/download", headers=_headers(), timeout=60)
    if r.status_code != 200 or r.headers.get("content-type", "").startswith("application/json"):
        raise RuntimeError(f"下载素材失败：{r.status_code} {r.text[:200]}")
    return r.content


def download_file(token: str) -> bytes:
    """知识库里直接上传的文件（obj_type=file）。"""
    r = requests.get(f"{DOMAIN}/open-apis/drive/v1/files/{token}/download", headers=_headers(), timeout=120)
    if r.status_code != 200 or r.headers.get("content-type", "").startswith("application/json"):
        raise RuntimeError(f"下载文件失败：{r.status_code} {r.text[:200]}")
    return r.content


# 块类型：https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/data-structure
_BLOCK_KEY = {2: "text", 12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo", 19: "callout"}
_BLOCK_KEY.update({3 + i: f"heading{i + 1}" for i in range(9)})


def _elements_text(body: dict) -> str:
    parts = []
    for e in (body or {}).get("elements", []):
        if "text_run" in e:
            parts.append(e["text_run"].get("content", ""))
        elif "mention_doc" in e:
            parts.append(e["mention_doc"].get("title", ""))
        elif "equation" in e:
            parts.append(e["equation"].get("content", ""))
    return "".join(parts)


def blocks_to_markdown(blocks: list, image_text=None) -> str:
    """文档块 → markdown：标题 → #、列表 → - / 1.、表格单元格逐行。
    image_text(token) 返回截图里认出的文字（可为空），放回图片原来的位置。"""
    lines, seq = [], {}
    for b in blocks:
        bt = b.get("block_type")
        if bt == 1:                        # page：文档标题，doc_name 里已经有了
            continue
        if bt == 27:
            tok = (b.get("image") or {}).get("token")
            txt = image_text(tok) if (image_text and tok) else ""
            if txt:
                lines.append("【截图文字】\n" + txt)
            continue
        key = _BLOCK_KEY.get(bt)
        if not key:
            continue
        text = _elements_text(b.get(key)).strip()
        if bt != 13:
            seq.pop(b.get("parent_id"), None)
        if not text:
            continue
        if key.startswith("heading"):
            lines.append("#" * min(int(key[7:]), 6) + " " + text)
        elif bt == 12:
            lines.append("- " + text)
        elif bt == 13:                     # 有序列表：飞书不给编号，按同一父块下连续出现的次数数
            n = seq.get(b.get("parent_id"), 0) + 1
            seq[b.get("parent_id")] = n
            lines.append(f"{n}. {text}")
        elif bt == 17:
            done = (b.get("todo") or {}).get("style", {}).get("done")
            lines.append(("- [x] " if done else "- [ ] ") + text)
        elif bt == 14:
            lines.append("```\n" + text + "\n```")
        elif bt == 15:
            lines.append("> " + text)
        else:
            lines.append(text)
    return "\n\n".join(lines).strip()


WORKLOG_TITLE_RE = re.compile(r"履历|日志|日报")


def worklog_urls() -> list:
    """所有工作日志的链接：同步时在知识库里发现的（data/feishu_sync_state.json 的 worklogs），
    没有的话退回 FEISHU_WORKLOG_URL。"""
    import json
    st = Path(__file__).resolve().parent.parent / "data" / "feishu_sync_state.json"
    try:
        wl = json.loads(st.read_text()).get("worklogs") or []
        if wl:
            return [w["url"] for w in wl if w.get("url")]
    except Exception:
        pass
    url = os.getenv("FEISHU_WORKLOG_URL", "").strip()
    return [url] if url else []


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


def fetch_worklogs(site: str = ""):
    """所有工作日志（site 非空时只要标题含它的），每篇一个 fetch_worklog 结果。"""
    out = []
    for url in worklog_urls():
        wl = fetch_worklog(url)
        if not site or site.lower() in (wl.get("title") or "").lower():
            out.append(wl)
    return out


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
