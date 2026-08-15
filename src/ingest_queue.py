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
        job_id, path = _q.get()
        with _lock:
            j = _jobs.get(job_id)
            if j:
                j["status"] = "processing"
                j["ts_start"] = time.time()
        try:
            # 子进程跑 embedding：CPU 密集活不占 Gradio 主进程 GIL，
            # 上传/查询/定时器全程畅通（修 embedding 把上传挤断的 ClientDisconnect）。
            proc = subprocess.run([py, script, path], cwd=root,
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
