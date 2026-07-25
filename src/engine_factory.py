"""Per-team engine instance factory.
Reuses shared Embedding model and LLM, but creates isolated
Qdrant collections and FTS connections per team.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from src.device import get_device


def _collection_name(team_id: str = "default") -> str:
    base = os.getenv("COLLECTION_NAME", "nikon_expert_v1")
    if team_id and team_id != "default":
        return f"{team_id}_{base}"
    return base


def create_engine(team_id: str = "default"):
    """Create a fully initialized engine dict for a given team.
    Returns dict with keys: retriever, reranker, client, collection, fts_conn
    """
    from llama_index.core import VectorStoreIndex, StorageContext, Settings
    from llama_index.core.retrievers import VectorIndexRetriever
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams
    from src.fts_factory import create_fts_connection

    qdrant_path  = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    fts_path     = os.getenv("FTS_DB_PATH", "./data/fts.db")
    collection   = _collection_name(team_id)
    llm_model    = os.getenv("LLM_MODEL", "qwen2.5:14b-instruct-q6_K")
    llm_url      = os.getenv("LLM_BASE_URL", "http://localhost:11434")
    top_k        = int(os.getenv("RETRIEVAL_TOP_K", "8"))
    rerank_top_n = int(os.getenv("RERANK_TOP_N", "4"))

    # Shared embedding model (loaded once globally)
    if Settings.embed_model is None:
        embed_path = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=embed_path,
            max_length=512,
            device=get_device(),
        )

    # Shared LLM connection
    if Settings.llm is None:
        from llama_index.llms.ollama import Ollama
        Settings.llm = Ollama(
            model=llm_model,
            base_url=llm_url,
            request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
        )

    # Per-team: Qdrant collection
    client = QdrantClient(url=os.getenv("QDRANT_URL")) if os.getenv("QDRANT_URL") else QdrantClient(path=qdrant_path)
    existing = [c.name for c in client.get_collections().collections]
    if collection not in existing:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )

    vector_store = QdrantVectorStore(client=client, collection_name=collection)
    storage_ctx = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_vector_store(vector_store, storage_context=storage_ctx)
    retriever = VectorIndexRetriever(index=index, similarity_top_k=top_k)

    # Per-team: FTS connection (table prefix isolation)
    fts_conn = create_fts_connection(fts_path, team_id)

    return {
        "retriever": retriever,
        "reranker": None,  # Reranker loaded separately if needed
        "client": client,
        "collection": collection,
        "fts_conn": fts_conn,
    }


def ensure_shared_resources():
    """Load shared models once (call at server startup)."""
    from llama_index.core import Settings
    if Settings.embed_model is None:
        embed_path = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=embed_path,
            max_length=512,
            device=get_device(),
        )
        print(f"⚙️  Embedding 模型已加载（{get_device()}）")

    if Settings.llm is None:
        from llama_index.llms.ollama import Ollama
        llm_model = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct-q8_0")
        llm_url = os.getenv("LLM_BASE_URL", "http://localhost:11434")
        Settings.llm = Ollama(
            model=llm_model,
            base_url=llm_url,
            request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "180")),
        )
        print(f"⚙️  LLM 已连接：{llm_model}")
