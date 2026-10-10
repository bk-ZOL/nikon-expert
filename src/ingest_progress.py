"""src/ingest_progress.py —— 摄入进度的跨进程 IPC。

摄入在独立子进程（scripts/ingest_one.py → engine.ingest_file）里跑，Gradio 主进程读不到
它的内部状态。这里用一个小 JSON 文件做进程间通道：子进程按批写 {stage, done, total}，
UI 主进程定时读并渲染真实进度条。token 用 ingest_queue 的 job id，一一对应。
"""
import json
import os
import tempfile
import time

_DIR = os.path.join(tempfile.gettempdir(), "nikon_ingest_progress")


def _path(token: str) -> str:
    return os.path.join(_DIR, f"{token}.json")


def write(token, stage: str, done: int = 0, total: int = 0) -> None:
    if not token:
        return
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _path(token) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"stage": stage, "done": int(done), "total": int(total),
                       "ts": time.time()}, f)
        os.replace(tmp, _path(token))   # 原子替换，避免 UI 读到半个文件
    except Exception:
        pass


def read(token) -> dict:
    if not token:
        return None
    try:
        with open(_path(token)) as f:
            return json.load(f)
    except Exception:
        return None


def clear(token) -> None:
    try:
        os.remove(_path(token))
    except Exception:
        pass
