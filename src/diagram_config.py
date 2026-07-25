"""Config-driven patterns for diagram indexing & retrieval.

ALL document-specific knowledge lives here (or is inferred at index time from the
document text) — never hardcoded into retrieval logic. To support a new machine /
new naming convention / new figure type, ADD entries here; do not touch the
retrieval or indexing code.

Rule of thumb for identifier regexes: prefer a miss over a false match
("宁漏不误连") — a wrong identifier is worse than a missing one.
"""
import re

# ─────────────────────────────────────────────────────────────────────────
# 1) Strong identifiers: net / wire / connector / mount / board numbers, etc.
#    Each matched substring becomes an inverted-index token (stored upper-cased).
#    Order matters only for labeling; a page can yield tokens from several rules.
# ─────────────────────────────────────────────────────────────────────────
IDENTIFIER_PATTERNS = [
    # net / wire numbers: 7214A2, 7214A2W-E, 7304A1
    ("net",         re.compile(r"(?<![A-Za-z0-9])\d{3,4}[A-Z]\d[A-Z]?(?:-[0-9A-Z]{1,4})?(?![A-Za-z0-9])")),
    # board / model numbers: 4S018-922
    ("board_model", re.compile(r"(?<![A-Za-z0-9])\d[A-Z]\d{3}-\d{3}(?![A-Za-z0-9])")),
    # multi-segment board / part names: IU-DRV1-X4P, RSDRVX4B (with hyphens)
    ("board_name",  re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}(?:-[A-Z0-9]{2,}){1,3}(?![A-Za-z0-9])")),
    # unit reference: A-712
    ("unit_ref",    re.compile(r"(?<![A-Za-z0-9])[A-Z]-\d{2,4}(?![A-Za-z0-9])")),
    # connector: CN12
    ("connector",   re.compile(r"(?<![A-Za-z0-9])CN\d{1,3}(?![A-Za-z0-9])")),
    # pin / sensor id: PS1, PS1A
    ("pin",         re.compile(r"(?<![A-Za-z0-9])PS\d{1,3}[A-Z]?(?![A-Za-z0-9])")),
]

# Tokens shorter than this (after strip) are dropped as too noisy.
MIN_TOKEN_LEN = 4

# ─────────────────────────────────────────────────────────────────────────
# 2) Exact-locate query intents: page number / figure number.
#    If a query matches, fetch that page/figure directly (bypass vector search).
#    section_title stored by ingest looks like "第N页"; PAGE_IN_SECTION parses it.
# ─────────────────────────────────────────────────────────────────────────
PAGE_QUERY_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*[页頁]"),
    re.compile(r"(?:page|p)\s*[.\-]?\s*(\d+)\b", re.I),
]
PAGE_IN_SECTION = re.compile(r"(\d+)")   # extract page int from section_title
FIGURE_NO_PATTERN = re.compile(r"\bNo\.?\s*(\d+)\b", re.I)

# ─────────────────────────────────────────────────────────────────────────
# 3) Figure-kind classification (inferred per page from its text/title).
#    keyword (lower-cased contains) -> canonical kind. Extensible.
#    First match wins in listed order.
# ─────────────────────────────────────────────────────────────────────────
FIGURE_KIND_KEYWORDS = [
    ("block",          ["ブロック図", "block diagram"]),
    ("general_wiring", ["総合配線図", "general wiring"]),
    ("wiring",         ["配線図", "wiring diagram"]),
    ("board_circuit",  ["基板回路図", "board circuit", "回路図 no", "circuit diagram no"]),
]

# ─────────────────────────────────────────────────────────────────────────
# 4) Connection / topology question routing.
#    If the query looks like "how does A connect to B / 接哪块板 / 走线",
#    re-rank candidate pages by figure kind (higher first). board_circuit is
#    deprioritized because it shows intra-board circuitry, not interconnects.
# ─────────────────────────────────────────────────────────────────────────
CONNECTION_INTENT_KEYWORDS = [
    "接", "連接", "连接", "接到", "接入", "接哪", "走线", "走線", "配線", "配线",
    "路由", "追线", "追線", "connect", "connects", "connection", "wiring",
    "goes to", "trace", "端到端", "どこ", "つなが", "接続",
]
CONNECTION_KIND_PRIORITY = {
    "block": 3,
    "general_wiring": 3,
    "wiring": 2,
    None: 1,
    "board_circuit": 0,
}

# ─────────────────────────────────────────────────────────────────────────
# 5) Unit / section label inference (open set — store whatever is found).
#    Used for disambiguation (e.g. filtering colliding short prefixes).
#    Returns the first pattern hit; genericity: add patterns, don't hardcode
#    the set of allowed unit values.
# ─────────────────────────────────────────────────────────────────────────
UNIT_PATTERNS = [
    re.compile(r"\bIU\s*No\.?\s*\d+\b", re.I),
    re.compile(r"\b[A-Z]{1,3}\s*No\.?\s*\d+\b"),
]
