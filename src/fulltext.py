# src/fulltext.py
# Nikon Expert — 全文搜索引擎 (Karpathy 式 grep 层)
# SQLite FTS5，零外部依赖，毫秒级关键词/Error Code 精确匹配

import os
import re
import hashlib
import sqlite3
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_db_path = os.getenv("FTS_DB_PATH", "./data/fts.db")
_conn: Optional[sqlite3.Connection] = None


def init_fts(db_path: str = None) -> sqlite3.Connection:
    """初始化 FTS5 数据库，返回连接"""
    global _conn
    db_path = db_path or _db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
            doc_id, doc_name, doc_type, machine_model, section_title, text,
            tokenize='unicode61'
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_meta (
            doc_id TEXT PRIMARY KEY,
            doc_name TEXT,
            doc_type TEXT,
            file_path TEXT,
            machine_model TEXT,
            ingested_at TEXT DEFAULT (datetime('now'))
        )
    """)
    _conn.commit()
    return _conn


def _get_conn() -> sqlite3.Connection:
    if _conn is None:
        return init_fts()
    return _conn


def ingest_fts(
    doc_id: str,
    doc_name: str,
    doc_type: str,
    machine_model: str,
    section_title: str,
    text: str,
    file_path: str = "",
) -> None:
    """写入一条全文记录"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO doc_fts VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, doc_name, doc_type, machine_model, section_title, text),
    )
    conn.execute(
        """INSERT OR REPLACE INTO doc_meta VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (doc_id, doc_name, doc_type, file_path, machine_model),
    )
    conn.commit()


def delete_doc_fts(doc_id: str) -> int:
    """删除指定文档的所有 FTS 记录，返回删除条数"""
    conn = _get_conn()
    cur = conn.execute("SELECT count() FROM doc_fts WHERE doc_id = ?", (doc_id,))
    count = cur.fetchone()[0]
    conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM doc_meta WHERE doc_id = ?", (doc_id,))
    conn.commit()
    return count


def delete_doc_fts_by_name(doc_name: str) -> int:
    """按 doc_name 删除"""
    conn = _get_conn()
    cur = conn.execute("SELECT count() FROM doc_fts WHERE doc_name = ?", (doc_name,))
    count = cur.fetchone()[0]
    conn.execute("DELETE FROM doc_fts WHERE doc_name = ?", (doc_name,))
    conn.execute("DELETE FROM doc_meta WHERE doc_name = ?", (doc_name,))
    conn.commit()
    return count


def search_fts(
    query: str,
    doc_type: str = None,
    machine_model: str = None,
    limit: int = 8,
) -> list:
    """
    BM25 全文搜索。
    返回: [{"doc_id", "doc_name", "doc_type", "machine_model", "section_title",
            "text", "score", "file_path"}]
    """
    conn = _get_conn()

    where = "doc_fts MATCH ?"
    params = [query]

    if doc_type:
        where += " AND doc_type = ?"
        params.append(doc_type)
    if machine_model:
        where += " AND machine_model = ?"
        params.append(machine_model)

    sql = f"""
        SELECT
            doc_fts.doc_id, doc_fts.doc_name, doc_fts.doc_type,
            doc_fts.machine_model, doc_fts.section_title, doc_fts.text,
            bm25(doc_fts) AS score,
            COALESCE(m.file_path, '') AS file_path
        FROM doc_fts
        LEFT JOIN doc_meta m ON doc_fts.doc_id = m.doc_id
        WHERE {where}
        ORDER BY score
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "doc_id": r[0], "doc_name": r[1], "doc_type": r[2],
            "machine_model": r[3], "section_title": r[4], "text": r[5],
            "score": r[6], "file_path": r[7],
            "source": "fts",
        })
    return results


def search_fts_exact(code: str, limit: int = 8) -> list:
    """
    精确匹配 Error Code / 部件号。
    用 FTS5 phrase query: MATCH '"E-5301"'
    """
    quoted = f'"{code}"'
    return search_fts(quoted, limit=limit)


def fts_status() -> dict:
    """返回 FTS 数据库状态"""
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT count() FROM doc_fts")
        count = cur.fetchone()[0]
        doc_cur = conn.execute("SELECT count(DISTINCT doc_id) FROM doc_meta")
        doc_count = doc_cur.fetchone()[0]
        return {"records": count, "documents": doc_count, "status": "ok"}
    except Exception as e:
        return {"records": 0, "documents": 0, "status": str(e)}


