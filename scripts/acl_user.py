#!/usr/bin/env python3
"""scripts/acl_user.py —— 用户/授权管理 CLI（方案 3.2 / 5）。

    # 建用户（角色决定密级上限；口令用于 Gradio 登录）
    python scripts/acl_user.py add   --uid zhang --name 张工 --role engineer --pw 'secret'
    python scripts/acl_user.py setpw --uid zhang --pw 'newsecret'
    # 授归属域（valid_until 强制；派工结束应到期回收）
    python scripts/acl_user.py grant --uid zhang --scope internal        --days 3650
    python scripts/acl_user.py grant --uid zhang --scope customer:xingyue --days 90
    python scripts/acl_user.py revoke --uid zhang --scope customer:xingyue
    python scripts/acl_user.py list
    python scripts/acl_user.py show  --uid zhang

角色→密级上限：trainee L2 / engineer,senior_engineer L3 / manager,kb_admin L4 / auditor 0。
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import acl  # noqa: E402


def _conn():
    db = os.getenv("FTS_DB_PATH", "./data/fts.db")
    return sqlite3.connect(db)


def _iso_in(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_add(a):
    cap = acl.ROLE_MAX_CLEARANCE.get(a.role)
    if cap is None:
        print(f"✗ 未知角色: {a.role}（可选: {', '.join(acl.ROLE_MAX_CLEARANCE)}）"); return 1
    clr = a.clearance if a.clearance is not None else cap
    clr = min(clr, cap)
    c = _conn()
    c.execute(
        """INSERT INTO users(uid,display_name,role,clearance,pw_hash,status,created_at)
           VALUES(?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(uid) DO UPDATE SET display_name=excluded.display_name,
             role=excluded.role, clearance=excluded.clearance""",
        (a.uid, a.name or a.uid, a.role, clr,
         acl.hash_pw(a.pw) if a.pw else None, "active"),
    )
    c.commit()
    print(f"✓ 用户 {a.uid}（{a.role}, 密级上限 L{clr}）{'含口令' if a.pw else '未设口令'}")
    return 0


def cmd_setpw(a):
    c = _conn()
    n = c.execute("UPDATE users SET pw_hash=? WHERE uid=?", (acl.hash_pw(a.pw), a.uid)).rowcount
    c.commit()
    print("✓ 口令已更新" if n else "✗ 用户不存在")
    return 0 if n else 1


def cmd_grant(a):
    c = _conn()
    if not c.execute("SELECT 1 FROM users WHERE uid=?", (a.uid,)).fetchone():
        print("✗ 用户不存在"); return 1
    until = _iso_in(a.days)
    c.execute(
        """INSERT INTO user_scopes(uid,scope,granted_by,granted_at,valid_until)
           VALUES(?,?,?,datetime('now'),?)
           ON CONFLICT(uid,scope) DO UPDATE SET valid_until=excluded.valid_until""",
        (a.uid, a.scope, a.by, until),
    )
    c.commit()
    print(f"✓ 授予 {a.uid} → {a.scope}（有效至 {until}）")
    return 0


def cmd_revoke(a):
    c = _conn()
    n = c.execute("DELETE FROM user_scopes WHERE uid=? AND scope=?", (a.uid, a.scope)).rowcount
    c.commit()
    print("✓ 已回收" if n else "✗ 无此授权")
    return 0


def cmd_list(a):
    c = _conn()
    rows = c.execute("SELECT uid,display_name,role,clearance,status FROM users ORDER BY uid").fetchall()
    if not rows:
        print("（无用户）"); return 0
    for uid, name, role, clr, st in rows:
        scopes = [r[0] for r in c.execute("SELECT scope FROM user_scopes WHERE uid=? AND valid_until>?",
                                          (uid, acl._now_iso())).fetchall()]
        print(f"  {uid:<12} {name:<8} {role:<16} L{clr} {st:<8} scopes={scopes}")
    return 0


def cmd_show(a):
    c = _conn()
    try:
        os.environ.setdefault("SECURITY_ENABLED", "true")
        u = acl.load_user(a.uid, c)
        print(f"uid={u.uid} role={u.role} clearance=L{u.clearance} active_scopes={sorted(u.active_scopes)}")
    except acl.PermissionDenied as e:
        print(f"✗ {e}")
        return 1
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--uid", required=True); a.add_argument("--name")
    a.add_argument("--role", required=True); a.add_argument("--pw"); a.add_argument("--clearance", type=int)
    s = sub.add_parser("setpw"); s.add_argument("--uid", required=True); s.add_argument("--pw", required=True)
    g = sub.add_parser("grant"); g.add_argument("--uid", required=True); g.add_argument("--scope", required=True)
    g.add_argument("--days", type=int, default=90); g.add_argument("--by", default="admin")
    r = sub.add_parser("revoke"); r.add_argument("--uid", required=True); r.add_argument("--scope", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show"); sh.add_argument("--uid", required=True)
    args = p.parse_args()
    return {"add": cmd_add, "setpw": cmd_setpw, "grant": cmd_grant, "revoke": cmd_revoke,
            "list": cmd_list, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
