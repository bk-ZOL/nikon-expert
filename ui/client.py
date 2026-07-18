"""BackendClient — 本地/远程双模式抽象层。
BACKEND_MODE=local（默认）：直接调用 src.engine
BACKEND_MODE=remote：通过 HTTP 调用 FastAPI 服务
"""
import json
import os

MODE = os.getenv("BACKEND_MODE", "local")
API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")


class BackendClient:
    def __init__(self, team_id: str = "default"):
        self.team_id = team_id

    @property
    def is_local(self):
        return MODE == "local"

    # ── 初始化 ─────────────────────────────────────────────────
    def init_engine(self):
        if self.is_local:
            from src.engine import _init_engine
            _init_engine()

    # ── 查询（流式）────────────────────────────────────────────
    def query_stream(self, question, mode="qa", history=None):
        if self.is_local:
            from src.engine import query_stream
            yield from query_stream(question, mode=mode, history=history)
        else:
            yield from self._remote_query_stream(question, mode, history)

    def _remote_query_stream(self, question, mode, history):
        import requests as _req
        resp = _req.post(
            f"{API_BASE}/api/query",
            json={"question": question, "history": history, "team_id": self.team_id},
            stream=True,
            headers={"X-Team-ID": self.team_id},
            timeout=180,
        )
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            data = json.loads(line[6:])
            yield (
                data.get("delta", ""),
                data.get("is_final", False),
                data.get("citations", []),
                data.get("citations_data", []),
                data.get("has_result", False),
            )

    # ── 摄入文件 ─────────────────────────────────────────────
    def ingest_file(self, file_path):
        if self.is_local:
            from src.engine import ingest_file
            return ingest_file(file_path)
        else:
            import requests as _req
            with open(file_path, "rb") as f:
                resp = _req.post(
                    f"{API_BASE}/api/ingest",
                    files={"file": (os.path.basename(file_path), f)},
                    data={"team_id": self.team_id},
                    headers={"X-Team-ID": self.team_id},
                    timeout=120,
                )
            return resp.json()

    # ── 文档管理 ─────────────────────────────────────────────
    def get_kb_documents(self):
        if self.is_local:
            from src.engine import get_kb_documents
            return get_kb_documents()
        else:
            import requests as _req
            resp = _req.get(
                f"{API_BASE}/api/documents",
                params={"team_id": self.team_id},
                headers={"X-Team-ID": self.team_id},
            )
            return [dict(d) for d in resp.json()]

    def delete_document(self, doc_name):
        if self.is_local:
            from src.engine import delete_document
            return delete_document(doc_name)
        else:
            import requests as _req
            resp = _req.delete(
                f"{API_BASE}/api/documents/{doc_name}",
                headers={"X-Team-ID": self.team_id},
            )
            return resp.json()

    # ── 知识库状态 ───────────────────────────────────────────
    def db_status(self):
        if self.is_local:
            from src.engine import engine_db_status
            return engine_db_status()
        else:
            import requests as _req
            resp = _req.get(
                f"{API_BASE}/api/status",
                params={"team_id": self.team_id},
                headers={"X-Team-ID": self.team_id},
            )
            return resp.json()

    # ── FTS 索引 ─────────────────────────────────────────────
    def fts_index(self, dir_path, pattern="*.md"):
        if self.is_local:
            from src.fulltext import index_directory
            return index_directory(dir_path, pattern)
        else:
            import requests as _req
            resp = _req.post(
                f"{API_BASE}/api/fts/index",
                params={"dir_path": dir_path, "pattern": pattern, "team_id": self.team_id},
                headers={"X-Team-ID": self.team_id},
            )
            return resp.json()

    def fts_clear(self):
        if self.is_local:
            from src.fulltext import clear_fts_index
            count = clear_fts_index(doc_type="grep_source")
            return {"cleared": count}
        else:
            import requests as _req
            resp = _req.post(
                f"{API_BASE}/api/fts/clear",
                params={"team_id": self.team_id},
                headers={"X-Team-ID": self.team_id},
            )
            return resp.json()

    def fts_paths(self):
        if self.is_local:
            from src.fulltext import get_indexed_paths
            return get_indexed_paths()
        else:
            return []  # FTS paths not exposed via API yet

    # ── LLM 管理 ─────────────────────────────────────────────
    def get_ollama_models(self, base_url="http://localhost:11434"):
        if self.is_local:
            import requests as _req
            try:
                r = _req.get(f"{base_url}/api/tags", timeout=3)
                return [m["name"] for m in r.json().get("models", [])]
            except Exception:
                return []
        else:
            import requests as _req
            resp = _req.get(f"{API_BASE}/api/models")
            return resp.json().get("models", [])

    def switch_llm(self, model_name, base_url="http://localhost:11434"):
        if self.is_local:
            from src.engine import switch_llm
            import os as _os
            _os.environ["LLM_BASE_URL"] = base_url
            switch_llm(model_name)
            return f"已切换至 {model_name}"
        else:
            import requests as _req
            resp = _req.post(f"{API_BASE}/api/models/switch", params={"model_name": model_name})
            return resp.json().get("message", "")

    def ingest_okf(self, path):
        """导入 OKF 目录/文件（本地模式直接调 engine）。"""
        if self.is_local:
            from src.engine import ingest_okf
            return ingest_okf(path)
        else:
            import requests as _req
            resp = _req.post(f"{API_BASE}/api/ingest_okf", params={"path": path})
            return resp.json()

    def switch_provider(self, provider_id, model_name):
        """切换外接大脑（本地模式直接调 engine）。返回状态文案；缺 key 等错误向上抛。"""
        if self.is_local:
            from src.engine import switch_provider
            return switch_provider(provider_id, model_name)
        else:
            import requests as _req
            resp = _req.post(
                f"{API_BASE}/api/provider/switch",
                params={"provider_id": provider_id, "model_name": model_name},
            )
            return resp.json().get("message", "")

    def get_current_model(self):
        if self.is_local:
            from src.engine import get_current_model
            return get_current_model()
        else:
            # Remote mode: model is managed server-side
            return os.getenv("LLM_MODEL", "unknown")

    # ── 团队管理 ─────────────────────────────────────────────
    def list_teams(self):
        import requests as _req
        resp = _req.get(f"{API_BASE}/api/teams")
        return resp.json().get("teams", [])

    def create_team(self, team_id):
        import requests as _req
        resp = _req.post(f"{API_BASE}/api/teams", params={"team_id": team_id})
        return resp.json()
