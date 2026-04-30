from typing import Optional
from pydantic import BaseModel


class QueryRequest(BaseModel):
    question: str
    history: Optional[list] = None
    team_id: str = "default"


class QueryChunk(BaseModel):
    delta: str = ""
    is_final: bool = False
    citations: list = []
    citations_data: list = []
    has_result: bool = False


class IngestRequest(BaseModel):
    file_path: str
    team_id: str = "default"


class IngestResponse(BaseModel):
    success: bool
    message: str
    chunks: Optional[int] = None


class DocumentItem(BaseModel):
    doc_name: str
    doc_type: str
    location: str
    chunks: int


class FtsIndexRequest(BaseModel):
    dir_path: str
    pattern: str = "*.md"
    team_id: str = "default"


class TeamCreateRequest(BaseModel):
    team_id: str
    description: Optional[str] = None


class StatusResponse(BaseModel):
    collection: str
    points: int
    fts_records: int
    status: str
    device: str
