"""src/acl_admin.py —— 权限管理后台（网页「审计 / 权限管理」页用，2026-10-15）。

  · 账号：新建、改角色/密级、勾选可见的 类别 / 机型 / 客户、有效期、停用、重置密码；
  · 资料：按类别/机型/客户/状态/关键词筛选，单份或批量改 类别 / 机型 / 客户 / 密级 / 审核状态；
  · 预览：选一个账号，列出他能看到 / 看不到哪些资料（附原因）。

改资料权限时同时写 doc_acl（SQLite，全文检索用）和 Qdrant payload（语义检索用），
classified_by 记成 admin:<操作人>，以后自动定级不会覆盖人工修改。
所有修改记进 access_log（操作「改权限」），审计页可查。
"""
import os
from datetime import datetime, timedelta, timezone

from src import acl

ROLE_LABELS = {
    "trainee": "实习生", "engineer": "工程师", "senior_engineer": "资深工程师",
    "manager": "主管", "kb_admin": "管理员", "auditor": "审计员（只看日志）",
}
STATUS_LABELS = {"active": "启用", "suspended": "停用", "left": "离职"}
REVIEW_LABELS = {"approved": "已审核", "pending": "待审核", "rejected": "已驳回"}


def _conn():
    try:
        import pysqlite3 as sqlite3
    except ImportError:
        import sqlite3
    c = sqlite3.connect(os.getenv("FTS_DB_PATH", "./data/fts.db"))
    c.row_factory = sqlite3.Row
    return c


def _log(admin: str, detail: str):
    try:
        from src import audit
        audit.log(admin or "?", f"改权限：{detail}"[:200], ok=True)
    except Exception:
        pass


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_models(models) -> str:
    m = [x for x in models if x]
    return "全部" if (not m or acl.ALL_MODELS in m) else "、".join(m)


# ── 账号 ──────────────────────────────────────────────────────────────
def list_users() -> list:
    c = _conn()
    out = []
    now = _now_iso()
    cust_names = acl.customers()
    for u in c.execute("SELECT uid, display_name, role, clearance, status FROM users ORDER BY uid"):
        scopes = [r[0] for r in c.execute(
            "SELECT scope FROM user_scopes WHERE uid=? AND valid_until > ?", (u["uid"], now))]
        until = c.execute("SELECT max(valid_until) FROM user_scopes WHERE uid=?", (u["uid"],)).fetchone()[0]
        cats, models, custs = acl.parse_grants(scopes)
        out.append({
            "账号": u["uid"], "名称": u["display_name"], "角色": ROLE_LABELS.get(u["role"], u["role"]),
            "密级": f"L{min(u['clearance'], acl.ROLE_MAX_CLEARANCE.get(u['role'], 0))}",
            "可看类别": "、".join(acl.CATEGORIES[x] for x in acl.CATEGORIES if x in cats) or "（无）",
            "可看机型": _fmt_models(models),
            "可看客户": "、".join(cust_names.get(x, x) for x in sorted(custs)) or "（无）",
            "有效期至": (until or "")[:10], "状态": STATUS_LABELS.get(u["status"], u["status"]),
        })
    c.close()
    return out


def get_user(uid: str) -> dict:
    c = _conn()
    u = c.execute("SELECT uid, display_name, role, clearance, status FROM users WHERE uid=?", (uid,)).fetchone()
    if not u:
        c.close()
        return {}
    scopes = [r[0] for r in c.execute(
        "SELECT scope FROM user_scopes WHERE uid=? AND valid_until > ?", (uid, _now_iso()))]
    until = c.execute("SELECT max(valid_until) FROM user_scopes WHERE uid=?", (uid,)).fetchone()[0]
    c.close()
    cats, models, custs = acl.parse_grants(scopes)
    return {"uid": u["uid"], "display_name": u["display_name"], "role": u["role"], "clearance": u["clearance"],
            "status": u["status"], "cats": sorted(cats), "models": sorted(models), "custs": sorted(custs),
            "valid_until": (until or "")[:10]}


