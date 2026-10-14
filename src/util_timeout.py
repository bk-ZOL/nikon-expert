"""src/util_timeout.py —— 统一的软超时。

在后台线程跑一个可能卡住的调用，超过 timeout 秒就让调用方立即解脱、返回兜底值。
注意：Python 无法强杀线程，底层调用仍在后台跑——但配合其 HTTP 客户端自身的超时，
卡住的线程也会在其超时后自行退出。此工具的目的是**不让上层被无限阻塞**。
"""
import contextvars
import threading


def call_with_timeout(fn, timeout, on_timeout=None, label="操作"):
    """跑 fn()，最多等 timeout 秒。
    超时→返回 on_timeout()（可调用）或 on_timeout 本身（字符串/值）。
    fn 内部异常照常向上抛。"""
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e

    # 带上调用方的 contextvars（ACL 登录身份），否则 fn 里的检索报 no valid identity
    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: ctx.run(run), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        if callable(on_timeout):
            return on_timeout()
        return on_timeout
    if "e" in box:
        raise box["e"]
    return box.get("v")
