"""HTTP client for WPS kwiki Skills-Hub — a drop-in replacement for
sync_kwiki.run_cli that talks to the REST API directly (no kwiki-cli binary).

Only the two subcommands sync_kwiki.py actually uses are implemented:
  file-list      -> GET  /skill/file/list?kuid=&page_size=&page_token=
  file-download  -> POST /skill/file/download {kuid, response_type}

Auth: header X-Kwiki-Auth (token from X_KWIKI_AUTH env). Base: KWIKI_BASE_URL.
Returns the `data` object so callers see .list / .next_page_token / .file_base64,
exactly like the CLI's --format json output.
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

BASE = os.getenv("KWIKI_BASE_URL", "https://zhishi.wps.cn").rstrip("/")
PREFIX = "/kwiki/api/v1/skills_hub/skill"
_THROTTLE = float(os.getenv("KWIKI_THROTTLE", "0.6"))
_last = [0.0]


def _token():
    t = os.getenv("X_KWIKI_AUTH", "").strip()
    if not t:
        raise RuntimeError("no WPS token: set X_KWIKI_AUTH")
    return t


def _throttle():
    wait = _THROTTLE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def _request(method, path, params=None, body=None, timeout=60):
    url = f"{BASE}{PREFIX}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Kwiki-Auth": _token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = ""
    for attempt in range(5):
        _throttle()
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            code = d.get("code")
            if code not in (0, None):
                raise RuntimeError(f"api error code={code} msg={d.get('msg')}")
            return d.get("data") or {}
        except urllib.error.HTTPError as he:
            # 4xx = business/client error (e.g. OTL 400408000): read JSON body,
            # surface the code, DO NOT retry. 5xx = server: retry.
            body = ""
            try:
                body = he.read().decode()
                j = json.loads(body)
                bmsg = f"code={j.get('code')} msg={j.get('msg')}"
            except Exception:
                bmsg = body[:150]
            if 400 <= he.code < 500:
                raise RuntimeError(f"api {he.code}: {bmsg}")
            last = f"HTTP {he.code}: {bmsg}"
            if attempt < 4:
                time.sleep(3 * (attempt + 1))
        except RuntimeError:
            raise
        except Exception as e:  # network / EOF / timeout — retry
            last = str(e)[:200]
            if attempt < 4:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"WPS API failed after retries: {method} {path}: {last}")


def _parse_flags(args):
    sub = args[0]
    flags, i = {}, 1
    while i < len(args):
        a = args[i]
        if isinstance(a, str) and a.startswith("--"):
            key = a[2:]
            val = args[i + 1] if i + 1 < len(args) and not str(args[i + 1]).startswith("--") else ""
            flags[key] = val
            i += 2
        else:
            i += 1
    return sub, flags


def run_cli(*args):
    """Drop-in for sync_kwiki.run_cli. Accepts kwiki-cli-style args, returns the
    `data` dict. Supports the subcommands sync_kwiki.py uses."""
    # tolerate a trailing --format json that sync_kwiki may append
    args = tuple(a for a in args if a not in ("--format", "json", "pretty"))
    sub, flags = _parse_flags(args)
    if sub == "file-list":
        return _request("GET", "/file/list", params={
            "kuid": flags.get("kuid", ""),
            "page_size": flags.get("page-size", "100"),
            "page_token": flags.get("page-token", ""),
        })
    if sub == "file-download":
        return _request("POST", "/file/download", body={
            "kuid": flags.get("kuid", ""),
            "response_type": flags.get("response-type", "file_base64"),
        })
    if sub == "knowledge-view-list":
        return _request("GET", "/knowledge_view/list", params={
            k.replace("-", "_"): v for k, v in flags.items()})
    raise RuntimeError(f"wps_api.run_cli: unsupported subcommand {sub!r}")


def ask(question, kuids=None, timeout=120):
    """Ask WPS kwiki's own RAG (knowledge_view/ask, SSE). Returns
    {answer, sources, caution}. answer = concatenated answer_gen chunks."""
    body = {"input": question}
    if kuids:
        body["kuids"] = list(kuids)
    else:
        body["scope"] = "all_wiki"
    req = urllib.request.Request(
        f"{BASE}{PREFIX}/knowledge_view/ask",
        data=json.dumps(body).encode(),
        headers={"X-Kwiki-Auth": _token(), "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST")
    parts, sources, seen, caution = [], [], set(), None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                dd = (json.loads(line[5:].strip()).get("data") or {})
            except Exception:
                continue
            dyn = dd.get("dynamic") or {}
            for ac in (dyn.get("answer_citations") or []):
                if ac.get("type") == "answer_gen" and ac.get("text"):
                    parts.append(ac["text"])
            for rc in (dd.get("recall_content") or []):
                nm = (rc.get("file_meta") or {}).get("fname")
                if nm and nm not in seen:
                    seen.add(nm); sources.append(nm)
            if dyn.get("caution"):
                caution = dyn["caution"]
    answer = "".join(parts).strip()
    return {"answer": answer, "sources": sources,
            "caution": (caution if not answer else None)}