def save_user(admin: str, uid: str, display_name: str, role: str, clearance: int, cats: list,
              models: list, custs: list, valid_until: str, status: str, new_pw: str, create: bool) -> str:
    uid = (uid or "").strip()
    if not uid or not uid.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return "⚠️ 账号名只能用字母、数字、下划线、横线、点。"
    if role not in acl.ROLE_MAX_CLEARANCE:
        return "⚠️ 角色无效。"
    cap = acl.ROLE_MAX_CLEARANCE[role]
    clearance = int(clearance or 0)
    note = ""
    if clearance > cap:
        note = f"（{ROLE_LABELS[role]}最高 L{cap}，密级已按 L{cap} 生效）"
    try:
        until = datetime.strptime((valid_until or "").strip(), "%Y-%m-%d")
    except ValueError:
        until = datetime.now() + timedelta(days=365 * 3)
    until_iso = until.strftime("%Y-%m-%dT23:59:59Z")
    models = [m for m in (models or [])]
    if not models or acl.ALL_MODELS in models:
        models = [acl.ALL_MODELS]
    scopes = sorted({f"cat:{x}" for x in (cats or []) if x in acl.CATEGORIES}
                    | {f"model:{m}" for m in models}
                    | {f"cust:{x}" for x in (custs or [])})
    c = _conn()
    exists = c.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone()
    if create and exists:
        c.close()
        return f"⚠️ 账号 {uid} 已存在，请在下拉框里选它再修改。"
    if not create and not exists:
        c.close()
        return f"⚠️ 账号 {uid} 不存在。"
    if create and not (new_pw or "").strip():
        c.close()
        return "⚠️ 新建账号必须设置初始密码。"
    if uid == admin and (status != "active" or role != "kb_admin"):
        c.close()
        return "⚠️ 不能停用自己或去掉自己的管理员角色（防止把自己锁在外面）。"
    if create:
        c.execute("INSERT INTO users(uid, display_name, role, clearance, pw_hash, status) VALUES (?,?,?,?,?,?)",
                  (uid, display_name or uid, role, clearance, acl.hash_pw(new_pw.strip()), status or "active"))
    else:
        c.execute("UPDATE users SET display_name=?, role=?, clearance=?, status=? WHERE uid=?",
                  (display_name or uid, role, clearance, status or "active", uid))
        if (new_pw or "").strip():
            c.execute("UPDATE users SET pw_hash=? WHERE uid=?", (acl.hash_pw(new_pw.strip()), uid))
    c.execute("DELETE FROM user_scopes WHERE uid=?", (uid,))
    for s in scopes:
        c.execute("INSERT INTO user_scopes(uid, scope, granted_by, valid_until) VALUES (?,?,?,?)",
                  (uid, s, admin, until_iso))
    c.commit()
    c.close()
    _log(admin, f"{'新建' if create else '修改'}账号 {uid} 角色={role} L{clearance} "
                f"类别={len(cats or [])} 机型={_fmt_models(models)} 客户={len(custs or [])}"
                + ("（重置密码）" if (new_pw or '').strip() and not create else ""))
    return f"✅ 已{'新建' if create else '保存'}账号 **{uid}**{note}。对方重新登录后生效。"


# ── 资料 ──────────────────────────────────────────────────────────────
def _doc_rows(c) -> list:
    """按文件名聚合 doc_acl（同名多 doc_id 视为一份资料）。"""
    rows = c.execute("""
        SELECT filename, MIN(sec_level) sec_level, MAX(review_status) review_status,
               MAX(category) category, MAX(models) models, MAX(customer) customer,
               MAX(classified_by) classified_by, COUNT(*) n
        FROM doc_acl GROUP BY filename ORDER BY filename""").fetchall()
    return [dict(r) for r in rows]


def list_docs(category="", model="", customer="", review="", keyword="") -> list:
    c = _conn()
    out = []
    for r in _doc_rows(c):
        models = [m for m in (r["models"] or ",ALL,").split(",") if m]
        if category and r["category"] != category:
            continue
        if model and model not in models and acl.ALL_MODELS not in models:
            continue
        if customer and (r["customer"] or "none") != customer:
            continue
        if review and r["review_status"] != review:
            continue
        if keyword and keyword.lower() not in (r["filename"] or "").lower():
            continue
        out.append(r)
    c.close()
    return out


def docs_table(rows: list) -> list:
    cust_names = acl.customers()
    return [{
        "文件名": r["filename"],
        "类别": acl.CATEGORIES.get(r["category"], r["category"] or "（未分类）"),
        "机型": _fmt_models([m for m in (r["models"] or "").split(",") if m]),
        "客户": cust_names.get(r["customer"], "—") if (r["customer"] or "none") != "none" else "—",
        "密级": f"L{r['sec_level']}",
        "状态": REVIEW_LABELS.get(r["review_status"], r["review_status"]),
        "定级": "人工" if (r["classified_by"] or "auto") != "auto" else "自动",
    } for r in rows]


