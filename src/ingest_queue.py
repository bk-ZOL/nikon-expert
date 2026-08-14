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
    while True:
        job_id, path = _q.get()
        with _lock:
            j = _jobs.get(job_id)
            if j:
                j["status"] = "processing"
        try:
            from src.engine import ingest_file
            r = ingest_file(path)
            msg = r.get("message", str(r)) if isinstance(r, dict) else str(r)
            ok = not (isinstance(r, dict) and r.get("success") is False)
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done" if ok else "failed", message=msg)
        except Exception as e:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="failed", message=str(e))
        finally:
            _dirty.set()
            _q.task_done()


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker, daemon=True, name="ingest-worker").start()
        _worker_started = True


def enqueue(path: str, name: str = None) -> int:
    """把文件加入摄入队列，立即返回 job_id（不阻塞）。"""
    _ensure_worker()
    with _lock:
        _seq[0] += 1
        jid = _seq[0]
        _jobs[jid] = {"id": jid, "name": name or os.path.basename(path),
                      "status": "queued", "message": "", "ts": time.time()}
    _q.put((jid, path))
    return jid


def recent_jobs(limit: int = 10) -> list:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: -j["ts"])[:limit]


def active_count() -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j["status"] in ("queued", "processing"))


def take_dirty() -> bool:
    """有 job 自上次以来完成过则返回 True 并清标志（供 UI 决定是否刷新文档表）。"""
    if _dirty.is_set():
        _dirty.clear()
        return True
    return False
