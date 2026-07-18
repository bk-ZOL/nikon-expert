#!/usr/bin/env python3
"""Nikon Expert MCP Server — exposes RAG capabilities as tools for Claude Code."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP

_initialized = False


def _ensure_init():
    """Lazy init: load embedding model + LLM on first tool call."""
    global _initialized
    if _initialized:
        return
    from llama_index.core import Settings
    # Check internal attr directly to avoid LlamaIndex's OpenAI resolver
    if Settings._embed_model is None:
        embed_path = os.getenv("EMBED_MODEL_PATH", "./models/bge-m3")
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from src.device import get_device
        Settings.embed_model = HuggingFaceEmbedding(
            model_name=embed_path, max_length=512, device=get_device(),
        )
    if Settings.llm is None:
        from src.engine_factory import ensure_shared_resources
        ensure_shared_resources()
    _initialized = True


mcp = FastMCP(
    name="nikon-expert",
    instructions=(
        "Nikon lithography equipment knowledge base and troubleshooting assistant. "
        "Supports Chinese, English, and Japanese queries about Nikon scanners "
        "(NSR-S series), error codes, component principles, and maintenance.\n\n"
        "Tools:\n"
        "- nikon_query: Ask a question, get AI answer with citations\n"
        "- nikon_search: Fast keyword search (no LLM)\n"
        "- nikon_search_error_code: Exact error code lookup\n"
        "- nikon_ingest: Add PDF/MD to knowledge base\n"
        "- nikon_list_documents: See what's in the KB\n"
        "- nikon_delete_document: Remove a document\n"
        "- nikon_get_status: System health check"
    ),
)


# ── Core Query ──────────────────────────────────────────────────

@mcp.tool()
def nikon_query(question: str, mode: str = "auto") -> str:
    """Query Nikon Expert knowledge base about lithography equipment.

    Supports error code lookup, troubleshooting, component principles,
    and maintenance procedures. Auto-detects query type (error code,
    component, concept) and routes to best retrieval strategy.

    Args:
        question: The question in Chinese, English, or Japanese
        mode: "auto" (recommended), "qa", or "troubleshoot"
    """
    _ensure_init()
    from src.engine import query
    from src.router import auto_mode

    resolved_mode = auto_mode(question) if mode == "auto" else mode
    try:
        result = query(question, mode=resolved_mode)
    except Exception as e:
        return f"Error: {e}"

    if not result.get("has_result"):
        return result.get("answer", "No relevant information found.")

    parts = [result["answer"], "", "---"]
    parts.append(
        f"Type: {result.get('query_type', '?')} | Mode: {resolved_mode} | "
        f"Sources: {result.get('retrieved', 0)}"
    )

    if result.get("citations"):
        parts.append("")
        parts.append("Citations:")
        for c in result["citations"]:
            parts.append(f"  {c}")

    return "\n".join(parts)


# ── Search ──────────────────────────────────────────────────────

@mcp.tool()
def nikon_search(query: str, doc_type: str = "", machine_model: str = "", limit: int = 8) -> str:
    """Full-text keyword search Nikon Expert knowledge base (no LLM call).

    Use for fast source lookup, verifying references, or exploring KB content.

    Args:
        query: Search keywords (e.g. "stage calibration", "E-5301")
        doc_type: Filter by type: "manual", "fault_history", "checksheet", "obsidian_note"
        machine_model: Filter by model (e.g. "NSR-S630D")
        limit: Max results (default 8)
    """
    from src.fulltext import search_fts

    kwargs = {"query": query, "limit": limit}
    if doc_type:
        kwargs["doc_type"] = doc_type
    if machine_model:
        kwargs["machine_model"] = machine_model

    try:
        results = search_fts(**kwargs)
    except Exception as e:
        return f"Search error: {e}"

    if not results:
        return f"No results for: {query}"

    lines = [f"{len(results)} results for: {query}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('doc_name', '?')} | {r.get('section_title', '')}")
        lines.append(f"    Type: {r.get('doc_type', '')} | Score: {r.get('score', 0):.2f}")
        text = r.get("text", "")[:300]
        lines.append(f"    {text}...")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def nikon_search_error_code(code: str, limit: int = 8) -> str:
    """Look up a specific Nikon error code exactly (e.g. E-5301, P-12345, ALM-04).

    Uses phrase matching for precise error code retrieval.

    Args:
        code: The exact error code
        limit: Max results (default 8)
    """
    from src.fulltext import search_fts_exact

    try:
        results = search_fts_exact(code.strip(), limit=limit)
    except Exception as e:
        return f"Error: {e}"

    if not results:
        return f"No records for: {code}"

    lines = [f"Error code: {code} — {len(results)} matches", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('doc_name', '')} | {r.get('section_title', '')}")
        lines.append(f"    {r.get('text', '')[:400]}")
        lines.append("")

    return "\n".join(lines)


# ── Document Management ─────────────────────────────────────────

@mcp.tool()
def nikon_list_documents() -> str:
    """List all documents in the Nikon Expert knowledge base."""
    _ensure_init()
    from src.engine import get_kb_documents

    try:
        docs = get_kb_documents()
    except Exception as e:
        return f"Error: {e}"

    if not docs:
        return "KB is empty. Use nikon_ingest to add documents."

    lines = [f"{len(docs)} documents in KB", ""]
    for d in docs:
        name = d.get("\u6587\u6863\u540d", d.get("doc_name", "?"))
        dtype = d.get("\u7c7b\u578b", d.get("doc_type", "?"))
        chunks = d.get("Chunks", d.get("chunks", 0))
        lines.append(f"  {name} ({dtype}) \u2014 {chunks} chunks")

    return "\n".join(lines)


@mcp.tool()
def nikon_delete_document(doc_name: str) -> str:
    """Delete a document from the Nikon Expert knowledge base.

    Removes from both vector DB and FTS index.
    Use nikon_list_documents first to find exact document names.

    Args:
        doc_name: Exact document name to delete
    """
    _ensure_init()
    from src.engine import delete_document

    try:
        return delete_document(doc_name)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def nikon_ingest(file_path: str, machine_model: str = "") -> str:
    """Ingest a PDF or Markdown file into the Nikon Expert knowledge base.

    The file will be chunked, embedded, and indexed for search.

    Args:
        file_path: Absolute path to .pdf or .md file
        machine_model: Optional model tag (e.g. "NSR-S630D")
    """
    _ensure_init()
    from src.engine import ingest_file

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"File not found: {file_path}"
    if path.suffix.lower() not in (".pdf", ".md"):
        return f"Unsupported: {path.suffix}. Use .pdf or .md"

    try:
        result = ingest_file(str(path))
    except Exception as e:
        return f"Ingestion failed: {e}"

    return result.get("message", str(result))


# ── Status ──────────────────────────────────────────────────────

@mcp.tool()
def nikon_get_status() -> str:
    """Get Nikon Expert system status — vector DB, FTS index, LLM model, compute device."""
    _ensure_init()
    from src.engine import engine_db_status, get_current_model
    from src.device import get_device

    try:
        db = engine_db_status()
    except Exception:
        db = {"status": "not initialized", "points": 0}

    try:
        model = get_current_model()
    except Exception:
        model = "unknown"

    lines = [
        "Nikon Expert Status",
        "=" * 40,
        f"  Device: {get_device()}",
        f"  LLM: {model}",
        f"  Vector DB: {db.get('collection', '?')} \u2014 {db.get('points', 0)} points",
        f"  FTS: {db.get('fts_records', 0)} records ({db.get('fts_documents', 0)} docs)",
        f"  Status: {db.get('status', '?')}",
    ]
    return "\n".join(lines)


# ── Entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
