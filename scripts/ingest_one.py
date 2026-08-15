#!/usr/bin/env python3
"""scripts/ingest_one.py —— 在独立子进程里摄入单个文件。

由异步队列 worker 以子进程方式调用：CPU 密集的 BGE-M3 embedding 在**独立进程**里跑，
不占 Gradio 主进程的 GIL/CPU，从而保证 web 服务器(上传/查询/定时器)全程畅通。
子进程各自加载模型（多用 ~1.2G 内存，摄完即释放），适合 2 核小机的后台入库。

    python scripts/ingest_one.py /path/to/file.pdf
输出末行以 OK: / FAIL: 开头，供 worker 解析状态。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("FAIL: 未提供文件路径")
        return 1
    path = sys.argv[1]
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, ".env"))
    except Exception:
        pass
    if not os.path.exists(path):
        print(f"FAIL: 文件不存在 {path}")
        return 1
    try:
        from src.engine import ingest_file
        r = ingest_file(path)
    except Exception as e:
        print(f"FAIL: {e}")
        return 2
    msg = r.get("message", str(r)) if isinstance(r, dict) else str(r)
    ok = not (isinstance(r, dict) and r.get("success") is False)
    print(("OK: " if ok else "FAIL: ") + str(msg))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
