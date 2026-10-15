"""src/acl_classify.py —— 文档定级规则 + 摄入侧自动定级挂钩。

规则(方案 6.1 / 9 章)被 scripts/classify_docs.py(批量) 和 摄入管线(增量) 共用，
保证"上传/同步即定级即可查"，不再需要事后手动补跑。

apply_doc_acl(doc_name)：摄入完成后调用——按文档名定级写 doc_acl，并把 ACL 字段
写进该文档在 Qdrant 里的所有 chunk payload。best-effort：失败只告警不影响摄入
（未定级文档会因缺 approved 标记而 fail-closed 不可见，安全的一侧）。
"""
import os
import re
import sqlite3

# 客户词典：文件名/路径命中 → 归属域
CUSTOMER_DICT = [
    (("星钥", "苏州星钥", "xingyue"), "customer:xingyue"),
    (("欣奕华", "xinyihua"), "customer:xinyihua"),
    (("长春", "changchun"), "customer:changchun"),
    (("鹏程", "pcl"), "customer:pcl"),
    (("8632018",), "customer:8632018"),
]

OEM_NAME_PAT = re.compile(
    r"(Technical Information|Maintenance Guide|SM_UsersManual|レチクル設計|Reticle Design"
    r"|^EA\d|^SD\d-EXX4|^LC[-/]|^A0-EXX4|-ELCIRC-|-ELSMIF|^HFE-|^INL-|^PPD-|^RCBR-"
    r"|MCSV .*(Function|Manual)|FOUNDATION|Nikon NSR|Constants and Programs Backup"
    r"|技术规格书"
    # S308F（X6 平台）原厂资料：检查表、修理手册、要因分析（2026-10-15）
    r"|S308F-|(?<![A-Z0-9])(?:AR|EX)?-?X6(?![0-9])|チェックシート|ﾁｪｯｸｼｰﾄ|REPAIR MANUAL|修理マニュアル|トラブル要因)",
    re.IGNORECASE,
)
OEM_TEXT_MARK = re.compile(r"NIKON CORPORATION|CONFIDENTIAL|S\d{3}E-RF-", re.IGNORECASE)


def _row(sec_level, owner_scope, review_status, source_class):
    return {"sec_level": sec_level, "owner_scope": owner_scope,
            "review_status": review_status, "source_class": source_class}


def _match_customer(name: str):
    for keys, scope in CUSTOMER_DICT:
        if any(k in name for k in keys):
            return scope
    return None


def classify(doc_name: str, doc_type: str) -> dict:
    """返回 {sec_level, owner_scope, review_status, source_class}。判不出→兜底不可见。"""
    name = doc_name or ""

    if doc_type == "circuit_diagram" or OEM_NAME_PAT.search(name):
        return _row(3, "vendor:nikon", "approved", "oem_manual")
    if any(k in name for k in ("技术应答表", "应答表", "报价", "承诺", "应答")):
        return _row(4, "internal", "pending", "commercial")
    if any(k in name for k in ("ATP", "翻新报告", "工作报告", "验收")):
        scope = _match_customer(name)
        return _row(4, scope or "internal", "pending", "customer_deliverable")
    scope = _match_customer(name)
    if scope:
        return _row(4, scope, "pending", "customer_deliverable")
    if doc_type in ("fault_history", "worklog", "feishu_worklog") or any(
        k in name for k in ("履历", "故障排查", "调试日志", "worklog")
    ):
        return _row(3, "internal", "approved", "fault_history")
    if "系统实现方案" in name or "Nikon_Expert" in name:
        return _row(3, "internal", "approved", "internal_sop")
    if doc_type in ("sop", "checksheet") or any(
        k in name for k in ("SOP", "操作规程", "Checksheet", "清单", "常用命令",
                            "计测", "流程", "调整", "作业", "备件", "耗材", "指南", "规范")
    ):
        return _row(2, "internal", "approved", "internal_sop")
    if any(k in name for k in ("会议纪要", "PPMI", "纪要")):
        return _row(4, "internal", "pending", None)
    if OEM_TEXT_MARK.search(name):
        return _row(3, "vendor:nikon", "approved", "oem_manual")
    if doc_type == "manual":
        return _row(3, "internal", "approved", "internal_sop")
    if doc_type == "obsidian_note":
        return _row(2, "internal", "approved", "internal_sop")
    return _row(4, "internal", "pending", None)  # 判不出 = 不可见 + 待办


# ── v2 细分维度：类别 / 机型 / 客户（2026-10-15）──────────────────────
def category_of(doc_type: str, source_class: str) -> str:
    """资料类别（acl.CATEGORIES 的 key）。"""
    if doc_type == "circuit_diagram":
        return "circuit"
    if source_class == "oem_manual":
        return "oem_manual"
    if source_class in ("customer_deliverable", "commercial"):
        return source_class
    if source_class == "fault_history":
        return "worklog" if doc_type in ("worklog", "feishu_worklog") else "fault_history"
    if source_class == "internal_sop":
        return "sop"
    return "other"


def customer_of(owner_scope: str) -> str:
    return owner_scope.split(":", 1)[1] if (owner_scope or "").startswith("customer:") else "none"


