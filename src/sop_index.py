"""src/sop_index.py —— 操作步骤（SOP）目录（2026-10-16）。

手册里的操作流程在全文索引里是按标题切开的：英文手册每一步一个标题（`## **1. Click [Select].**`），
检索只能命中零散的一两步。这里按入库顺序（doc_fts 的 rowid）把同一份资料的段落接回去，
认出「标题 → 1. → 2. → 3. …」这样的连续编号，拼成完整的一个流程存进目录：
  - 英文/中文手册：`1.` `2)` `Step 1` `步骤1`，标题或正文里的加粗行都算；
  - 日文检查表：`①②③`；
  - 自己写的 SOP（doc_type=sop）：整份文档就是一个 SOP，不拆。
流程标题取它前面最近的章节标题（`3.2 Operation Procedures › (1) Specifying …`），
页码取正文里的页脚（`■ 3-4 ■`、`P.20-00/33`）。

不调用大模型，规则拆分；只给「标题 + 前几步」算向量（bge-m3，中文问题也能找到英文手册的流程）。
查询时按向量相似度 + 标题关键词 + 机型加权排序，权限沿用全文索引的 doc_acl。
数据在 data/sop_index.db（独立库，不和全文检索抢锁）。重建：scripts/build_sop_index.py。
"""
import hashlib
import json
import os
import re
import threading
from collections import Counter

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

DB = os.getenv("SOP_INDEX_DB", "./data/sop_index.db")
SRC_TYPES = ("manual", "checksheet", "sop")
MIN_STEPS = 2               # 少于这么多步的不算流程
MAX_STEP_CHARS = 1500       # 一步的说明超过这么长就截断（多半是流程已经结束、后面接了别的内容）
MAX_STEPS = 60

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_STEP_RE = re.compile(r"^(?:[-*•]\s+)?(?:step\s*|步骤\s*|手順\s*|ステップ\s*)?(\d{1,2})\s*[.)．、](?!\d)\s*(\S.*)$", re.I)
_CIRCLED_RE = re.compile(rf"^(?:[-*•]\s+)?([{_CIRCLED}])\s*(\S.*)$")
_SECTION_RE = re.compile(r"^(?:chapter\s+|第)?\d+(?:\.\d+)+\.?\s+\S", re.I)      # 3.2 Operation Procedures
_SUBSEC_RE = re.compile(r"^[(（]\s*(?:\d{1,2}|[a-z])\s*[)）]\s*\S", re.I)           # (1) Specifying …
# 这些标题是步骤里的提示框，不是新章节
_BOLD_SUBSEC_RE = re.compile(r"^\s*(?:[-*•]\s+)?\*\*\s*[(（]\s*\d{1,2}\s*[)）]\s*[^*]{2,90}\*\*\s*$")
_NOTE_RE = re.compile(r"^(?:note|notes|caution|warning|danger|important|reference|tip|memo|"
                      r"注意|警告|危険|注記|参考|备注|備考|メモ|figure|fig\.|table|図|表)\b", re.I)
_PAGE_RES = [re.compile(r"■\s*([0-9]+(?:-[0-9]+)+)\s*■"), re.compile(r"\bP\.\s*([0-9]+(?:-[0-9]+)*(?:/[0-9]+)?)")]
_PIC_RE = re.compile(r"==>\s*picture .*? omitted\s*<==|intentionally omitted", re.I)
# 动作词：一步里有这些才像「操作」。带编号的原因列表、注意事项列表一般没有
_ACTION_RE = re.compile(
    r"\b(?:click|press|push|select|choose|enter|input|type|set|turn|switch|open|close|remove|install|attach|detach|"
    r"mount|dismount|insert|pull|loosen|tighten|fasten|connect|disconnect|plug|unplug|replace|adjust|measure|check|"
    r"confirm|verify|make sure|ensure|start|stop|execute|run|perform|exit|load|unload|move|rotate|align|clean|wipe|"
    r"apply|record|write down|read|wait|repeat|reboot|restart|power|log ?in|change|specify|place|put|lift|lower|"
    r"slide|release|lock|unlock|cut|attach|use|return|display|go to)\b|"
    r"確認|押[しす]|ｸﾘｯｸ|クリック|選択|入力|設定|外[しす]|取り[付外]|取付|取外|締め|緩め|接続|交換|調整|測定|実施|実行|起動|"
    r"開[けく]|閉[じめ]|切り?替|移動|回[しす]|挿入|抜[きく]|差し|ON\s*[にす]|OFF\s*[にす]|記録|読み|待[つち]|戻[しす]|"
    r"点击|按下|按|选择|输入|设定|设置|拆|装|安装|取下|拧|松|紧|连接|断开|更换|调整|测量|确认|检查|执行|启动|打开|关闭|"
    r"切换|移动|旋转|插|拔|记录|读取|等待|恢复|断电|上电|通电", re.I)
