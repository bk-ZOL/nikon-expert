#!/usr/bin/env python3
"""
手动下载 Embedding 模型（网络失败时单独运行）。
用法：python scripts/download_models.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from pathlib import Path
from huggingface_hub import snapshot_download


def download(repo_id: str, local_dir: str, name: str):
    p = Path(local_dir)
    if p.exists() and any(p.iterdir()):
        print(f"✅ {name} 已存在，跳过")
        return
    print(f"⏳ 下载 {name}（{repo_id}）...")
    snapshot_download(
        repo_id=repo_id,
        local_dir=local_dir,
        ignore_patterns=["*.pt", "flax_*", "tf_*", "*.msgpack", "rust_model.ot"],
    )
    print(f"✅ {name} 下载完成 → {local_dir}")


if __name__ == "__main__":
    embed_path    = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
    reranker_path = os.getenv("RERANKER_MODEL_PATH", "./models/bge-reranker-v2-m3")

    download("BAAI/bge-m3",              embed_path,    "BGE-M3 Embedding (~2.5GB)")
    download("BAAI/bge-reranker-v2-m3",  reranker_path, "BGE-Reranker-v2-m3 (~1.1GB)")

    print("\n🎉 所有模型下载完成！")
