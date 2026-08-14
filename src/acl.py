"""src/acl.py —— 单一鉴权咽喉点 (Access Control choke point)。

落地《Nikon_Expert_权限与分层保密管理设计方案 V1.0》第 2 / 4 章：
  · 三维正交模型：密级 sec_level(有序) × 归属域 owner_scope(集合) × 角色 role。
  · 所有检索路径共用 visible() / build_qdrant_filter() / fts_acl_clause()，禁止旁路。
  · 默认拒绝 (Deny by Default)：缺字段 / 缺身份 / 异常 一律不可见。

灰度开关：环境变量 SECURITY_ENABLED（默认 false）。
  false → 行为与加权限前完全一致（便于先部署代码、再灌 doc_acl/users，最后翻开关上线）。
  true  → 强制执行本模块全部判定。方案 settings.yaml 的 security.enabled 即对应此开关。
"""
from __future__ import annotations

import os
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── 灰度开关 ──────────────────────────────────────────────────────────
def security_enabled() -> bool:
    """每次调用实时读取，方便运行中翻开关（测试/灰度）。"""
    return os.getenv("SECURITY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


# ── 密级 / 角色定义（对应方案 2.1 / 2.3 与 settings.yaml）──────────────
LEVEL_NAMES = {1: "public", 2: "internal", 3: "confidential", 4: "restricted"}

ROLE_MAX_CLEARANCE = {
    "trainee": 2,
    "engineer": 3,
    "senior_engineer": 3,
    "manager": 4,
    "kb_admin": 4,
    "auditor": 0,  # 审计员不可读内容
}

PUBLIC_SCOPE = "public"


class PermissionDenied(Exception):
    """鉴权失败。异常即拒绝——调用方禁止 try/except 后放行。"""


@dataclass(frozen=True)
class User:
    """请求主体。active_scopes 只含『未过期』的归属域授权。"""
    uid: str
    role: str
    clearance: int
    active_scopes: frozenset = field(default_factory=frozenset)
    status: str = "active"
    is_superuser: bool = False  # 仅维护脚本用，绝不用于请求路径

    @property
    def is_auditor(self) -> bool:
        return self.role == "auditor"


# 维护脚本（迁移/定级/回填）专用超级用户。绝不可作为匿名兜底注入请求路径。
SYSTEM_USER = User(
    uid="__system__", role="kb_admin", clearance=4,
    active_scopes=frozenset(), status="active", is_superuser=True,
)


# ── 当前请求用户（contextvar）──────────────────────────────────────────
# 在查询入口 set 一次，检索深处（含 agent 工具、固定签名函数）不必层层传参即可读到。
# 这是"权限逻辑不依赖调用方自觉传参"(方案 2.5)的落地：单一咽喉点绑定身份。
_current_user: ContextVar = ContextVar("nikon_current_user", default=None)


def set_current_user(user: Optional[User]):
    """在请求入口绑定当前用户，返回 token 供 reset。"""
    return _current_user.set(user)


def get_current_user() -> Optional[User]:
    return _current_user.get()


def reset_current_user(token) -> None:
    try:
        _current_user.reset(token)
    except Exception:
        pass


def _resolve(user: Optional[User]) -> Optional[User]:
    """显式实参优先，否则回退 contextvar。"""
    return user if user is not None else _current_user.get()


# ── 单一可见性判定式（方案 2.4）──────────────────────────────────────
def visible(user: Optional[User], doc: dict) -> bool:
    """全系统唯一可见性判定。doc 为含 ACL 字段的元数据 dict。

    doc 需含：sec_level(int)、owner_scope(str)、review_status(str)，
    可选：shared_scopes(list[str])、acl_users(list[str])。
    """
    if not security_enabled():
        return True
    if user is None:
        return False  # 缺身份即拒绝，不回退匿名
    if user.is_superuser:
        return True
    if user.is_auditor:
        return False  # 审计员只读日志，不读文档内容

    # 默认拒绝：缺字段视同 L4 且不可见
    try:
        sec_level = int(doc["sec_level"])
        owner_scope = doc["owner_scope"]
        review_status = doc["review_status"]
    except (KeyError, TypeError, ValueError):
        return False

    if review_status != "approved":
        return False  # 未定级 / 未复核 一律不可见
    if user.clearance < sec_level:
        return False  # 密级维度

    # 归属域维度：owner_scope 命中，或 public，或 shared_scopes 与用户域相交
    scopes = user.active_scopes
    shared = set(doc.get("shared_scopes") or [])
    if not (owner_scope == PUBLIC_SCOPE
            or owner_scope in scopes
            or (shared & scopes)):
        return False

    # L4 走显式点名 ACL
    if sec_level >= 4:
        if user.uid not in set(doc.get("acl_users") or []):
            return False

    return True


def _effective_scopes(user: User) -> list:
    """用户归属域 + public，供检索预过滤下推。"""
    return list(user.active_scopes) + [PUBLIC_SCOPE]


# ── Qdrant 侧：ACL Filter 注入（方案 4.3）────────────────────────────
def build_qdrant_filter(user: Optional[User]):
    """返回 qdrant Filter，作为 query_filter 预过滤下推（非事后删）。

    security 关闭 → None（不过滤，行为不变）。
    security 开启但无有效身份 → PermissionDenied（默认拒绝）。
    """
    if not security_enabled():
        return None
    user = _resolve(user)
    if user is None:
        raise PermissionDenied("no valid identity")  # 缺身份即拒绝，不回退匿名
    if user.is_superuser:
        return None
    # 无归属域的用户由下方 filter 自然限制为仅 public，无需额外硬拒

    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, Range

    scopes = _effective_scopes(user)
    must = [
        FieldCondition(key="review_status", match=MatchValue(value="approved")),
        FieldCondition(key="sec_level", range=Range(lte=user.clearance)),
        FieldCondition(key="owner_scope", match=MatchAny(any=scopes)),
    ]
    # L4 必须显式命名：sec_level<=3  OR  uid in acl_users
    l4_gate = Filter(should=[
        FieldCondition(key="sec_level", range=Range(lte=3)),
        FieldCondition(key="acl_users", match=MatchAny(any=[user.uid])),
    ])
    return Filter(must=must + [l4_gate])


# ── FTS5 侧：ACL 子句注入（方案 4.4）─────────────────────────────────
def fts_acl_clause(user: Optional[User]) -> tuple:
    """返回 (sql_fragment, params)，AND 进 FTS 的 WHERE，过滤先于 LIMIT。

    以 doc_id IN (子查询 doc_acl) 形式，最小侵入现有 SQL 拼接。
    security 关闭 → ("", [])。开启但无身份 → PermissionDenied。
    """
    if not security_enabled():
        return "", []
    user = _resolve(user)
    if user is None:
        raise PermissionDenied("no valid identity")
    if user.is_superuser:
        return "", []
    if user.is_auditor:
        return " AND 1 = 0", []  # 审计员读不到任何文档内容

    scopes = _effective_scopes(user)
    placeholders = ",".join("?" for _ in scopes)
    clause = f"""
        AND doc_fts.doc_id IN (
            SELECT a.doc_id FROM doc_acl a
            WHERE a.review_status = 'approved'
              AND a.sec_level <= ?
              AND a.owner_scope IN ({placeholders})
              AND ( a.sec_level < 4 OR EXISTS (
                    SELECT 1 FROM doc_acl_users u
                    WHERE u.doc_id = a.doc_id AND u.uid = ? ) )
        )"""
    params = [user.clearance] + scopes + [user.uid]
    return clause, params


# ── 身份加载（方案 3.2：users + user_scopes）─────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_pw(password: str) -> str:
    """P0 简化：SHA-256(带固定盐)。后续可换 bcrypt/scrypt。"""
    import hashlib
    salt = os.getenv("ACL_PW_SALT", "nikon-expert-acl")
    return hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()


def verify_login(uid: str, password: str, conn: sqlite3.Connection) -> bool:
    """校验 Gradio 登录口令。用户不存在/停用/口令错 → False。"""
    row = conn.execute(
        "SELECT pw_hash, status FROM users WHERE uid = ?", (uid,)
    ).fetchone()
    if not row or row[1] != "active" or not row[0]:
        return False
    return row[0] == hash_pw(password)


def load_user(uid: str, conn: sqlite3.Connection) -> User:
    """从 users + user_scopes 组装 User；过期授权在加载时即剔除。

    找不到 / 已停用 / 已离职 → PermissionDenied（默认拒绝）。
    """
    if not uid:
        raise PermissionDenied("empty uid")
    row = conn.execute(
        "SELECT uid, role, clearance, status FROM users WHERE uid = ?", (uid,)
    ).fetchone()
    if row is None:
        raise PermissionDenied(f"unknown user: {uid}")
    _uid, role, clearance, status = row
    if status != "active":
        raise PermissionDenied(f"user not active: {uid} ({status})")

    now = _now_iso()
    scope_rows = conn.execute(
        "SELECT scope FROM user_scopes WHERE uid = ? AND valid_until > ?",
        (uid, now),
    ).fetchall()
    scopes = frozenset(r[0] for r in scope_rows)

    # clearance 不得超过角色上限（兜底纠偏）
    cap = ROLE_MAX_CLEARANCE.get(role, 0)
    clearance = min(int(clearance), cap)

    return User(uid=uid, role=role, clearance=clearance,
                active_scopes=scopes, status=status)
