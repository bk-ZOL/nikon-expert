"""src/local_llm.py —— 用户 Mac 上的本地模型（2026-10-17）。

提炼故障排查 / SOP 这类批量活不发给云端（deepseek 没有保密承诺，Anthropic 不对中国公司开放），
改用 Mac 上 Ollama 跑的 gemma4：服务器经 Tailscale 调 Mac（资料只在自己的设备之间走）。
Mac 那边：~/Applications/ollama-0.40.4，LaunchAgent com.jayce.ollama-tailnet，只监听 Tailscale 地址。

只在 Mac 在线、而且用户没在用（键盘鼠标空闲够久）时才调，免得和用户抢电脑。
  LOCAL_LLM_URL        默认 http://100.89.146.17:11434（Mac 的 Tailscale 地址）
  LOCAL_LLM_MODEL      默认 gemma4:latest
  LOCAL_LLM_IDLE_MIN   Mac 空闲多少分钟以上才用（默认 10）
  LOCAL_LLM_SSH        查空闲时间用的 ssh 别名（默认 mac，见 /root/.ssh/config）
"""
import os
import subprocess
import time

import requests

URL = os.getenv("LOCAL_LLM_URL", "http://100.89.146.17:11434").rstrip("/")
MODEL = os.getenv("LOCAL_LLM_MODEL", "gemma4:latest")
IDLE_MIN = float(os.getenv("LOCAL_LLM_IDLE_MIN", "10"))
SSH = os.getenv("LOCAL_LLM_SSH", "mac")


def _session():
    s = requests.Session()
    s.trust_env = False                    # 不走代理：Tailscale 地址直连
    return s


def online() -> bool:
    try:
        return _session().get(f"{URL}/api/version", timeout=5).ok
    except Exception:
        return False


def idle_seconds():
    """Mac 键盘鼠标多久没动了（秒）；查不到返回 None。"""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", SSH,
                            "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'"],
                           capture_output=True, text=True, timeout=20)
        return int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
    except Exception:
        return None


def available(require_idle: bool = True) -> tuple:
    """(能不能用, 原因)。"""
    if not online():
        return False, "Mac 不在线（关机 / 睡眠 / Ollama 没起来）"
    if require_idle and IDLE_MIN > 0:
        idle = idle_seconds()
        if idle is None:                   # Tailscale 偶尔丢一下，隔几秒再试一次
            time.sleep(5)
            idle = idle_seconds()
        if idle is None:
            return False, "查不到 Mac 空闲时间（ssh 不通）"
        if idle < IDLE_MIN * 60:
            return False, f"Mac 正在用（{idle // 60} 分钟前有操作）"
    return True, ""


def keep_awake(seconds: int = 3600):
    """跑的时候不让 Mac 自动睡眠（只防闲置睡眠；合盖不插电照样会睡）。"""
    try:
        subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", SSH,
                        f"nohup caffeinate -i -t {int(seconds)} >/dev/null 2>&1 &"], timeout=20)
    except Exception:
        pass


def chat(prompt: str, timeout: int = 900, num_ctx: int = 8192) -> str:
    """调一次本地模型，返回文字。关掉思考（抽取用不着，还省一半时间）。"""
    t = time.time()
    r = _session().post(f"{URL}/api/chat", timeout=timeout, json={
        "model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False, "think": False,
        "options": {"temperature": 0, "num_ctx": num_ctx}})
    r.raise_for_status()
    d = r.json()
    if "message" not in d:
        raise RuntimeError(f"本地模型返回异常：{str(d)[:200]}")
    chat.last = {"sec": round(time.time() - t, 1), "in": d.get("prompt_eval_count", 0), "out": d.get("eval_count", 0)}
    return d["message"].get("content") or ""


chat.last = {}