MIN_ACTION_RATIO = 0.5      # 至少一半的步骤带动作词


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS procedures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id TEXT, doc_name TEXT, doc_type TEXT, machine_model TEXT,
        title TEXT, breadcrumb TEXT, intro TEXT, steps TEXT, n_steps INTEGER,
        page TEXT, rid_start INTEGER, rid_end INTEGER,
        group_key TEXT, emb_key TEXT, emb BLOB);
    CREATE INDEX IF NOT EXISTS ix_proc_doc ON procedures(doc_id);
    CREATE INDEX IF NOT EXISTS ix_proc_name ON procedures(doc_name);
    """)
    return c


# ── 1. 拆分 ─────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    """去掉 markdown 修饰：加粗/斜体、docx 转出来的反斜杠转义、<br>。"""
    s = (s or "").replace("<br>", " ")
    s = re.sub(r"\\([\\.\-()\[\]_*#>+!])", r"\1", s)
    s = re.sub(r"~~|\*\*|__|(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", "", s)
    s = re.sub(r"^#+\s*", "", s.strip())
    s = re.sub(r"^>\s*", "", s)
    return s.strip()


def _step_of(line: str):
    """是步骤行就返回 (编号, 文字)。"""
    m = _STEP_RE.match(line)
    if m:
        return int(m.group(1)), m.group(2).strip()
    m = _CIRCLED_RE.match(line)
    if m:
        return _CIRCLED.index(m.group(1)) + 1, m.group(2).strip()
    return None


_XLSX_TITLE_RE = re.compile(r"^Sheet:(.*?)\s+Row:\d+$")
_XLSX_PREFIX_RE = re.compile(r"^[^|:\n]{2,80}?:\s+(?=\S)")
_XLSX_STEP_RE = re.compile(r"^(?:step\s*)?(\d{1,2})\s*[:：.]?\s*\|\s*", re.I)
_PROC_NAME_RE = re.compile(r"procedure|how[_ ]to|method|sop|手順|方法|步骤|流程|作业指导", re.I)
_SOP_NAME_RE = re.compile(r"(?:^|[^A-Za-z])SOP(?:[^A-Za-z]|$)|作业指导|操作规程|作業手順", re.I)
_SKIP_NAME_RE = re.compile(r"工作报告|日报|周报|月报|履历|记录|物料|耗材|清单|报价", re.I)   # 里面的编号列表是计划/清单，不是操作
_END_PUNCT = tuple("。.．!?！？)）」』:：")


def _xlsx_line(text: str) -> str:
    """表格行「表头: 1 | 内容」「表头: STEP1: | 内容」→「1. 内容」；其余去掉表头前缀、竖线换成空格。"""
    t = _XLSX_PREFIX_RE.sub("", text.strip(), count=1)
    m = _XLSX_STEP_RE.match(t)
    if m:
        return f"{m.group(1)}. " + t[m.end():].replace(" | ", " ")
    return t.replace(" | ", " ")


def _action_ratio(steps) -> float:
    return sum(1 for x in steps if _ACTION_RE.search(x["text"])) / max(len(steps), 1)


def _stream(rows):
    """把一份资料的段落按顺序摊成 (kind, text, rid)：kind=heading/body/page。
    同一份资料里反复出现的短行（页眉页脚）去掉，图片占位、图中文字块去掉。"""
    raw, sheet = [], None
    for r in rows:
        text = r["text"] or ""
        m = _XLSX_TITLE_RE.match(r["section_title"] or "")
        if m:                              # 表格：一行一段，标题是「Sheet:xx Row:n」，换表时才算新标题
            if m.group(1) != sheet:
                sheet = m.group(1)
                raw.append(("heading", sheet, r["rid"]))
            text = _xlsx_line(text)
        else:
            raw.append(("heading", r["section_title"] or "", r["rid"]))
        in_pic = False
        for ln in text.split("\n"):
            if "Start of picture text" in ln:
                in_pic = True
            if in_pic:
                if "End of picture text" in ln:
                    in_pic = False
                continue
            raw.append(("body", ln, r["rid"]))
    freq = Counter(_clean(t) for k, t, _ in raw if k == "body")
    out = []
    for kind, t, rid in raw:
        if kind == "body" and _BOLD_SUBSEC_RE.match(t):
            kind = "heading"               # 正文里整行加粗的「(3) Locking Out …」：小节标题没单独成段
            t = re.sub(r"^\s*[-*•]\s+", "", t)
        page = None
        for pr in _PAGE_RES:
            m = pr.search(t)
            if m:
                page = m.group(1)
                break
        if page:
            out.append(("page", page, rid))
        c = _clean(t)
        if not c or _PIC_RE.search(c) or c in ("■", "-"):
            continue
        if kind == "body" and len(c) < 120 and freq[c] >= 5 and not _step_of(c):
            continue                       # 页眉页脚（NSR/Maintenance V …、Nikon Confidential、株式会社ニコン）
        if page and len(c) < 30:
            continue                       # 只有页码的行
        out.append((kind, c, rid))
    return out


def split_procedures(rows) -> list:
    """rows: 同一份资料按 rowid 排好的段落（需有 rid/section_title/text）。返回流程列表。"""
    procs, cur = [], None
    sec = sub = ""                         # 当前章节、小节标题
    last_body = ""                         # 步骤 1 前最近的一句说明（"Use the following procedure …"）
    page = ""

    def close():
        nonlocal cur
        if cur and len(cur["steps"]) >= MIN_STEPS and _action_ratio(cur["steps"]) >= MIN_ACTION_RATIO:
            procs.append(cur)
        cur = None

    for kind, text, rid in _stream(rows):
        if kind == "page":
            page = text
            if cur:
                cur["page_end"] = text
            continue
        st = _step_of(text)
        if st:
            n, body = st
            if n == 1:
                close()
                title = sub or sec or last_body[:80]
                cur = {"title": title, "breadcrumb": " › ".join(x for x in (sec, sub) if x and x != title) or "",
                       "intro": last_body if last_body and last_body != title else "",
                       "steps": [{"n": 1, "text": body, "detail": ""}], "page": page, "page_end": page,
                       "rid_start": rid, "rid_end": rid}
                continue
            if cur and n == cur["steps"][-1]["n"] + 1 and len(cur["steps"]) < MAX_STEPS:
                cur["steps"].append({"n": n, "text": body, "detail": ""})
                cur["rid_end"] = rid
                continue
            if cur and n <= cur["steps"][-1]["n"]:
                close()                    # 编号倒回去了：一般是另一组列表，不是这个流程的
                continue
            # 跳号：当成说明文字
        if kind == "heading" and not _NOTE_RE.match(text) and not st:
            if _SECTION_RE.match(text):
                close()
                sec, sub, last_body = text, "", ""
                continue
            if _SUBSEC_RE.match(text) or (len(text) < 90 and not cur):
                close()
                sub, last_body = text, ""
                continue
            if len(text) < 90:             # 流程中间冒出一个普通标题：流程结束，这个标题当小节
                close()
                sub, last_body = text, ""
                continue
        if cur and not st:
            s = cur["steps"][-1]
            if kind == "body" and not s["detail"] and not s["text"].endswith(_END_PUNCT) \
                    and not re.match(r"^[-*•※注(（]", text) and len(s["text"]) < 300:
                # PDF 里一句话跨行断开（日文检查表常见）：接回步骤本身
                sep = "" if re.search(r"[\u3000-\u9fff\uff00-\uffef]$", s["text"]) else " "
                s["text"] = (s["text"] + sep + text).strip()
                cur["rid_end"] = rid
                continue
            if len(s["detail"]) < MAX_STEP_CHARS:
                s["detail"] = (s["detail"] + "\n" + text).strip()[:MAX_STEP_CHARS]
            cur["rid_end"] = rid
        elif not cur and kind == "body":
            last_body = text if len(text) < 200 else ""
    close()
    for p in procs:
        if p.get("page_end") and p["page_end"] != p["page"]:
            p["page"] = f"{p['page']}～{p['page_end']}" if p["page"] else p["page_end"]
        p.pop("page_end", None)
    return procs


def whole_doc(rows, doc_name: str) -> dict:
    """自己写的 SOP：整份就是一个流程，每个章节一步（目的 / 工具 / 作业步骤 …），内容全保留。"""
    steps = []
    for r in rows:
        head = _clean(r["section_title"] or "")
        text = r["text"] or ""
        m = _XLSX_TITLE_RE.match(r["section_title"] or "")
        if m:                              # 表格：一张表一节，行依次往下接
            head = m.group(1) if not steps or steps[-1]["text"] != m.group(1) else ""
            text = _xlsx_line(text)
        body = "\n".join(c for c in (_clean(x) for x in text.split("\n")) if c)
        if not body and not head:
            continue
        if steps and (not head or head == doc_name):
            steps[-1]["detail"] = (steps[-1]["detail"] + "\n" + body).strip()
            continue
        steps.append({"n": len(steps) + 1, "text": head or "（概要）", "detail": body})
    title = re.sub(r"\.(docx?|md|pdf|xlsx?)$", "", doc_name, flags=re.I)
    if steps and steps[0]["text"] in ("（概要）", doc_name):
        first = steps[0]["detail"].split("\n")[0].strip()
        if 4 <= len(first) <= 80:
            title = f"{first}（{title}）"
    return {"title": title, "breadcrumb": "", "intro": "", "steps": steps, "page": "",
            "rid_start": rows[0]["rid"] if rows else 0, "rid_end": rows[-1]["rid"] if rows else 0,
            "whole": True}


def _group_key(p: dict) -> str:
    """同一个流程常在多个版本的手册里重复（S306C / S307E 的 MNTV）：标题 + 前两步相同就算一组。"""
    norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())
    return hashlib.md5("|".join([norm(p["title"])] + [norm(s["text"]) for s in p["steps"][:2]])
                       .encode()).hexdigest()[:16]


def _embed_text(p: dict) -> str:
    head = " ".join(x for x in (p["breadcrumb"], p["title"], p.get("intro", "")) if x)
    steps = "；".join(s["text"] for s in p["steps"][:6])
    if p.get("whole"):                     # 整份收录的：小标题常常只是「目的」「Sheet1」，加上正文开头
        body = " ".join((s.get("detail") or "").replace("\n", " ") for s in p["steps"])
        return f"{head}\n{steps}\n{body}"[:1200]
    return f"{head}\n{steps}"[:800]


# ── 2. 建库 ─────────────────────────────────────────────────────────
def _src_rows(fts, doc_id=None):
    """从全文索引读段落。fts 可以是正式库（doc_fts）也可以是导出的库（rows 表）。"""
    tables = {r[0] for r in fts.execute("select name from sqlite_master")}
    if "doc_fts" in tables:
        sql = ("SELECT rowid AS rid, doc_id, doc_name, doc_type, machine_model, section_title, text "
               "FROM doc_fts WHERE doc_type IN ({})".format(",".join("?" * len(SRC_TYPES))))
    else:
        sql = ("SELECT rid, doc_id, doc_name, doc_type, machine_model, section_title, text "
               "FROM rows WHERE doc_type IN ({})".format(",".join("?" * len(SRC_TYPES))))
    params = list(SRC_TYPES)
    if doc_id:
        sql += " AND doc_id = ?"
        params.append(doc_id)
    fts.row_factory = sqlite3.Row
    return fts.execute(sql + " ORDER BY doc_id, rid", params)


def _doc_models(fts) -> dict:
    """机型标签以 doc_meta 为准（段落上的 machine_model 有的是空的）。"""
    try:
        return {r[0]: r[1] or "" for r in fts.execute("SELECT doc_id, machine_model FROM doc_meta")}
    except Exception:
        return {}


def _embedder():
    try:
        from llama_index.core import Settings
        if Settings._embed_model is not None:
            return Settings._embed_model
    except Exception:
        pass
    from src.api_embedding import APIEmbedding
    return APIEmbedding()


def build(fts_path: str = None, only_doc: str = None, embed: bool = True, log=print) -> dict:
    """重建目录。only_doc=doc_name 时只重建这一份（入库/同步后调用）。
    向量按文本缓存（emb_key），内容没变的流程不重新算。"""
    import numpy as np
    from src.fulltext import _db_path
    fts = sqlite3.connect(fts_path or _db_path)
    models = _doc_models(fts)
    c = _conn()
    old = {r["emb_key"]: r["emb"] for r in c.execute("SELECT emb_key, emb FROM procedures WHERE emb IS NOT NULL")}
    doc_id = None
    if only_doc:
        row = fts.execute("SELECT doc_id FROM doc_meta WHERE doc_name = ?", (only_doc,)).fetchone()
        c.execute("DELETE FROM procedures WHERE doc_name = ?", (only_doc,))
        if not row:
            c.commit()
            return {"docs": 0, "procedures": 0}
        doc_id = row[0]
    else:
        c.execute("DELETE FROM procedures")

    stats = Counter()
    pending = []                           # 要算向量的 (id, text)

    def flush(rows):
        if not rows:
            return
        r0 = rows[0]
        name = r0["doc_name"]
        if r0["doc_type"] != "sop" and _SKIP_NAME_RE.search(name):
            return
        if r0["doc_type"] == "sop" or _SOP_NAME_RE.search(name) or (r0["doc_type"] == "checksheet" and _PROC_NAME_RE.search(name)):
            procs = [whole_doc(rows, name)]          # 文件名就是一份 SOP / 某个作业方法：整份算一个，不拆
        else:
            procs = split_procedures(rows)
        model = models.get(r0["doc_id"]) or r0["machine_model"] or ""
        for p in procs:
            et = _embed_text(p)
            key = hashlib.md5(et.encode()).hexdigest()
            cur = c.execute(
                "INSERT INTO procedures(doc_id, doc_name, doc_type, machine_model, title, breadcrumb, intro, steps,"
                " n_steps, page, rid_start, rid_end, group_key, emb_key, emb) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r0["doc_id"], name, r0["doc_type"], model, p["title"], p["breadcrumb"], p["intro"],
                 json.dumps(p["steps"], ensure_ascii=False), len(p["steps"]), p["page"],
                 p["rid_start"], p["rid_end"], _group_key(p), key, old.get(key)))
            if old.get(key) is None:
                pending.append((cur.lastrowid, et))
        stats["docs"] += 1
        stats["procedures"] += len(procs)

    buf, cur_doc = [], None
    for r in _src_rows(fts, doc_id):
        if r["doc_id"] != cur_doc and buf:
            flush(buf)
            buf = []
        cur_doc = r["doc_id"]
        buf.append(r)
    flush(buf)
    c.commit()
    log(f"拆分完成：{stats['docs']} 份资料，{stats['procedures']} 个流程；需要新算向量 {len(pending)} 个")

    if embed and pending:
        emb = _embedder()
        B = 32
        for i in range(0, len(pending), B):
            chunk = pending[i:i + B]
            vecs = emb.get_text_embedding_batch([t for _, t in chunk])
            c.executemany("UPDATE procedures SET emb = ? WHERE id = ?",
                          [(np.asarray(v, dtype=np.float32).tobytes(), pid) for (pid, _), v in zip(chunk, vecs)])
            c.commit()
            if (i // B) % 20 == 0:
                log(f"  向量 {min(i + B, len(pending))}/{len(pending)}")
    _CACHE.clear()
    return dict(stats, embedded=len(pending) if embed else 0)


# ── 3. 查询 ─────────────────────────────────────────────────────────
_CACHE = {}
_LOCK = threading.Lock()


def _matrix():
    """全部流程的向量矩阵，按库文件修改时间缓存。"""
    import numpy as np
    mtime = os.path.getmtime(DB) if os.path.exists(DB) else 0
    with _LOCK:
        if _CACHE.get("mtime") == mtime:
            return _CACHE["ids"], _CACHE["mat"]
        c = _conn()
        ids, vecs = [], []
        for r in c.execute("SELECT id, emb FROM procedures WHERE emb IS NOT NULL"):
            ids.append(r["id"])
            vecs.append(np.frombuffer(r["emb"], dtype=np.float32))
        mat = np.vstack(vecs) if vecs else np.zeros((0, 1024), dtype=np.float32)
        if len(mat):
            mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        _CACHE.update(mtime=mtime, ids=ids, mat=mat)
        return ids, mat


def _allowed_docs(user):
    """权限：沿用全文索引的 doc_acl。security 关闭 → None（不过滤）。"""
    from src import acl
    from src.fulltext import _get_conn
    clause, params = acl.fts_acl_clause(user)
    if not clause:
        return None
    # fts_acl_clause 写的是 doc_fts.doc_id，这里给 doc_meta 起同样的别名复用
    rows = _get_conn().execute(f"SELECT doc_fts.doc_id FROM doc_meta doc_fts WHERE 1=1 {clause}", params)
    return {r[0] for r in rows}


_MODEL_IN_TEXT_RE = re.compile(r"(?<![\w.])(?:nsr-?)?(?:s?(?:20[5-8]|30[5-8])[a-z]?|sf\d{3}|[ae][rx]-?x\d)(?![\w.])", re.I)
_MODEL_WORD_RE = re.compile(r"^(?:nsr-?)?(?:s\d{3}[a-z]?|sf\d{3}|[ae][rx]-?x\d)$")


def _terms(q: str) -> list:
    words = [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}|\d+[a-z]+\d*", q.lower())
             if w not in {"the", "and", "how", "procedure", "sop", "step", "steps"}
             and not _MODEL_WORD_RE.match(w)]     # 机型只用来加权（model_factor），不当关键词
    cjk = [w for w in re.findall(r"[一-鿿぀-ヿｦ-ﾟ]{2,}", q)
           if w not in {"怎么", "如何", "步骤", "流程", "操作", "方法", "作业", "一下", "怎样"}]
    return words + cjk


def search(query: str, user=None, want_models=None, machine_model: str = "", k: int = 8) -> list:
    """返回流程列表（dict，steps 已解析），按相关度排好；同一组（多版本手册里的同一流程）只留一个，其余放 also_in。"""
    import numpy as np
    from src import machine_model as mm
    q = (query or "").strip()
    if not q:
        return []
    allowed = _allowed_docs(user)
    ids, mat = _matrix()
    scores = {}
    if len(mat):
        # 机型名不进向量：「S207D 卤素灯更换」会被标题带 S207 的资料带偏，机型交给 model_factor 加权
        qe = _MODEL_IN_TEXT_RE.sub(" ", q).strip() or q
        qv = np.asarray(_embedder().get_query_embedding(qe), dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-9
        sims = mat @ qv
        top = np.argsort(-sims)[:300]
        scores = {ids[i]: float(sims[i]) for i in top}
    c = _conn()
    terms = _terms(q)
    # 关键词：标题里出现查询词的也捞进来（向量没覆盖到的英文缩写、按钮名）
    for t in terms[:6]:
        for r in c.execute("SELECT id FROM procedures WHERE title LIKE ? OR breadcrumb LIKE ? LIMIT 200",
                           (f"%{t}%", f"%{t}%")):
            scores.setdefault(r["id"], 0.3)
    if not scores:
        return []
    want = want_models if want_models is not None else mm.models_in_query(q)
    if machine_model:
        want = mm.normalize(machine_model)
    rows = c.execute(f"SELECT * FROM procedures WHERE id IN ({','.join('?' * len(scores))})", list(scores)).fetchall()
    ranked = []
    for r in rows:
        if allowed is not None and r["doc_id"] not in allowed:
            continue
        if machine_model and not mm.matches_query_models(r["machine_model"], want):
            continue
        s = scores[r["id"]]
        head = f"{r['title']} {r['breadcrumb']}".lower()
        s += 0.06 * sum(1 for t in terms if t.lower() in head)
        if r["doc_type"] == "sop":
            s += 0.08                      # 自己写的 SOP：现场验证过、中文
        s *= 0.85 + 0.15 * mm.model_factor(r["machine_model"], q if not machine_model else machine_model)
        name_fams = mm.models_in_query(r["doc_name"])
        if want and name_fams and not any(f in want for f in name_fams):
            s *= 0.93                      # 文件名写明是别的机型（有的资料机型标签打错了，以文件名为准再压一下）
        s += 0.005 * min(r["n_steps"], 10)
        ranked.append((s, r))
    ranked.sort(key=lambda x: -x[0])
    out, groups = [], {}
    for s, r in ranked:
        g = r["group_key"]
        same = (r["doc_name"], r["breadcrumb"] or r["title"])
        if same in groups:                 # 同一章节的几部分 / 同标题的几段：只列一次（详情里合在一起）
            continue
        if g in groups:
            groups[g]["also_in"].append(r["doc_name"])
            continue
        d = dict(r)
        d.pop("emb", None)
        d["steps"] = json.loads(d["steps"])
        d["score"] = round(s, 3)
        d["also_in"] = []
        groups[g] = groups[same] = d
        out.append(d)
        if len(out) >= k:
            break
    return [expand(d, c) for d in out]


MAX_PARTS = 12              # 一个章节下超过这么多段流程就不合并（多半是参数说明这类大杂烩章节）


def expand(p: dict, c=None) -> dict:
    """同一章节（breadcrumb 相同）下的几段流程合成一个完整作业：
    如「11.1 Replacing the Halogen Lamp」下的 (1) Displaying the ITV Monitor、(2) Turning Off …、(3) Locking Out …。
    每一步带 part（所属小节），format_text 按小节分段显示。"""
    if not p.get("breadcrumb") or p.get("parts"):
        return p
    c = c or _conn()
    sibs = c.execute("SELECT * FROM procedures WHERE doc_id = ? AND breadcrumb = ? ORDER BY rid_start",
                     (p["doc_id"], p["breadcrumb"])).fetchall()
    if len(sibs) <= 1 or len(sibs) > MAX_PARTS:
        return p
    q = dict(p)
    q["steps"] = []
    for sb in sibs:
        for st in json.loads(sb["steps"]):
            st["part"] = sb["title"]
            q["steps"].append(st)
    q["parts"] = [sb["title"] for sb in sibs]
    q["hit_part"] = p["title"]
    q["title"], q["breadcrumb"] = p["breadcrumb"], ""
    q["n_steps"] = len(q["steps"])
    first, last = sibs[0]["page"] or "", sibs[-1]["page"] or ""
    a, b = first.split("～")[0], last.split("～")[-1]
    q["page"] = a if a == b else (f"{a}～{b}" if a and b else a or b)
    q["intro"] = sibs[0]["intro"] or ""
    return q


def get(pid: int, user=None):
    c = _conn()
    r = c.execute("SELECT * FROM procedures WHERE id = ?", (int(pid),)).fetchone()
    if not r:
        return None
    allowed = _allowed_docs(user)
    if allowed is not None and r["doc_id"] not in allowed:
        return None
    d = dict(r)
    d.pop("emb", None)
    d["steps"] = json.loads(d["steps"])
    d["also_in"] = [x["doc_name"] for x in c.execute(
        "SELECT DISTINCT doc_name FROM procedures WHERE group_key = ? AND id != ?", (r["group_key"], r["id"]))]
    return expand(d, c)


def cite(p: dict) -> str:
    parts = [p["doc_name"]]
    if p.get("breadcrumb"):
        parts.append(p["breadcrumb"])
    if p.get("page"):
        parts.append(f"页 {p['page']}")
    return " ｜ ".join(parts)


def format_text(p: dict, md: bool = True, detail_chars: int = 400, max_chars: int = 0) -> str:
    """原文步骤（不翻译、不改写）。detail_chars=-1 时每步说明全文列出；max_chars 限制总长。"""
    lines = [f"### {p['title']}" if md else f"【{p['title']}】",
             f"来源：{cite(p)}" + (f" ｜ 适用机型：{p['machine_model']}" if p.get("machine_model") else "")]
    if p.get("also_in"):
        lines.append("同样的流程也在：" + "、".join(sorted(set(p["also_in"]))[:5]))
    if p.get("intro"):
        lines.append(p["intro"])
    if p.get("parts"):
        lines.append(f"共 {len(p['parts'])} 部分：" + "、".join(p["parts"]))
    lines.append("")
    part = None
    for s in p["steps"]:
        if s.get("part") and s["part"] != part:
            part = s["part"]
            lines.append(f"\n#### {part}" if md else f"\n〔{part}〕")
        lines.append(f"{s['n']}. {('**' + s['text'] + '**') if md else s['text']}")
        if s.get("detail") and detail_chars:
            det = s["detail"] if detail_chars < 0 else \
                s["detail"][:detail_chars] + ("…" if len(s["detail"]) > detail_chars else "")
            lines.append("   " + det.replace("\n", "\n   "))
    out = "\n".join(lines)
    if max_chars and len(out) > max_chars:
        out = out[:max_chars] + f"\n……（后面还有，共 {p['n_steps']} 步/节，完整内容见原文）"
    return out


ORGANIZE_PROMPT = """你是光刻机现场工程师的助手。下面是从 Nikon 手册里原样摘出来的一个操作流程，请整理成中文的作业步骤，给现场工程师照着做。

