#!/usr/bin/env python3
"""飞书（国内版 open.feishu.cn）user_access_token 管理器。

首次跑 `login` 走浏览器 OAuth 拿到 access_token + refresh_token 并缓存；
之后 `get_user_token()` 自动用 refresh_token 静默续期（新 refresh_token 覆盖回存），
只要 refresh_token 未过期（默认 ~30 天，用一次就顺延）就无需再手动授权。

OAuth 2.0 端点（已核对官方文档，2026-08）：
  授权页  GET  https://accounts.feishu.cn/open-apis/authen/v1/authorize
  换/刷   POST https://open.feishu.cn/open-apis/authen/v2/oauth/token
`offline_access` scope 才会返回 refresh_token。

CLI:
  python scripts/feishu_auth.py login     # 首次浏览器授权
  python scripts/feishu_auth.py status     # 看当前 token 剩余有效期
  python scripts/feishu_auth.py token      # 打印一个有效 access_token（自动刷新）
  python scripts/feishu_auth.py refresh     # 手动强制刷新一次
"""
import os
import sys
import json
import time
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".feishu_env")
load_dotenv(ROOT / ".env", override=False)

APP_ID       = os.getenv("FEISHU_APP_ID", "")
APP_SECRET   = os.getenv("FEISHU_APP_SECRET", "")
DOMAIN       = os.getenv("FEISHU_DOMAIN", "https://open.feishu.cn").rstrip("/")
REDIRECT_URI = os.getenv("FEISHU_REDIRECT_URI", "http://localhost:3000/callback")
TOKEN_FILE   = ROOT / os.getenv("FEISHU_TOKEN_FILE", ".feishu_token.json")

# 读云文档 + 取「最后编辑时间」做增量 + 拿 refresh_token
SCOPES = os.getenv(
    "FEISHU_SCOPES",
    "docx:document:readonly drive:drive:readonly offline_access",
)

# 国内版：授权页在 accounts.feishu.cn，token 端点在 open.feishu.cn
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL     = f"{DOMAIN}/open-apis/authen/v2/oauth/token"

# access_token 剩余不足这么多秒就提前刷新
_REFRESH_BUFFER = 300


def _require_creds():
    if not APP_ID or not APP_SECRET:
        sys.exit("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET（检查 .feishu_env）")


# ── token 缓存读写 ────────────────────────────────────────────────
def _load() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(tok: dict):
    now = int(time.time())
    tok["access_expires_at"] = now + int(tok.get("expires_in", 0))
    if tok.get("refresh_token_expires_in"):
        tok["refresh_expires_at"] = now + int(tok["refresh_token_expires_in"])
    tok["updated_at"] = now
    TOKEN_FILE.write_text(json.dumps(tok, ensure_ascii=False, indent=1))
    os.chmod(TOKEN_FILE, 0o600)


# ── OAuth：code → token / refresh → token ─────────────────────────
def _exchange(payload: dict) -> dict:
    payload = {"client_id": APP_ID, "client_secret": APP_SECRET, **payload}
    r = requests.post(
        TOKEN_URL,
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    data = r.json()
    # v2 成功 code=0；失败给出 error / error_description
    if data.get("code") not in (0, None) or data.get("error"):
        raise RuntimeError(
            f"token 端点报错 {r.status_code}: "
            f"{data.get('error') or data.get('code')} "
            f"{data.get('error_description') or data.get('msg') or ''}"
        )
    if "access_token" not in data:
        raise RuntimeError(f"响应缺 access_token：{data}")
    return data


def refresh() -> str:
    _require_creds()
    tok = _load()
    rt = tok.get("refresh_token")
    if not rt:
        raise RuntimeError(
            "无 refresh_token（未开通 offline_access）。请在本机重新跑 "
            "`python scripts/feishu_auth.py login`")
    if tok.get("refresh_expires_at") and tok["refresh_expires_at"] < time.time():
        raise RuntimeError("refresh_token 已过期，请重新 login")
    data = _exchange({"grant_type": "refresh_token", "refresh_token": rt})
    _save(data)  # 新的 refresh_token 一并覆盖保存
    return data["access_token"]


def get_user_token() -> str:
    """返回一个有效的 user_access_token；过期则自动刷新。供同步脚本/MCP 工具调用。"""
    tok = _load()
    if not tok.get("access_token"):
        raise RuntimeError("尚未授权：请先在本机跑 `python scripts/feishu_auth.py login`")
    if tok.get("access_expires_at", 0) - _REFRESH_BUFFER > time.time():
        return tok["access_token"]
    # 已过期：有 refresh_token 就自动续期，否则要求重新授权
    if tok.get("refresh_token"):
        return refresh()
    raise RuntimeError(
        "user_access_token 已过期，且无 refresh_token（当前未开通 offline_access）。"
        "请在本机重新跑 `python scripts/feishu_auth.py login`")


# ── 首次浏览器授权 ────────────────────────────────────────────────
class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(REDIRECT_URI).path:
            self.send_response(404); self.end_headers(); return
        q = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _CallbackHandler.result
        msg = "✅ 授权成功，可以关掉这个页面回到终端。" if ok else \
              f"❌ 授权失败：{_CallbackHandler.result}"
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{msg}</body></html>"
                         .encode("utf-8"))

    def log_message(self, *a):
        pass  # 静音


def login():
    _require_creds()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    host, port = parsed.hostname, parsed.port or 80
    server = HTTPServer((host, port), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print("在浏览器完成飞书授权（若没自动弹出，手动打开下面的链接）：\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"等待回调 {REDIRECT_URI} …（授权后自动继续；Ctrl-C 取消）")
    while "code" not in _CallbackHandler.result and "error" not in _CallbackHandler.result:
        time.sleep(0.3)
    server.shutdown()

    res = _CallbackHandler.result
    if res.get("state") != state:
        sys.exit(f"state 不匹配（疑似 CSRF），中止：{res}")
    if "code" not in res:
        sys.exit(f"未拿到 code：{res}")

    data = _exchange({
        "grant_type": "authorization_code",
        "code": res["code"],
        "redirect_uri": REDIRECT_URI,
    })
    _save(data)
    print(f"\n✅ 已获取并缓存 user_access_token → {TOKEN_FILE}")
    _print_status()


def _print_status():
    tok = _load()
    if not tok.get("access_token"):
        print("（尚未授权）"); return
    now = time.time()
    a = int(tok.get("access_expires_at", 0) - now)
    r = int(tok.get("refresh_expires_at", 0) - now) if tok.get("refresh_expires_at") else None
    print(f"  access_token  剩余 ~{a//60} 分钟")
    if r is not None:
        print(f"  refresh_token 剩余 ~{r//86400} 天")
    print(f"  scope: {tok.get('scope', SCOPES)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "login":
        login()
    elif cmd == "refresh":
        print(refresh()[:12] + "…（已刷新并回存）")
    elif cmd == "token":
        print(get_user_token())
    elif cmd == "status":
        _print_status()
    else:
        sys.exit(f"未知命令：{cmd}（可用 login/status/token/refresh）")
