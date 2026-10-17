#!/usr/bin/env python3
"""scripts/mac_embed_server.py —— 在 Mac 上跑的 BGE-M3 嵌入服务（OpenAI 兼容）。

让云端服务器（Vultr 上的 nikon-expert 主页面）经 Tailscale 借用**你 Mac 的算力**嵌向量：
服务器上传时把文本 POST 到本服务，Mac 用本地 BGE-M3（优先 Apple MPS/GPU）嵌好返回。
向量仍是 BGE-M3 dense（1024 维），与现有云端/服务器本地向量同空间兼容（Qdrant 用 COSINE）。

用法：`~/nikon-expert/.venv/bin/python scripts/mac_embed_server.py`
默认绑 0.0.0.0:7861（Tailscale 可达）。服务器侧把 EMBED_API_BASE 指到 http://<MacTailscaleIP>:7861/v1 即可。
⚠️ Mac 睡眠/关机即断（同 Ollama 桥的性质）。
"""
import os
from typing import List, Union

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

MODEL_PATH = os.path.expanduser(os.getenv("MAC_EMBED_MODEL_PATH", "~/nikon-expert/models/bge-m3"))
PORT = int(os.getenv("MAC_EMBED_PORT", "7861"))

_model = None


def _get_model():
    global _model
    if _model is None:
        import torch
        from sentence_transformers import SentenceTransformer
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"⚙️  加载 BGE-M3：{MODEL_PATH}（device={device}）")
        _model = SentenceTransformer(MODEL_PATH, device=device)
        print("✅ 模型就绪")
    return _model


app = FastAPI(title="Mac BGE-M3 Embedding Server")


class EmbReq(BaseModel):
    input: Union[str, List[str]]
    model: str = "BAAI/bge-m3"


@app.get("/health")
def health():
    return {"status": "ok", "model": "BAAI/bge-m3", "loaded": _model is not None}


@app.post("/v1/embeddings")
def embeddings(req: EmbReq):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    m = _get_model()
    vecs = m.encode(texts, normalize_embeddings=True, batch_size=32,
                    convert_to_numpy=True, show_progress_bar=False)
    data = [{"object": "embedding", "index": i, "embedding": v.tolist()}
            for i, v in enumerate(vecs)]
    return {"object": "list", "data": data, "model": req.model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


if __name__ == "__main__":
    _get_model()  # 启动即加载，首个请求不用等
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
