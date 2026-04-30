"""Team registry: manages per-team engine instances and FTS connections."""
import os
from typing import Optional

_engines: dict = {}


def get_engine(team_id: str = "default") -> dict:
    """Get or create engine for a team (cached in memory)."""
    if team_id not in _engines:
        from src.engine_factory import create_engine
        _engines[team_id] = create_engine(team_id)
    return _engines[team_id]


def list_team_collections() -> list:
    """List all Qdrant collections (proxy for teams)."""
    from qdrant_client import QdrantClient
    qdrant_path = os.getenv("QDRANT_PATH", "./data/qdrant_db")
    client = QdrantClient(path=qdrant_path)
    collections = client.get_collections()
    return [c.name for c in collections.collections]


def remove_team(team_id: str) -> bool:
    """Remove a team's engine from cache. Does not delete Qdrant data."""
    return _engines.pop(team_id, None) is not None