要求：
1. **只用原文里有的内容**，不得补充原文没写的步骤、数值、工具、按钮。原文没写的项就写「原文未写」。
2. 正文直接写成通顺的中文，普通名词（清洁布、有机溶剂、装载臂…）直接用中文，**不要**写成「英文（中文）」。
   只有画面上能看到的按钮名、菜单名、窗口名、参数名、指令、数值、单位保持原文（如 [Select]、Focus Offset、-30.000 to 30.000 µm）。
3. 步骤编号和顺序与原文一致，不合并、不拆分；原文分了几部分（〔…〕）就按部分分小标题。每步写「做什么」，有画面变化/确认点的写成「→ 确认：…」。
4. 原文里的 NOTE / CAUTION / WARNING 放进对应步骤，标 ⚠️。
5. 结尾一行写来源：{cite}

输出格式（Markdown）：
### {title}（中文标题）
**适用 / 目的**：…
**前提条件**：…
**步骤**
1. …
**注意事项**：…
**来源**：…

工程师的问题（供你理解他关心什么，不要改变步骤内容）：{question}

原文：
{body}
"""


def organize(p: dict, question: str = "") -> str:
    """用大模型把原文步骤整理成中文 SOP（只整理、不补内容）。"""
    from src.ts_table import _llm
    body = format_text(p, md=False, detail_chars=-1, max_chars=24000)
    return _llm(ORGANIZE_PROMPT.format(cite=cite(p), title=p["title"], question=question or "（无）", body=body))


def lookup_for_agent(query: str, user=None, want_models=None, k: int = 3) -> str:
    hits = search(query, user=user, want_models=want_models, k=k)
    if not hits:
        return f"操作步骤目录里没找到与「{query}」相关的流程。"
    return "\n\n".join(format_text(p, md=False, detail_chars=300, max_chars=3500) for p in hits)


def status() -> dict:
    c = _conn()
    r = c.execute("SELECT COUNT(*), COUNT(DISTINCT doc_id), SUM(emb IS NOT NULL) FROM procedures").fetchone()
    return {"procedures": r[0], "docs": r[1], "embedded": r[2] or 0}