def index_directory(dir_path: str, pattern: str = "*.md", doc_type: str = "grep_source") -> dict:
    """
    Karpathy 式批量索引：直接读文件原文写入 FTS，不向量化。
    支持通配符: "*.md", "*.txt", "*.pdf", "*.md;*.txt"（分号分隔多类型）

    返回: {"indexed": 文件数, "records": 记录数, "errors": 错误列表}
    """
    conn = _get_conn()
    results = {"indexed": 0, "records": 0, "errors": []}

    root = Path(dir_path)
    if not root.is_dir():
        results["errors"].append(f"路径不存在：{dir_path}")
        return results

    # 支持分号分隔多 pattern
    patterns = [p.strip() for p in pattern.split(";")]
    files = []
    for pat in patterns:
        files.extend(root.rglob(pat))

    # 去重（同名文件可能匹配多个 pattern）
    seen = set()
    unique_files = []
    for f in files:
        key = str(f)
        if key not in seen:
            seen.add(key)
            unique_files.append(f)

    print(f"🔍 扫描到 {len(unique_files)} 个文件，开始索引...")

    for f in unique_files:
        try:
            # 跳过不可读文件（iCloud 占位符等）
            f.read_bytes()
        except (OSError, IOError):
            continue

        rid = hashlib.sha256(str(f).encode()).hexdigest()[:12]
        doc_name = f.name

        # 读取文本
        suffix = f.suffix.lower()
        if suffix in (".md", ".txt", ".markdown", ".rst"):
            try:
                text = f.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError:
                results["errors"].append(f"编码错误：{doc_name}")
                continue
        elif suffix == ".pdf":
            text = _extract_pdf_text(f)
            if not text:
                continue
        else:
            continue

        if not text or len(text) < 5:
            continue

        # 去重：先删旧数据
        delete_doc_fts(rid)

        # 按 ## 标题分区，保持原始结构
        sections = _split_sections(text)
        for sec_title, sec_text in sections:
            if not sec_text.strip():
                continue
            ingest_fts(rid, doc_name, doc_type, "", sec_title, sec_text, str(f))
            results["records"] += 1

        results["indexed"] += 1

    conn.commit()
    print(f"✅ FTS 索引完成：{results['indexed']} 文件，{results['records']} 条记录")
    return results


def clear_fts_index(doc_type: str = None) -> int:
    """清空 FTS 索引（可选按 doc_type 过滤）"""
    conn = _get_conn()
    if doc_type:
        cur = conn.execute("SELECT count() FROM doc_fts WHERE doc_type = ?", (doc_type,))
        count = cur.fetchone()[0]
        conn.execute("DELETE FROM doc_fts WHERE doc_type = ?", (doc_type,))
        conn.execute("DELETE FROM doc_meta WHERE doc_type = ?", (doc_type,))
    else:
        cur = conn.execute("SELECT count() FROM doc_fts")
        count = cur.fetchone()[0]
        conn.execute("DELETE FROM doc_fts")
        conn.execute("DELETE FROM doc_meta")
    conn.commit()
    return count


def get_indexed_paths() -> list:
    """返回已索引的目录/文件路径列表"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT file_path, doc_type FROM doc_meta WHERE file_path != '' ORDER BY file_path"
    ).fetchall()
    return [{"path": r[0], "doc_type": r[1]} for r in rows]


def _split_sections(text: str) -> list:
    """按 # / ## 标题分割，短文本保持整体"""
    if len(text) < 500:
        return [("", text)]

    sections = re.split(r'^(#{1,3}\s.+)$', text, flags=re.MULTILINE)
    result = []
    current_title = ""
    current_body = ""

    for part in sections:
        if re.match(r'^#{1,3}\s', part):
            if current_body.strip():
                result.append((current_title, current_body.strip()))
            current_title = part.strip()
            current_body = ""
        else:
            current_body += part

    if current_body.strip():
        result.append((current_title, current_body.strip()))

    return result if result else [("", text)]


def _extract_pdf_text(path: Path) -> str:
    """从 PDF 提取文本（简化版，仅用于 FTS）"""
    try:
        import pymupdf4llm
        pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        return "\n".join(p.get("text", "") for p in pages if p.get("text", "").strip())
    except Exception:
        return ""
