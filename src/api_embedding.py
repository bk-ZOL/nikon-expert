"""src/api_embedding.py —— 调云端 BGE-M3 Embedding API（OpenAI 兼容，如硅基流动）。

让服务器**不用本地 2 核 CPU 嵌向量**：ingestion 变快、query 也快，还省掉本地 BGE-M3
的 ~1.2G 内存。向量仍是 **BGE-M3**，与现有 2.5 万向量兼容（同模型同向量空间）。

启用：.env 里设 `EMBED_API_KEY`（+可选 `EMBED_API_BASE`、`EMBED_API_MODEL`）。
默认 base=硅基流动，model=BAAI/bge-m3。代价：文本片段会发给该 API（同 LLM 一样的外传性质）。
"""
import os
from typing import List

import httpx
from llama_index.core.bridge.pydantic import Field, PrivateAttr
from llama_index.core.embeddings import BaseEmbedding


# 免费额度有频率限制：请求之间留间隔，失败按指数退避重试
_LAST_CALL = [0.0]
_MAX_CHARS = int(os.getenv("EMBED_MAX_CHARS", "6000"))


class APIEmbedding(BaseEmbedding):
    api_base: str = Field(default="https://api.siliconflow.cn/v1")
    api_key: str = Field(default="")
    _client: httpx.Client = PrivateAttr()

    def __init__(self, model_name: str = None, api_base: str = None,
                 api_key: str = None, embed_batch_size: int = int(os.getenv("EMBED_BATCH_SIZE", "16")), **kw):
        super().__init__(
            model_name=model_name or os.getenv("EMBED_API_MODEL", "BAAI/bge-m3"),
            embed_batch_size=embed_batch_size, **kw)
        self.api_base = (api_base or os.getenv("EMBED_API_BASE",
                                               "https://api.siliconflow.cn/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("EMBED_API_KEY", "")
        # 国内直连、不吃系统代理（避免 Clash 把国内 API 也代理走）
        self._client = httpx.Client(timeout=60, trust_env=False)

    @classmethod
    def class_name(cls) -> str:
        return "APIEmbedding"

    def _post(self, texts: List[str]) -> List[List[float]]:
        import time
        delay, r, err = 2.0, None, None
        for attempt in range(7):
            wait = _LAST_CALL[0] + float(os.getenv("EMBED_MIN_INTERVAL", "0.3")) - time.time()
            if wait > 0:
                time.sleep(wait)
            r = err = None
            try:
                r = self._client.post(
                    f"{self.api_base}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json={"model": self.model_name, "input": texts, "encoding_format": "float"},
                )
            except httpx.TransportError as e:
                err = e
            _LAST_CALL[0] = time.time()
            if r is not None and r.status_code == 200:
                data = sorted(r.json()["data"], key=lambda x: x["index"])
                return [d["embedding"] for d in data]
            if r is not None and r.status_code == 400:
                r.raise_for_status()                      # 输入问题，交给 _embed 拆分处理
            ra = r.headers.get("Retry-After") if r is not None else None
            pause = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
            why = r.status_code if r is not None else type(err).__name__
            print(f"    ⏳ embedding API {why}，{pause:.0f}s 后重试（第 {attempt + 1} 次）", flush=True)
            time.sleep(pause)
            delay = min(delay * 2, 60)
        if r is not None:
            r.raise_for_status()
        raise err

    def _embed(self, texts: List[str]) -> List[List[float]]:
        texts = [((t or "").strip() or " ")[:_MAX_CHARS] for t in texts]
        try:
            return self._post(texts)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400:
                raise
            if len(texts) > 1:                            # 一批被拒：对半拆开找出问题段
                mid = len(texts) // 2
                return self._embed(texts[:mid]) + self._embed(texts[mid:])
            if len(texts[0]) > 500:                       # 单段被拒：截短再试
                return self._embed([texts[0][: len(texts[0]) // 2]])
            raise

    def _get_text_embedding(self, text: str) -> List[float]:
        return self._embed([text])[0]

    def _get_query_embedding(self, query: str) -> List[float]:
        return self._embed([query])[0]

    def _get_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        return self._embed(texts)

    async def _aget_query_embedding(self, query: str) -> List[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> List[float]:
        return self._get_text_embedding(text)
