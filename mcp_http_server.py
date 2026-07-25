#!/usr/bin/env python3
"""nikon-expert MCP over streamable-http (remote/web connector).

Reuses tools from mcp_server.py. Binds 127.0.0.1:8000 behind cloudflared.
Fully initializes the engine via engine._init_engine() at startup — same path
as the Gradio app — so nikon_query / status / list_documents all work
(the shared-resources path leaves engine._engine=None -> "not initialized").
Requires Qdrant in server mode (QDRANT_URL) so it can coexist with the UI service.
"""
import os
import sys
from mcp_server import mcp  # load_dotenv() + registers @mcp.tool()
import mcp_tools_extra  # registers nikon_view_diagram
from mcp.server.transport_security import TransportSecuritySettings

PUBLIC_HOST = os.getenv("MCP_PUBLIC_HOST", "mcp.xbtxhuhhis.ccwu.cc")
mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
mcp.settings.port = int(os.getenv("MCP_PORT", "8000"))
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:*", "127.0.0.1:*", "localhost:*"],
    allowed_origins=[
        f"https://{PUBLIC_HOST}", "https://claude.ai", "https://*.claude.ai",
        "http://127.0.0.1:*", "http://localhost:*",
    ],
)

# Full engine init (LLM + embed + Qdrant-server client + FTS + _engine global)
try:
    from src import engine
    engine._init_engine()
    print("[mcp] engine initialized (full)", file=sys.stderr)
except Exception as e:
    import traceback; traceback.print_exc()
    print(f"[mcp] WARN: engine init failed, FTS-only: {e}", file=sys.stderr)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
