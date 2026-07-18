# src/vision.py
# Nikon Expert — 识图（方案 A：视觉大模型直读电路图/接线图）
# 复用 providers 的 base_url/key/代理配置，把图片 + 问题发给视觉大模型，返回文字解读。
#
# 支持三类：
#   - ollama：本地多模态模型（如 gemma3:4b/12b、qwen2.5vl），离线零外传，默认。
#   - anthropic：Claude 视觉（走本地代理）。
#   - openai 兼容：Gemini / GPT-4o / 千问-VL 等（image_url data URI）。

import os
import base64
from pathlib import Path

from src.providers import get_provider, provider_key, _httpx_clients, REQUEST_TIMEOUT

# 默认视觉大脑：本地 gemma3（多模态、离线、无需 key）
DEFAULT_VISION_PROVIDER = os.getenv("VISION_PROVIDER", "ollama_local")
DEFAULT_VISION_MODEL    = os.getenv("VISION_MODEL", "gemma3:4b")

# 各 provider 的推荐视觉模型（UI 未指定时用）
VISION_MODEL_DEFAULTS = {
    "ollama_local": "gemma3:4b",
    "ollama_lan":   "gemma3:4b",
    "claude":       "claude-opus-4-8",
    "openai":       "gpt-4o",
    "gemini":       "gemini-2.5-flash",
    "qwen":         "qwen-vl-max",
}

DEFAULT_PROMPT = (
    "这是光刻机的电路图/接线图。请：1) 识别图中的元件与标注；"
    "2) 说明它们的连接关系；3) 若能看出，指出可能的故障点或检查点。"
)


def _media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(ext, "image/png")


def read_image(image_path: str, question: str = "", provider_id: str = None,
               model: str = None) -> str:
    """单图便捷入口。"""
    return read_images([image_path], question, provider_id, model)


def read_images(image_paths, question: str = "", provider_id: str = None,
                model: str = None) -> str:
    """把一张或多张图片 + 问题发给视觉大模型，返回文字解读。
    多图用于跨页/关联的图纸一起识读。缺 key 等错误抛异常由上层兜底。"""
    paths = [p for p in (image_paths or []) if p and Path(p).exists()]
    if not paths:
        return "⚠️ 未找到图片。"
    question = (question or "").strip() or DEFAULT_PROMPT
    provider_id = provider_id or DEFAULT_VISION_PROVIDER
    model = model or VISION_MODEL_DEFAULTS.get(provider_id, DEFAULT_VISION_MODEL)

    p = get_provider(provider_id)
    kind = p["kind"]
    b64s = [base64.b64encode(Path(x).read_bytes()).decode() for x in paths]
    max_tok = int(os.getenv("LLM_MAX_TOKENS", "1024"))

    if kind == "ollama":
        import requests
        r = requests.post(
            f"{p['base_url'].rstrip('/')}/api/chat",
            json={"model": model, "stream": False,
                  "messages": [{"role": "user", "content": question, "images": b64s}]},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "") or "(模型无返回)"

    key = provider_key(provider_id)
    if p.get("needs_key") and not key:
        raise ValueError(f"{p['label']} 需要 API key：请在 .env 设置 {p['key_env']}")

    if kind == "anthropic":
        import anthropic
        http = _httpx_clients(True)[0] if p["use_proxy"] else None
        client = anthropic.Anthropic(api_key=key, http_client=http) if http else anthropic.Anthropic(api_key=key)
        content = [{"type": "image", "source": {"type": "base64", "media_type": _media_type(x), "data": b}}
                   for x, b in zip(paths, b64s)]
        content.append({"type": "text", "text": question})
        msg = client.messages.create(model=model, max_tokens=max_tok,
                                     messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in msg.content if b.type == "text") or "(模型无返回)"

    if kind == "openai":
        from openai import OpenAI
        http = _httpx_clients(p["use_proxy"])[0]
        client = OpenAI(api_key=key, base_url=p["base_url"], http_client=http)
        content = [{"type": "image_url", "image_url": {"url": f"data:{_media_type(x)};base64,{b}"}}
                   for x, b in zip(paths, b64s)]
        content.append({"type": "text", "text": question})
        r = client.chat.completions.create(model=model, max_tokens=max_tok,
                                           messages=[{"role": "user", "content": content}])
        return (r.choices[0].message.content or "(模型无返回)")

    raise ValueError(f"不支持的视觉 provider kind：{kind}")
