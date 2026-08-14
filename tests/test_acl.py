"""tests/test_acl.py —— visible() 真值表 + fts 子句 + load_user 过期回收。

覆盖方案 2.4 判定式与 2.5 默认拒绝原则。运行：
    SECURITY_ENABLED=true python -m pytest tests/test_acl.py -q
或直接： SECURITY_ENABLED=true python tests/test_acl.py
"""
import os
import sqlite3

os.environ["SECURITY_ENABLED"] = "true"  # 测试恒定开启

from src import acl  # noqa: E402
from src.acl import User, visible, load_user, fts_acl_clause, PermissionDenied  # noqa: E402


def _doc(sec_level=3, owner_scope="vendor:nikon", review_status="approved", **kw):
    return {"sec_level": sec_level, "owner_scope": owner_scope,
            "review_status": review_status, **kw}


def _user(clearance=3, scopes=("internal",), role="engineer", uid="u1"):
    return User(uid=uid, role=role, clearance=clearance,
                active_scopes=frozenset(scopes))


CASES = [
    # (说明, user, doc, 期望可见)
    ("原厂L3+有nikon域 → 可见", _user(3, ("internal", "vendor:nikon")), _doc(3, "vendor:nikon"), True),
    ("原厂L3+无nikon域 → 挡", _user(3, ("internal",)), _doc(3, "vendor:nikon"), False),
    ("密级不足(L2<L3) → 挡", _user(2, ("internal", "vendor:nikon")), _doc(3, "vendor:nikon"), False),
    ("public 任何人可见", _user(2, ()), _doc(1, "public"), True),
    ("未approved → 挡", _user(4, ("internal",)), _doc(2, "internal", "pending"), False),
    ("跨客户:持欣奕华看星钥 → 挡", _user(4, ("internal", "customer:xinyihua")),
        _doc(4, "customer:xingyue", acl_users=["u1"]), False),
    ("L4+归属域对但不在ACL → 挡", _user(4, ("internal", "customer:xingyue")),
        _doc(4, "customer:xingyue", acl_users=["someone_else"]), False),
    ("L4+归属域对+在ACL → 可见", _user(4, ("internal", "customer:xingyue")),
        _doc(4, "customer:xingyue", acl_users=["u1"]), True),
    ("缺字段 → 默认拒绝", _user(4, ("internal",)), {"owner_scope": "internal"}, False),
    ("shared_scopes 相交 → 可见", _user(3, ("customer:pcl",)),
        _doc(3, "internal", shared_scopes=["customer:pcl"]), True),
]


def test_visible_truth_table():
    for desc, u, d, expect in CASES:
        assert visible(u, d) is expect, f"FAIL: {desc}"


def test_deny_when_no_identity():
    assert visible(None, _doc()) is False


def test_superuser_sees_all():
    assert visible(acl.SYSTEM_USER, _doc(4, "customer:xingyue")) is True


def test_auditor_reads_nothing():
    a = User(uid="aud", role="auditor", clearance=0, active_scopes=frozenset())
    assert visible(a, _doc(1, "public")) is False


def test_security_disabled_allows_all(monkeypatch=None):
    os.environ["SECURITY_ENABLED"] = "false"
    try:
        assert visible(None, _doc(4, "customer:xingyue")) is True
        assert fts_acl_clause(None) == ("", [])
    finally:
        os.environ["SECURITY_ENABLED"] = "true"


def test_fts_clause_denies_no_identity():
    try:
        fts_acl_clause(None)
        assert False, "应抛 PermissionDenied"
    except PermissionDenied:
        pass


def test_fts_clause_shape():
    sql, params = fts_acl_clause(_user(3, ("internal", "vendor:nikon")))
    assert "doc_acl" in sql and "review_status = 'approved'" in sql
    # params: clearance + scopes(+public) + uid
    assert params[0] == 3 and "public" in params and params[-1] == "u1"


def test_load_user_drops_expired_scope():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE users(uid TEXT PRIMARY KEY, display_name TEXT, role TEXT,
            clearance INT, pw_hash TEXT, status TEXT, created_at TEXT);
        CREATE TABLE user_scopes(uid TEXT, scope TEXT, granted_by TEXT,
            granted_at TEXT, valid_until TEXT, PRIMARY KEY(uid,scope));
        INSERT INTO users VALUES('eng','Eng','engineer',3,'x','active','2026-01-01');
        INSERT INTO user_scopes VALUES('eng','internal','a','2026-01-01','2099-01-01T00:00:00Z');
        INSERT INTO user_scopes VALUES('eng','customer:xingyue','a','2026-01-01','2026-01-02T00:00:00Z');
        """
    )
    u = load_user("eng", conn)
    assert "internal" in u.active_scopes
    assert "customer:xingyue" not in u.active_scopes  # 已过期，加载时剔除


def test_load_user_unknown_denied():
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE users(uid TEXT PRIMARY KEY, role TEXT, clearance INT, status TEXT);")
    try:
        load_user("ghost", conn)
        assert False
    except PermissionDenied:
        pass


def test_clearance_capped_by_role():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE users(uid TEXT PRIMARY KEY, display_name TEXT, role TEXT,
            clearance INT, pw_hash TEXT, status TEXT, created_at TEXT);
           CREATE TABLE user_scopes(uid TEXT, scope TEXT, granted_by TEXT,
            granted_at TEXT, valid_until TEXT, PRIMARY KEY(uid,scope));
           INSERT INTO users VALUES('t','T','trainee',4,'x','active','2026-01-01');"""
    )
    u = load_user("t", conn)
    assert u.clearance == 2  # trainee 上限 L2，即便库里写了 4


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