def update_docs(admin: str, filenames: list, category=None, models=None, customer=None,
                sec_level=None, review=None) -> str:
    """批量改资料权限。参数为 None 表示该项不改。"""
    filenames = [f for f in (filenames or []) if f]
    if not filenames:
        return "⚠️ 没有选中任何资料。"
    sets, params = [], []
    if category:
        sets.append("category=?"); params.append(category)
    if models is not None:
        ms = [m for m in models if m]
        ms = [acl.ALL_MODELS] if (not ms or acl.ALL_MODELS in ms) else ms
        sets.append("models=?"); params.append("," + ",".join(ms) + ",")
    if customer:
        sets.append("customer=?"); params.append(customer)
        sets.append("owner_scope=?"); params.append("internal" if customer == "none" else f"customer:{customer}")
    if sec_level:
        sets.append("sec_level=?"); params.append(int(sec_level))
    if review:
        sets.append("review_status=?"); params.append(review)
    if not sets:
        return "⚠️ 没有要改的项（都选了「不改」）。"
    sets.append("classified_by=?"); params.append(f"admin:{admin}")
    sets.append("classified_at=datetime('now')")
    c = _conn()
    for f in filenames:
        c.execute(f"UPDATE doc_acl SET {', '.join(sets)} WHERE filename=?", params + [f])
    c.commit()
    # 同步到 Qdrant（语义检索用）
    from src.acl_classify import acl_row_for, _set_qdrant_payload
    failed = []
    for f in filenames:
        try:
            row = acl_row_for(c, f)
            if row:
                _set_qdrant_payload(f, row)
        except Exception as e:
            failed.append(f"{f}（{type(e).__name__}）")
    c.close()
    what = "、".join(x for x, v in (("类别", category), ("机型", models is not None), ("客户", customer),
                                   ("密级", sec_level), ("审核状态", review)) if v)
    _log(admin, f"批量改资料 {len(filenames)} 份：{what}；首份 {filenames[0][:40]}")
    msg = f"✅ 已修改 {len(filenames)} 份资料的 {what}，立即生效。"
    if failed:
        msg += f"\n\n⚠️ 语义检索那边有 {len(failed)} 份没同步上（全文检索已生效）：" + "；".join(failed[:5])
    return msg


# ── 预览 ──────────────────────────────────────────────────────────────
def preview(uid: str) -> tuple:
    """返回 (摘要 markdown, 能看到的资料行, 看不到的资料行[附原因])。"""
    c = _conn()
    try:
        user = acl.load_user(uid, c)
    except acl.PermissionDenied as e:
        c.close()
        return f"⚠️ {e}", [], []
    docs = _doc_rows(c)
    c.close()
    cats, models, custs = user.grants
    vis, hid = [], []
    os.environ.setdefault("SECURITY_ENABLED", "true")
    for r in docs:
        ms = [m for m in (r["models"] or ",ALL,").split(",") if m]
        reason = []
        if r["review_status"] != "approved":
            reason.append(REVIEW_LABELS.get(r["review_status"], r["review_status"]))
        if r["sec_level"] > user.clearance:
            reason.append(f"密级 L{r['sec_level']} 高于 L{user.clearance}")
        if r["category"] not in cats:
            reason.append(f"无「{acl.CATEGORIES.get(r['category'], r['category'])}」类别权限")
        if acl.ALL_MODELS not in models and acl.ALL_MODELS not in ms and not (set(ms) & set(models)):
            reason.append(f"无 {'/'.join(ms)} 机型权限")
        if (r["customer"] or "none") != "none" and r["customer"] not in custs:
            reason.append("无该客户权限")
        (hid if reason else vis).append((r, "；".join(reason)))
    by_cat = {}
    for r, _ in vis:
        k = acl.CATEGORIES.get(r["category"], r["category"])
        by_cat[k] = by_cat.get(k, 0) + 1
    summary = (f"**{uid}**（{ROLE_LABELS.get(user.role, user.role)} · L{user.clearance}）能看到 **{len(vis)}** 份，"
               f"看不到 **{len(hid)}** 份。\n\n能看到的按类别：" +
               ("、".join(f"{k} {v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])) or "无"))
    vis_rows = docs_table([r for r, _ in vis])
    hid_rows = [{**t, "看不到的原因": why} for t, (r, why) in zip(docs_table([r for r, _ in hid]), hid)]
    return summary, vis_rows, hid_rows
