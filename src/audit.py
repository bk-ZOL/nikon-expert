"""src/audit.py —— 登录/使用审计（管理员视角）。

记录谁在什么时候、从哪个 IP、做了什么；提供 IP 归属地（带缓存）和用户权限总览。
表建在 data/fts.db（与 ACL 同库，复用 fulltext._get_conn）。全程 best-effort：
审计失败绝不影响正常功能（所有写操作 try/except 吞掉）。
"""
import ipaddress
import os
import threading
import time
from typing import List

# 同一 (uid, ip, action) 60s 内只记一条，避免刷屏
_THROTTLE: dict = {}
_THROTTLE_SEC = 60

# 审计用独立 sqlite 连接（连同一个 fts.db 文件，但不与 FTS 检索共用连接，
# 避免干扰对方事务）。SQLite 多连接靠文件锁 + busy_timeout 协调。
_conn_obj = None
_conn_lock = threading.Lock()


def _db_path() -> str:
    try:
        from src.fulltext import _db_path as p
        return p
    except Exception:
        return os.getenv("FTS_DB_PATH", "./data/fts.db")


_tls = threading.local()


def _conn():
    """每个线程一个连接（多人同时提问时，共用一个连接写日志会互相干扰）。"""
    c = getattr(_tls, "conn", None)
    if c is None:
        try:
            import pysqlite3 as sqlite3  # 与 fulltext 一致，优先 pysqlite3
        except Exception:
            import sqlite3
        c = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10)
        c.execute("PRAGMA busy_timeout=5000")
        _tls.conn = c
    return c


def ensure_tables() -> None:
    try:
        c = _conn()
        c.execute("""CREATE TABLE IF NOT EXISTS access_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, uid TEXT, action TEXT, ip TEXT, ua TEXT, ok INTEGER DEFAULT 1)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_access_ts ON access_log(ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS ip_geo(
            ip TEXT PRIMARY KEY, geo TEXT, isp TEXT, ts REAL)""")
        c.commit()
    except Exception:
        pass


def log(uid: str, action: str, ip: str = "", ua: str = "", ok: bool = True) -> None:
    """记一条访问事件（best-effort，带节流）。"""
    try:
        key = (uid or "?", ip or "", action or "")
        now = time.time()
        last = _THROTTLE.get(key, 0)
        if ok and now - last < _THROTTLE_SEC:   # 失败事件不节流（安全需要）
            return
        _THROTTLE[key] = now
        ensure_tables()
        c = _conn()
        c.execute("INSERT INTO access_log(ts, uid, action, ip, ua, ok) VALUES(?,?,?,?,?,?)",
                  (now, uid or "?", action or "", ip or "", (ua or "")[:300], 1 if ok else 0))
        c.commit()
    except Exception:
        pass


def _is_private(ip: str) -> bool:
    """内网 / Tailscale(100.64.0.0/10 CGNAT) / 环回 视为内网。"""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
        # Tailscale CGNAT 段
        if addr.version == 4 and ipaddress.ip_address("100.64.0.0") <= addr <= ipaddress.ip_address("100.127.255.255"):
            return True
    except Exception:
        return False
    return False


def geo(ip: str) -> str:
    """IP 归属地（中文），带 DB 缓存。内网/Tailscale 直接标注，公网查 ip-api.com。"""
    if not ip:
        return "—"
    if _is_private(ip):
        return "内网/Tailscale"
    # 缓存
    try:
        c = _conn()
        row = c.execute("SELECT geo FROM ip_geo WHERE ip=?", (ip,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    label = "?"
    try:
        import httpx
        cli = httpx.Client(timeout=6, trust_env=False)  # 不吃系统代理
        r = cli.get(f"http://ip-api.com/json/{ip}",
                    params={"lang": "zh-CN", "fields": "status,country,regionName,city,isp,query"})
        d = r.json()
        if d.get("status") == "success":
            parts = [d.get("country", ""), d.get("regionName", ""), d.get("city", "")]
            label = " ".join(p for p in parts if p) or "?"
            isp = d.get("isp", "")
            try:
                c = _conn()
                c.execute("INSERT OR REPLACE INTO ip_geo(ip, geo, isp, ts) VALUES(?,?,?,?)",
                          (ip, label, isp, time.time()))
                c.commit()
            except Exception:
                pass
    except Exception:
        label = "?（查询失败）"
    return label


def recent_events(limit: int = 200) -> List[dict]:
    try:
        ensure_tables()
        c = _conn()
        rows = c.execute(
            "SELECT ts, uid, action, ip, ua, ok FROM access_log ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"ts": r[0], "uid": r[1], "action": r[2], "ip": r[3], "ua": r[4], "ok": r[5]}
                for r in rows]
    except Exception:
        return []


def user_permissions() -> List[dict]:
    """所有用户的角色/密级/归属域/状态（从 users + user_scopes）。"""
    out = []
    try:
        c = _conn()
        urows = c.execute("SELECT uid, role, clearance, status FROM users ORDER BY uid").fetchall()
        now = time.time()
        for uid, role, clearance, status in urows:
            try:
                srows = c.execute(
                    "SELECT scope FROM user_scopes WHERE uid=? AND valid_until > ?",
                    (uid, now)).fetchall()
                scopes = ", ".join(s[0] for s in srows) or "—"
            except Exception:
                scopes = "?"
            out.append({"uid": uid, "role": role, "clearance": clearance,
                        "scopes": scopes, "status": status})
    except Exception:
        pass
    return out
