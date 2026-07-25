# src/providers.py
# Nikon Expert — 多 LLM Provider 切换层（"外接大脑"）
# 统一构造 llama_index LLM 并设置 Settings.llm，RAG 与 Agent 两条路径都随之切换。
#
# 设计：
#   - 本地/内网 Ollama：默认，零外传。
#   - Claude(Anthropic)：原生集成，海外，走本地 socks5 代理。
#   - 其余（GPT/Gemini/DeepSeek/千问/GLM/Kimi）：OpenAI 兼容接口，各家 base_url。
#       海外(GPT/Gemini) 走代理；国内(DeepSeek/千问/GLM/Kimi) 直连。
#   - 各家 API key 从 .env 读（ANTHROPIC_API_KEY / DEEPSEEK_API_KEY ...），绝不硬编码。
#
# ⚠️ 数据外传：选用云端 provider 时，"检索到的知识库片段 + 问题"会发往对应 API。
#     embedding/检索始终本地。本地 Ollama 为默认，保持零外传。

import os

# 本地代理（Clash/socks5）。海外 provider 用它绕过封锁；国内直连不用。
PROXY = os.getenv("LLM_PROXY", "socks5h://127.0.0.1:7890")

REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "180"))
CLOUD_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))


# ── Provider 注册表 ─────────────────────────────────────────────
# kind: ollama | anthropic | openai(=OpenAI 兼容)
# models: 预设可选模型（可在 .env 用 <ID>_MODEL 覆盖默认，或 UI 里手填）
# key_env: 读 API key 的环境变量名
# use_proxy: 是否走本地 socks5 代理（海外为 True）
PROVIDERS = {
    "ollama_local": {
        "label": "本地 Ollama（零外传·默认）",
        "kind": "ollama",
        "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434"),
        "models": [],          # 动态从 ollama 拉取
        "use_proxy": False,
        "needs_key": False,
    },
    "ollama_lan": {
        "label": "内网 Ollama",
        "kind": "ollama",
        "base_url": os.getenv("REMOTE_OLLAMA_URL", "http://192.168.168.208:11434"),
        "models": [],
        "use_proxy": False,
        "needs_key": False,
    },
    "claude": {
        "label": "Claude（Anthropic）",
        "kind": "anthropic",
        "base_url": None,
        "models": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
        "key_env": "ANTHROPIC_API_KEY",
        "use_proxy": True,
        "needs_key": True,
    },
    "openai": {
        "label": "ChatGPT（OpenAI）",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "key_env": "OPENAI_API_KEY",
        "use_proxy": True,
        "needs_key": True,
    },
    "gemini": {
        "label": "Gemini（Google）",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "key_env": "GEMINI_API_KEY",
        "use_proxy": True,
        "needs_key": True,
    },
    "deepseek": {
        "label": "DeepSeek（深度求索）",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_env": "DEEPSEEK_API_KEY",
        "use_proxy": False,
        "needs_key": True,
    },
    "qwen": {
        "label": "千问（阿里 DashScope）",
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "key_env": "DASHSCOPE_API_KEY",
        "use_proxy": False,
        "needs_key": True,
    },
    "glm": {
        "label": "GLM（智谱）",
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4", "glm-4-air"],
        "key_env": "ZHIPU_API_KEY",
        "use_proxy": False,
        "needs_key": True,
    },
    "kimi": {
        "label": "Kimi（Moonshot）",
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-32k", "moonshot-v1-128k", "moonshot-v1-8k"],
        "key_env": "MOONSHOT_API_KEY",
        "use_proxy": False,
        "needs_key": True,
    },
}


def list_providers() -> list:
    """返回 [(id, label), ...] 供 UI 下拉。"""
    return [(pid, p["label"]) for pid, p in PROVIDERS.items()]


def get_provider(provider_id: str) -> dict:
    if provider_id not in PROVIDERS:
        raise ValueError(f"未知 provider：{provider_id}")
    return PROVIDERS[provider_id]


def provider_key(provider_id: str) -> str:
    """读取该 provider 的 API key（.env）。默认模型可用 <ID>_MODEL 覆盖，这里只管 key。"""
    p = PROVIDERS[provider_id]
    return os.getenv(p.get("key_env", ""), "") if p.get("needs_key") else ""


def list_models(provider_id: str) -> list:
    """返回该 provider 的可选模型列表。Ollama 动态拉取，其余用预设。"""
    p = get_provider(provider_id)
    if p["kind"] == "ollama":
        return _ollama_models(p["base_url"])
    return list(p["models"])


def _ollama_models(base_url: str) -> list:
    import requests
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _httpx_clients(use_proxy: bool):
    """构造 (sync, async) httpx client：海外走 socks5 代理，国内直连。
    trust_env=False 避免误继承环境里的 ALL_PROXY。"""
    import httpx
    # PROXY 为空（如墙外服务器）时直连，不走代理
    proxy = PROXY if (use_proxy and PROXY) else None
    sync = httpx.Client(proxy=proxy, trust_env=False, timeout=REQUEST_TIMEOUT)
    asyncc = httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=REQUEST_TIMEOUT)
    return sync, asyncc


def build_llm(provider_id: str, model: str):
    """按 provider + model 构造 llama_index LLM。缺 key 抛 ValueError（含友好提示）。"""
    p = get_provider(provider_id)
    kind = p["kind"]

    if kind == "ollama":
        from llama_index.llms.ollama import Ollama
        return Ollama(model=model, base_url=p["base_url"], request_timeout=REQUEST_TIMEOUT)

    key = provider_key(provider_id)
    if p.get("needs_key") and not key:
        raise ValueError(
            f"{p['label']} 需要 API key：请在 .env 中设置 {p['key_env']}=<你的key>"
        )

    if kind == "anthropic":
        from llama_index.llms.anthropic import Anthropic
        llm = Anthropic(model=model, api_key=key, max_tokens=CLOUD_MAX_TOKENS)
        if p["use_proxy"]:
            # llama_index 的 Anthropic 内部自建 SDK client、不暴露 http_client，
            # 这里直接覆盖为带 socks5 代理的 client，确保绕过封锁。
            import anthropic
            sync, asyncc = _httpx_clients(True)
            llm._client = anthropic.Anthropic(api_key=key, http_client=sync)
            llm._aclient = anthropic.AsyncAnthropic(api_key=key, http_client=asyncc)
        return llm

    if kind == "openai":
        # 用 OpenAILike：不校验模型名是否属于 OpenAI 官方列表，适配各家兼容接口。
        from llama_index.llms.openai_like import OpenAILike
        sync, asyncc = _httpx_clients(p["use_proxy"])
        return OpenAILike(
            model=model,
            api_base=p["base_url"],
            api_key=key,
            http_client=sync,
            async_http_client=asyncc,
            is_chat_model=True,
            is_function_calling_model=True,
            context_window=int(os.getenv("CLOUD_CONTEXT_WINDOW", "32768")),
            max_tokens=CLOUD_MAX_TOKENS,
            timeout=REQUEST_TIMEOUT,
        )

    raise ValueError(f"不支持的 provider kind：{kind}")
