"""Diagram identifier inverted-index + per-page tags (unit / figure-kind).

Generic & incremental: iterates the FTS page rows (doc_fts) and, using ONLY the
patterns in diagram_config, extracts strong identifiers and infers per-page
labels. New documents are picked up automatically on (re)build — nothing about a
specific machine / page range / figure number is hardcoded here.

Tables (created in the same SQLite db as FTS, data/fts.db):
  diagram_tokens(token, doc_name, doc_type, page)         -- inverted index
  diagram_pages(doc_name, doc_type, page, unit, kind)     -- per-page tags
  diagram_index_state(doc_name, indexed_at)               -- incremental marker
"""
import os
import re

from src import diagram_config as C

_DB = os.getenv("FTS_DB_PATH", "./data/fts.db")


def _conn():
    try:
        import pysqlite3 as sq  # trigram FTS build needs this on old system sqlite
    except Exception:
        import sqlite3 as sq
    return sq.connect(_DB)


def _ensure_tables(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS diagram_tokens(
            token TEXT, doc_name TEXT, doc_type TEXT, page INTEGER);
        CREATE INDEX IF NOT EXISTS ix_dt_token ON diagram_tokens(token);
        CREATE INDEX IF NOT EXISTS ix_dt_doc   ON diagram_tokens(doc_name);
        CREATE TABLE IF NOT EXISTS diagram_pages(
            doc_name TEXT, doc_type TEXT, page INTEGER, unit TEXT, kind TEXT,
            PRIMARY KEY(doc_name, page));
        CREATE TABLE IF NOT EXISTS diagram_index_state(
            doc_name TEXT PRIMARY KEY, indexed_at TEXT);
        """
    )


# ── extraction (pure, testable) ──────────────────────────────────────────
def extract_identifiers(text: str) -> set:
    """Return the set of strong-identifier tokens found in text (upper-cased)."""
    if not text:
        return set()
    out = set()
    for _name, rx in C.IDENTIFIER_PATTERNS:
        for m in rx.findall(text):
            tok = m.strip().upper()
            if len(tok) >= C.MIN_TOKEN_LEN:
                out.add(tok)
    return out


def classify_figure_kind(text: str):
    if not text:
        return None
    low = text.lower()
    for kind, kws in C.FIGURE_KIND_KEYWORDS:
        for kw in kws:
            if kw.lower() in low:
                return kind
    return None


def infer_unit(text: str):
    if not text:
        return None
    for rx in C.UNIT_PATTERNS:
        m = rx.search(text)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip().upper()
    return None


def page_from_section(section_title: str):
    if not section_title:
        return None
    m = C.PAGE_IN_SECTION.search(str(section_title))
    return int(m.group(1)) if m else None


# ── build (incremental) ──────────────────────────────────────────────────
def build_index(incremental: bool = True, doc_types=("circuit_diagram",),
                verbose: bool = True):
    con = _conn()
    _ensure_tables(con)
    cur = con.cursor()

    # documents in scope + their latest ingested_at (change detection)
    q = "SELECT doc_name, MAX(ingested_at) FROM doc_meta"
    params = []
    if doc_types:
        q += " WHERE doc_type IN (%s)" % ",".join("?" * len(doc_types))
        params = list(doc_types)
    q += " GROUP BY doc_name"
    docs = cur.execute(q, params).fetchall()

    state = dict(cur.execute("SELECT doc_name, indexed_at FROM diagram_index_state").fetchall())
    total_docs = total_tok = total_pg = 0

    for doc_name, ingested_at in docs:
        if incremental and state.get(doc_name) and state[doc_name] >= (ingested_at or ""):
            continue
        # reindex this document: drop its old rows
        cur.execute("DELETE FROM diagram_tokens WHERE doc_name=?", (doc_name,))
        cur.execute("DELETE FROM diagram_pages  WHERE doc_name=?", (doc_name,))

        rows = cur.execute(
            "SELECT doc_type, section_title, text FROM doc_fts WHERE doc_name=?",
            (doc_name,),
        ).fetchall()
        tok_rows, pg_rows = [], []
        for doc_type, section, text in rows:
            page = page_from_section(section)
            if page is None:
                continue
            for tok in extract_identifiers(text):
                tok_rows.append((tok, doc_name, doc_type, page))
            pg_rows.append((doc_name, doc_type, page,
                            infer_unit(text), classify_figure_kind(text)))
        cur.executemany(
            "INSERT INTO diagram_tokens(token,doc_name,doc_type,page) VALUES(?,?,?,?)",
            tok_rows)
        cur.executemany(
            "INSERT OR REPLACE INTO diagram_pages(doc_name,doc_type,page,unit,kind) "
            "VALUES(?,?,?,?,?)", pg_rows)
        cur.execute(
            "INSERT OR REPLACE INTO diagram_index_state(doc_name,indexed_at) VALUES(?,?)",
            (doc_name, ingested_at or ""))
        total_docs += 1
        total_tok += len(tok_rows)
        total_pg += len(pg_rows)
        if verbose:
            print(f"  indexed {doc_name}: {len(pg_rows)} pages, {len(tok_rows)} tokens")

    con.commit()
    con.close()
    if verbose:
        print(f"[diagram_index] docs={total_docs} pages={total_pg} tokens={total_tok}")
    return {"docs": total_docs, "pages": total_pg, "tokens": total_tok}


# ── query helpers ────────────────────────────────────────────────────────
def is_connection_query(query: str) -> bool:
    low = (query or "").lower()
    return any(kw.lower() in low for kw in C.CONNECTION_INTENT_KEYWORDS)


def page_numbers_in_query(query: str):
    out = []
    for rx in C.PAGE_QUERY_PATTERNS:
        for m in rx.findall(query or ""):
            try:
                out.append(int(m))
            except (TypeError, ValueError):
                pass
    return out


def locate_by_page(query: str, doc_types=("circuit_diagram",)):
    """Exact page-number channel -> [(doc_name, doc_type, page)]."""
    pages = page_numbers_in_query(query)
    if not pages:
        return []
    con = _conn(); _ensure_tables(con)
    ph = ",".join("?" * len(pages))
    q = f"SELECT DISTINCT doc_name, doc_type, page FROM diagram_pages WHERE page IN ({ph})"
    params = list(pages)
    if doc_types:
        q += " AND doc_type IN (%s)" % ",".join("?" * len(doc_types))
        params += list(doc_types)
    rows = con.execute(q, params).fetchall()
    con.close()
    return rows


def lookup_identifiers(query: str, doc_types=("circuit_diagram",), limit: int = 8):
    """Inverted-index channel: identifiers in query -> ranked [(doc,type,page,hits)]."""
    toks = extract_identifiers(query)
    if not toks:
        return []
    con = _conn(); _ensure_tables(con)
    ph = ",".join("?" * len(toks))
    q = (f"SELECT doc_name, doc_type, page, COUNT(DISTINCT token) c "
         f"FROM diagram_tokens WHERE token IN ({ph})")
    params = list(toks)
    if doc_types:
        q += " AND doc_type IN (%s)" % ",".join("?" * len(doc_types))
        params += list(doc_types)
    q += " GROUP BY doc_name, doc_type, page ORDER BY c DESC, page ASC LIMIT ?"
    params.append(limit)
    rows = con.execute(q, params).fetchall()
    con.close()
    return rows


def get_page_tag(doc_name: str, page: int):
    con = _conn(); _ensure_tables(con)
    row = con.execute(
        "SELECT unit, kind FROM diagram_pages WHERE doc_name=? AND page=?",
        (doc_name, page)).fetchone()
    con.close()
    return (row[0], row[1]) if row else (None, None)


def get_page_text(doc_name: str, page: int) -> str:
    """Full (untruncated) text layer of a specific page. Generic: matches by
    parsed page number, not by assuming the exact section_title string."""
    con = _conn()
    rows = con.execute(
        "SELECT section_title, text FROM doc_fts WHERE doc_name=? "
        "AND section_title LIKE '%'||?||'%'", (doc_name, str(page))).fetchall()
    con.close()
    for section, text in rows:
        if page_from_section(section) == page:
            return text or ""
    return ""


def rank_for_connection(cands):
    """cands: list of dicts with doc_name/page/kind -> sorted by kind priority."""
    def key(c):
        return -C.CONNECTION_KIND_PRIORITY.get(c.get("kind"), 1)
    return sorted(cands, key=key)
