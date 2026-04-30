"""Per-team FTS connections with table name prefix isolation."""
import sqlite3
from pathlib import Path
from typing import Optional


def create_fts_connection(
    db_path: str,
    team_id: str = "default",
) -> sqlite3.Connection:
    """Create a FTS connection for a specific team.
    Uses table name prefixes to isolate team data in the same SQLite file.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if team_id == "default" else f"{team_id}_"

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS [{prefix}doc_fts] USING fts5(
            doc_id, doc_name, doc_type, machine_model, section_title, text,
            tokenize='unicode61'
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS [{prefix}doc_meta] (
            doc_id TEXT PRIMARY KEY,
            doc_name TEXT,
            doc_type TEXT,
            file_path TEXT,
            machine_model TEXT,
            ingested_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn
