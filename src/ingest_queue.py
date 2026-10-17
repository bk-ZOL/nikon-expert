"""src/ingest_queue.py —— 云端异步摄入队列。

上传不再阻塞：文件入队即返回，单个后台 worker 线程串行摄入（2 核小机不宜并行嵌），
摄入走 engine.ingest_file（内含自动定级）。UI 轮询 recent_jobs() 显示进度，
job 完成置 dirty，供 UI 刷新文档表。进程重启后 job 记录丢失（已入库的文档不受影响）。
"""
import os
import queue
import threading
import time

_q: "queue.Queue" = queue.Queue()
_jobs: dict = {}
_lock = threading.Lock()
_dirty = threading.Event()      # 有 job 刚完成 → UI 该刷新文档表
_worker_started = False
_seq = [0]


def _worker():
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = os.path.join(root, ".venv", "bin", "python")
    script = os.path.join(root, "scripts", "ingest_one.py")
    while True:
        job_id, path, embed_mode = _q.get()
        with _lock:
            j = _jobs.get(job_id)
            if j:
                j["status"] = "processing"
                j["ts_start"] = time.time()
        try:
            # 子进程跑 embedding：CPU 密集活不占 Gradio 主进程 GIL，
            # 上传/查询/定时器全程畅通（修 embedding 把上传挤断的 ClientDisconnect）。
            env = {**os.environ, "INGEST_PROGRESS_TOKEN": str(job_id)}
            if embed_mode in ("local", "api"):
                env["INGEST_EMBED"] = embed_mode
            elif embed_mode == "mac":
                # 借 Mac 算力：仍走 API 路径，但 base 指向 Mac 上的嵌入服务（Tailscale）
                env["INGEST_EMBED"] = "api"
                mac = os.getenv("MAC_EMBED_URL", "").strip()
                if mac:
                    env["EMBED_API_BASE"] = mac
                    env.setdefault("EMBED_API_KEY", "mac-local")  # Mac 服务不校验，占位即可
            proc = subprocess.run([py, script, path], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=7200)
            lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
            last = lines[-1] if lines else ""
            ok = proc.returncode == 0 and last.startswith("OK")
            msg = last[3:].strip() if (":" in last[:5]) else last
            if not ok and not msg:
                msg = (proc.stderr or "")[-200:] or "摄入失败"
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done" if ok else "failed", message=msg)
        except subprocess.TimeoutExpired:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="failed", message="超时（>2h），可能文件过大")
        except Exception as e:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="failed", message=str(e))
        finally:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["ts_done"] = time.time()
            try:
                from src import ingest_progress
                ingest_progress.clear(str(job_id))
            except Exception:
                pass
            _dirty.set()
            _q.task_done()


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker, daemon=True, name="ingest-worker").start()
        _worker_started = True


def enqueue(path: str, name: str = None, embed_mode: str = "") -> int:
    """把文件加入摄入队列，立即返回 job_id（不阻塞）。
    embed_mode: "local"=服务器本地模型(不外传) / "api"=云端API(快) / ""=按 .env 默认。"""
    _ensure_worker()
    with _lock:
        _seq[0] += 1
        jid = _seq[0]
        _jobs[jid] = {"id": jid, "name": name or os.path.basename(path),
                      "status": "queued", "message": "", "ts": time.time(),
                      "embed_mode": embed_mode}
    _q.put((jid, path, embed_mode))
    return jid


def recent_jobs(limit: int = 10) -> list:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: -j["ts"])[:limit]


def recent_done_names(limit: int = 20) -> list:
    """最近成功摄入的文档名，最新在前（供 UI 把新上传的文档置顶，避免用户以为没成功）。"""
    with _lock:
        done = [j for j in _jobs.values() if j["status"] == "done" and j.get("ts_done")]
    done.sort(key=lambda j: -j["ts_done"])
    seen, out = set(), []
    for j in done:
        if j["name"] not in seen:
            seen.add(j["name"])
            out.append(j["name"])
        if len(out) >= limit:
            break
    return out


def active_count() -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j["status"] in ("queued", "processing"))


def take_dirty() -> bool:
    """有 job 自上次以来完成过则返回 True 并清标志（供 UI 决定是否刷新文档表）。"""
    if _dirty.is_set():
        _dirty.clear()
        return True
    return False