def models_of(tags) -> list:
    """机型标签（可多个、可能是旧写法）→ 机型族列表；没标签或通用资料 → ["ALL"]。"""
    from src.machine_model import normalize
    fams = []
    for t in tags or []:
        fams.extend(normalize(t))
    fams = [f for f in dict.fromkeys(fams) if f]
    if not fams or "COMMON" in fams:
        return ["ALL"]
    return fams


def models_str(models: list) -> str:
    """存进 doc_acl.models 的写法：前后带逗号，便于 LIKE '%,S207D,%'。"""
    return "," + ",".join(models) + ","


def doc_models(conn, doc_name: str) -> list:
    """从全文索引取这份资料的机型标签。"""
    rows = conn.execute("SELECT DISTINCT machine_model FROM doc_fts WHERE doc_name = ?", (doc_name,)).fetchall()
    tags = [r[0] for r in rows if r[0]]
    if not tags:
        try:
            tags = [r[0] for r in conn.execute(
                "SELECT DISTINCT machine_model FROM doc_meta WHERE doc_name = ?", (doc_name,)).fetchall() if r[0]]
        except Exception:
            pass
    return models_of(tags)


# ── 摄入侧增量定级 ───────────────────────────────────────────────────
def apply_doc_acl(doc_name: str) -> bool:
    """摄入某文档后自动定级：写 doc_acl(SQLite) + 回填 Qdrant payload。
    按 doc_name 关联(同名多 doc_id/多 chunk 取同一定级)。失败只告警返回 False。"""
    if not doc_name:
        return False
    try:
        db = os.getenv("FTS_DB_PATH", "./data/fts.db")
        conn = sqlite3.connect(db)
        # 从刚写入的 doc_meta 取该文档的 doc_id / doc_type
        rows = conn.execute(
            "SELECT doc_id, doc_type FROM doc_meta WHERE doc_name = ?", (doc_name,)
        ).fetchall()
        if not rows:
            conn.close()
            return False
        doc_type = rows[0][1] or ""
        c = classify(doc_name, doc_type)
        c["category"] = category_of(doc_type, c["source_class"])
        c["customer"] = customer_of(c["owner_scope"])
        c["models"] = doc_models(conn, doc_name)

        for (doc_id, _dt) in rows:
            # 保护人工定级：已被人改过(classified_by != 'auto')的不覆盖
            ex = conn.execute(
                "SELECT classified_by FROM doc_acl WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if ex and ex[0] and ex[0] != "auto":
                continue
            conn.execute(
                """INSERT INTO doc_acl
                   (doc_id, filename, sec_level, owner_scope, review_status,
                    source_class, category, models, customer, classified_by, classified_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'auto', datetime('now'))
                   ON CONFLICT(doc_id) DO UPDATE SET
                     filename=excluded.filename, sec_level=excluded.sec_level,
                     owner_scope=excluded.owner_scope, review_status=excluded.review_status,
                     source_class=excluded.source_class, category=excluded.category,
                     models=excluded.models, customer=excluded.customer, classified_by='auto',
                     classified_at=datetime('now')""",
                (doc_id, doc_name, c["sec_level"], c["owner_scope"],
                 c["review_status"], c["source_class"], c["category"],
                 models_str(c["models"]), c["customer"]),
            )
        conn.commit()
        # 回填 Qdrant payload：以 doc_acl 里的实际值为准（人工改过的不会被自动定级覆盖）
        actual = acl_row_for(conn, doc_name) or c
        conn.close()
        _set_qdrant_payload(doc_name, actual)
        return True
    except Exception as e:
        print(f"⚠️  自动定级失败（{doc_name}）：{e}（该文档暂不可见，可事后 classify_docs 补）")
        return False


def acl_row_for(conn, doc_name: str):
    """doc_acl 里这份资料的权限（同名多 doc_id 取第一条）。"""
    r = conn.execute(
        "SELECT sec_level, owner_scope, review_status, category, models, customer "
        "FROM doc_acl WHERE filename = ? ORDER BY doc_id LIMIT 1", (doc_name,)).fetchone()
    if not r:
        return None
    return {"sec_level": r[0], "owner_scope": r[1], "review_status": r[2], "category": r[3] or "other",
            "models": [m for m in (r[4] or ",ALL,").split(",") if m], "customer": r[5] or "none"}


def _set_qdrant_payload(doc_name: str, c: dict):
    url = os.getenv("QDRANT_URL")
    if not url:
        return  # 文件模式(无 URL)不处理；生产是 server 模式
    from qdrant_client import QdrantClient, models as qm
    collection = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    client = QdrantClient(url=url)
    client.set_payload(
        collection_name=collection,
        payload={"sec_level": c["sec_level"], "owner_scope": c["owner_scope"],
                 "review_status": c["review_status"], "acl_users": [],
                 "category": c["category"], "models": c["models"], "customer": c["customer"]},
        points=qm.Filter(must=[qm.FieldCondition(
            key="doc_name", match=qm.MatchValue(value=doc_name))]),
    )
    client.close()
