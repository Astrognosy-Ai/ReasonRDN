"""rdn.notes - saved-note storage helpers shared by the client and the local web service.

Everything here works on one SQLite database file and never touches the network.

What lives here:

- The table layout for saved notes, plus small upgrades for older databases
  (two status columns and a full-text search index).
- Search over every saved note. When SQLite has the FTS5 full-text module with
  its ``trigram`` tokenizer, a search index finds the candidate notes quickly.
  Without it, every note is read and checked instead. Either way the whole
  store is searched; there is no cap on how far back a search looks.
- Date filters (``since`` and ``until``, written as ``YYYY-MM-DD``).
- Forgetting a note (hiding it, or deleting it for good) and updating a note
  (saving a new version that replaces the old one in searches).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("rdn.notes")

#: Name of the full-text search table that mirrors ``warf_artifacts``.
SEARCH_TABLE = "rdn_note_search"
#: Marker stored in ``rdn_schema_meta`` once the search table is filled.
SEARCH_INDEX_VERSION = "trigram-v1"
#: Shortest note id prefix accepted by forget and update.
MIN_ID_PREFIX = 6

_SEARCH_TRIGGERS = (
    "rdn_note_search_before_insert",
    "rdn_note_search_after_insert",
    "rdn_note_search_after_delete",
    "rdn_note_search_after_update",
)
_BODY_SQL = "{p}.address || ' ' || {p}.domain || ' ' || {p}.deposited_at || ' ' || {p}.metadata_json"


class NoteLookupError(LookupError):
    """No saved note matches, or a short id matches more than one note."""


# ---------------------------------------------------------------------------
# Query tokens and ranking
# ---------------------------------------------------------------------------

#: English stopwords and question words removed before matching.
RECALL_STOPWORDS: frozenset = frozenset({
    "what", "who", "how", "why", "when", "where", "which",
    "does", "do", "did", "is", "are", "should", "could", "would", "can",
    "i", "me", "my", "the", "a", "an", "of", "for", "to", "in", "on",
    "and", "or",
})


def tokenize_query(query: str) -> List[str]:
    """Return meaningful lowercase search words from *query*.

    - Split on anything that is not a letter, digit, ``-``, or ``.`` so that
      ``ISSUE-025`` and ``0.6.1`` stay whole.
    - Also keep each part of such words, so ``ISSUE-025`` also gives
      ``issue`` and ``025``.
    - Drop leading and trailing ``-`` and ``.``, and common stopwords.
    - Return each word once, in first-seen order.
    """
    raw_parts = re.split(r"[^\w.\-]+", query.lower())
    tokens: List[str] = []
    seen: set = set()

    def _add(tok: str) -> None:
        tok = tok.strip(".-")
        if tok and tok not in RECALL_STOPWORDS and tok not in seen:
            tokens.append(tok)
            seen.add(tok)

    for part in raw_parts:
        _add(part)
        if "-" in part or "." in part:
            for sub in re.split(r"[.-]", part):
                _add(sub)
    return tokens


def idf_score(tokens: List[str], all_haystacks: List[str], n_docs: int) -> Dict[str, float]:
    """Return ``log(n_docs / df)`` per token, where df counts notes containing it."""
    weights: Dict[str, float] = {}
    for tok in tokens:
        df = sum(1 for h in all_haystacks if tok in h) or 1
        weights[tok] = math.log(max(n_docs, 1) / df)
    return weights


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DayInput = Union[None, str, date, datetime]


def parse_day(value: DayInput, name: str = "date") -> Optional[date]:
    """Turn ``YYYY-MM-DD`` (or a ``date``) into a ``date``; raise ``ValueError`` otherwise."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not _DAY_RE.match(text):
        raise ValueError(
            f"{name} must be a date written as YYYY-MM-DD, for example 2026-10-21 (got {value!r})"
        )
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} is not a real calendar date: {value!r}") from None


def day_bounds(since: DayInput = None, until: DayInput = None) -> Tuple[Optional[str], Optional[str]]:
    """Return UTC text bounds ``(low, high)`` for comparing saved timestamps.

    Days are calendar days in this computer's time zone and both ends are
    included: a note saved at any time on the ``until`` day is kept.
    ``low`` is inclusive and ``high`` is exclusive.
    """
    since_day = parse_day(since, "since")
    until_day = parse_day(until, "until")
    if since_day and until_day and since_day > until_day:
        raise ValueError(
            f"since ({since_day.isoformat()}) is after until ({until_day.isoformat()})"
        )

    def _utc_start(day: date) -> str:
        local_midnight = datetime(day.year, day.month, day.day).astimezone()
        return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    low = _utc_start(since_day) if since_day else None
    high = _utc_start(until_day + timedelta(days=1)) if until_day else None
    return low, high


def _normalize_stamp(stamp: str) -> str:
    """Return a timestamp as comparable UTC text (``YYYY-MM-DDTHH:MM:SS...``)."""
    text = (stamp or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def stamp_in_range(stamp: str, low: Optional[str], high: Optional[str]) -> bool:
    """True when a saved timestamp falls inside ``day_bounds`` output."""
    if low is None and high is None:
        return True
    value = _normalize_stamp(stamp)
    if low is not None and value < low:
        return False
    if high is not None and value >= high:
        return False
    return True


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the saved-note tables. Safe to run on every start."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS warf_artifacts (
            artifact_id TEXT PRIMARY KEY,
            address TEXT NOT NULL UNIQUE,
            domain TEXT NOT NULL,
            category TEXT NOT NULL,
            task TEXT NOT NULL,
            deposited_at TEXT NOT NULL,
            audit_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_warf_domain_time ON warf_artifacts(domain, deposited_at DESC)"
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(warf_artifacts)")}
    # 0.6.3: a forgotten note keeps its row (unless purged) but is hidden.
    if "forgotten_at" not in columns:
        conn.execute("ALTER TABLE warf_artifacts ADD COLUMN forgotten_at TEXT")
    # 0.6.3: an updated note points at the address of its newer version.
    if "superseded_by" not in columns:
        conn.execute("ALTER TABLE warf_artifacts ADD COLUMN superseded_by TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_warf_time ON warf_artifacts(deposited_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rdn_schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    _ensure_search_index(conn)


def _drop_search_triggers(conn: sqlite3.Connection) -> None:
    for name in _SEARCH_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _search_table_usable(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(f"SELECT rowid FROM {SEARCH_TABLE} LIMIT 0").fetchall()
        return True
    except sqlite3.Error:
        return False


def _ensure_search_index(conn: sqlite3.Connection) -> None:
    """Build the full-text index when SQLite supports it, and keep it filled.

    The index is kept in step with ``warf_artifacts`` by triggers, so writes
    from any version of ReasonRDN (or other programs) are indexed. When the
    database is opened by a SQLite without FTS5, the triggers are removed so
    saving still works, and the index is rebuilt the next time FTS5 is there.
    """
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {SEARCH_TABLE} USING fts5(body, tokenize='trigram')"
        )
    except sqlite3.Error as exc:
        logger.debug("Full-text search is not available, reading every note instead: %s", exc)
        try:
            conn.rollback()
            _drop_search_triggers(conn)
            conn.execute("DELETE FROM rdn_schema_meta WHERE key = 'note_search'")
            conn.commit()
        except sqlite3.Error:
            pass
        return
    if not _search_table_usable(conn):
        try:
            _drop_search_triggers(conn)
            conn.execute("DELETE FROM rdn_schema_meta WHERE key = 'note_search'")
            conn.commit()
        except sqlite3.Error:
            pass
        return

    row = conn.execute("SELECT value FROM rdn_schema_meta WHERE key = 'note_search'").fetchone()
    trigger_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?, ?, ?)",
        _SEARCH_TRIGGERS,
    ).fetchone()[0]
    if row and row[0] == SEARCH_INDEX_VERSION and trigger_count == len(_SEARCH_TRIGGERS):
        return

    new_body = _BODY_SQL.format(p="new")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Another process may have built the index while this one waited.
        row = conn.execute("SELECT value FROM rdn_schema_meta WHERE key = 'note_search'").fetchone()
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?, ?, ?)",
            _SEARCH_TRIGGERS,
        ).fetchone()[0]
        if row and row[0] == SEARCH_INDEX_VERSION and trigger_count == len(_SEARCH_TRIGGERS):
            conn.commit()
            return
        _drop_search_triggers(conn)
        # BEFORE INSERT also covers INSERT OR REPLACE, which removes the old
        # row without firing delete triggers.
        conn.execute(
            f"""
            CREATE TRIGGER rdn_note_search_before_insert BEFORE INSERT ON warf_artifacts BEGIN
                DELETE FROM {SEARCH_TABLE} WHERE rowid IN (
                    SELECT rowid FROM warf_artifacts
                    WHERE address = new.address OR artifact_id = new.artifact_id
                );
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER rdn_note_search_after_insert AFTER INSERT ON warf_artifacts BEGIN
                DELETE FROM {SEARCH_TABLE} WHERE rowid = new.rowid;
                INSERT INTO {SEARCH_TABLE}(rowid, body) VALUES (new.rowid, {new_body});
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER rdn_note_search_after_delete AFTER DELETE ON warf_artifacts BEGIN
                DELETE FROM {SEARCH_TABLE} WHERE rowid = old.rowid;
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER rdn_note_search_after_update
            AFTER UPDATE OF address, domain, deposited_at, metadata_json ON warf_artifacts BEGIN
                DELETE FROM {SEARCH_TABLE} WHERE rowid = old.rowid;
                INSERT INTO {SEARCH_TABLE}(rowid, body) VALUES (new.rowid, {new_body});
            END
            """
        )
        # Fill the index from every existing note (also repairs a stale index).
        conn.execute(f"DELETE FROM {SEARCH_TABLE}")
        conn.execute(
            f"INSERT INTO {SEARCH_TABLE}(rowid, body) SELECT a.rowid, {_BODY_SQL.format(p='a')} "
            "FROM warf_artifacts a"
        )
        conn.execute(
            "INSERT OR REPLACE INTO rdn_schema_meta(key, value) VALUES ('note_search', ?)",
            (SEARCH_INDEX_VERSION,),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.warning("Could not build the note search index; searching every note instead: %s", exc)


def search_index_ready(conn: sqlite3.Connection) -> bool:
    """True when the full-text index exists, is usable, and is filled."""
    try:
        row = conn.execute("SELECT value FROM rdn_schema_meta WHERE key = 'note_search'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == SEARCH_INDEX_VERSION and _search_table_usable(conn))


# ---------------------------------------------------------------------------
# Reading notes
# ---------------------------------------------------------------------------


def _filters(
    project: Optional[str],
    low: Optional[str],
    high: Optional[str],
    include_hidden: bool,
    prefix: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if project:
        clauses.append("a.domain = ?")
        params.append(project)
    if not include_hidden:
        clauses.append("a.forgotten_at IS NULL AND a.superseded_by IS NULL")
    if low is not None:
        clauses.append("a.deposited_at >= ?")
        params.append(low)
    if high is not None:
        clauses.append("a.deposited_at < ?")
        params.append(high)
    if prefix:
        clauses.append("a.address GLOB ?")
        params.append(_glob_escape(prefix) + "*")
    return (" AND ".join(clauses) or "1=1"), params


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _fts_phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def note_state(row: Any) -> str:
    """Return ``visible``, ``forgotten``, or ``superseded`` for a stored row."""
    if row["forgotten_at"]:
        return "forgotten"
    if row["superseded_by"]:
        return "superseded"
    return "visible"


def row_to_result(row: Any, meta: Dict[str, Any], source: str) -> Dict[str, Any]:
    result = {
        "id": row["artifact_id"],
        "address": row["address"],
        "project": row["domain"],
        "deposited_at": row["deposited_at"],
        "content": meta.get("content", ""),
        "tags": meta.get("tags", []),
        "meta": meta,
        "source": source,
    }
    state = note_state(row)
    if state != "visible":
        result["state"] = state
        if row["superseded_by"]:
            result["superseded_by"] = row["superseded_by"]
    return result


_ROW_COLUMNS = (
    "a.rowid AS rowid, a.artifact_id, a.address, a.domain, a.deposited_at, "
    "a.metadata_json, a.forgotten_at, a.superseded_by"
)


def search_notes(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    *,
    tags: Optional[Sequence[str]] = None,
    project: Optional[str] = None,
    limit: int = 20,
    since: DayInput = None,
    until: DayInput = None,
    include_hidden: bool = False,
    extra: Optional[Iterable[Tuple[Dict[str, Any], Dict[str, Any], str, Dict[str, Any]]]] = None,
    source: str = "local",
) -> List[Dict[str, Any]]:
    """Search every saved note and return the best matches, newest first on ties.

    ``extra`` adds other searchable items (local contributions) as
    ``(row, meta, haystack, result)`` tuples that the caller already filtered.
    Raises ``ValueError`` for a badly written date.
    """
    low, high = day_bounds(since, until)
    raw_query = (query or "").strip()
    needle = raw_query.lower()
    tokens = tokenize_query(raw_query) if raw_query else []
    use_token_search = bool(tokens)
    tags = list(tags) if tags else None
    where, params = _filters(project, low, high, include_hidden)
    conn.row_factory = sqlite3.Row

    # Words the index can look up: every token for word search, or the whole
    # query text when it only has stopwords. Trigram lookups need 3+ letters.
    lookup_terms = tokens if use_token_search else ([needle] if needle else [])
    use_index = (
        bool(lookup_terms)
        and all(len(term) >= 3 for term in lookup_terms)
        and search_index_ready(conn)
    )

    if not raw_query and not tags and not extra:
        rows = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE {where} "
            "ORDER BY a.deposited_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [row_to_result(r, _load_meta(r), source) for r in rows]

    if use_index:
        match = " OR ".join(_fts_phrase(t) for t in lookup_terms)
        rows = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE {where} AND a.rowid IN "
            f"(SELECT rowid FROM {SEARCH_TABLE} WHERE {SEARCH_TABLE} MATCH ?) "
            "ORDER BY a.deposited_at DESC",
            params + [match],
        ).fetchall()
        n_docs = conn.execute(
            f"SELECT COUNT(*) FROM warf_artifacts a WHERE {where}", params
        ).fetchone()[0]
        doc_freq: Dict[str, int] = {}
        if use_token_search:
            for tok in tokens:
                doc_freq[tok] = conn.execute(
                    f"SELECT COUNT(*) FROM warf_artifacts a WHERE {where} AND a.rowid IN "
                    f"(SELECT rowid FROM {SEARCH_TABLE} WHERE {SEARCH_TABLE} MATCH ?)",
                    params + [_fts_phrase(tok)],
                ).fetchone()[0]
    else:
        rows = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE {where} "
            "ORDER BY a.deposited_at DESC",
            params,
        ).fetchall()
        n_docs = len(rows)
        doc_freq = {}

    parsed: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
    for row in rows:
        meta = _load_meta(row)
        if meta is None:
            continue
        haystack = " ".join(
            [row["address"] or "", row["domain"] or "", row["deposited_at"] or "", row["metadata_json"] or ""]
        ).lower()
        parsed.append((meta, haystack, row_to_result(row, meta, source)))

    extra_items = list(extra or [])
    for _row, meta, haystack, result in extra_items:
        parsed.append((meta, haystack, result))
    n_docs += len(extra_items)

    if use_token_search:
        if use_index:
            weights: Dict[str, float] = {}
            for tok in tokens:
                df = doc_freq.get(tok, 0) + sum(1 for _r, _m, h, _x in extra_items if tok in h)
                weights[tok] = math.log(max(n_docs, 1) / (df or 1))
        else:
            weights = idf_score(tokens, [h for _m, h, _x in parsed], n_docs or 1)
    else:
        weights = {}

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for meta, haystack, result in parsed:
        if use_token_search:
            matched = [tok for tok in tokens if tok in haystack]
            if not matched:
                continue
            score = sum(weights[tok] for tok in matched)
            if needle and needle in haystack:
                score += 5.0
            tags_str = " ".join(meta.get("tags", []) or []).lower()
            score += 0.5 * sum(1 for tok in matched if tok in tags_str)
        else:
            if needle and needle not in haystack:
                continue
            score = 0.0
        if tags:
            entry_tags = meta.get("tags", []) or []
            if not any(t in entry_tags for t in tags) and not any(
                t in (result.get("address") or "") for t in tags
            ):
                continue
        scored.append((score, result))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in scored[:limit]]


def list_notes(
    conn: sqlite3.Connection,
    prefix: str,
    *,
    limit: int = 50,
    since: DayInput = None,
    until: DayInput = None,
    include_hidden: bool = False,
    source: str = "local",
) -> List[Dict[str, Any]]:
    """Return saved notes whose address starts with ``prefix``, newest first."""
    low, high = day_bounds(since, until)
    where, params = _filters(None, low, high, include_hidden, prefix=prefix)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE {where} "
        "ORDER BY a.deposited_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    results = []
    for row in rows:
        meta = _load_meta(row) or {}
        results.append(row_to_result(row, meta, source))
    return results


def visible_address_exists(conn: sqlite3.Connection, address: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM warf_artifacts WHERE address = ? AND forgotten_at IS NULL",
        (address,),
    ).fetchone()
    return row is not None


def _load_meta(row: Any) -> Optional[Dict[str, Any]]:
    try:
        meta = json.loads(row["metadata_json"])
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


# ---------------------------------------------------------------------------
# Forget and update
# ---------------------------------------------------------------------------


def find_note(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Find one saved note by its reason:// address, its id, or a unique id start.

    Hidden notes are found too, so they can be purged.
    """
    conn.row_factory = sqlite3.Row
    text = (ref or "").strip()
    if not text:
        raise NoteLookupError("Give a note id or a reason:// address")
    if text.startswith("reason://"):
        row = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE a.address = ?", (text,)
        ).fetchone()
        if row is None:
            raise NoteLookupError(
                f"No saved note at {text}. (Forget and update work on notes saved with remember.)"
            )
        return row
    key = text.lower()
    if not re.fullmatch(r"[0-9a-f]+", key):
        raise NoteLookupError(
            f"{text!r} is not a note id. Use the id or address that remember and recall print."
        )
    if len(key) < MIN_ID_PREFIX:
        raise NoteLookupError(
            f"Note id {text!r} is too short. Use at least {MIN_ID_PREFIX} characters."
        )
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM warf_artifacts a WHERE a.artifact_id GLOB ? LIMIT 2",
        (key + "*",),
    ).fetchall()
    if not rows:
        raise NoteLookupError(f"No saved note with id {text}")
    if len(rows) > 1:
        raise NoteLookupError(
            f"Note id {text} matches more than one note. Use more characters."
        )
    return rows[0]


def forget_note(conn: sqlite3.Connection, ref: str, *, purge: bool = False) -> Dict[str, Any]:
    """Hide a saved note from recall and list, or with ``purge`` delete it for good."""
    row = find_note(conn, ref)
    result = {"id": row["artifact_id"], "address": row["address"], "purged": bool(purge)}
    if purge:
        conn.execute("PRAGMA secure_delete = ON")
        with conn:
            conn.execute("DELETE FROM warf_artifacts WHERE artifact_id = ?", (row["artifact_id"],))
        try:
            # Rewrite the search index so the deleted words are not kept in it.
            with conn:
                conn.execute(f"INSERT INTO {SEARCH_TABLE}({SEARCH_TABLE}) VALUES ('optimize')")
        except sqlite3.Error:
            pass
        result["status"] = "purged"
        return result
    if not row["forgotten_at"]:
        with conn:
            conn.execute(
                "UPDATE warf_artifacts SET forgotten_at = ? WHERE artifact_id = ?",
                (datetime.now(timezone.utc).isoformat(), row["artifact_id"]),
            )
    result["status"] = "forgotten"
    return result


def build_note(
    content: str,
    *,
    tags: Optional[Iterable[str]] = None,
    project: str = "astrognosy",
    meta: Optional[Dict[str, Any]] = None,
    reason_address: Optional[str] = None,
) -> Tuple[str, str, str, str, str, Dict[str, Any]]:
    """Return ``(artifact_id, address, project, deposited_at, audit_hash, metadata)``.

    Same record layout as ``RDNClient.remember``.
    """
    from .addressing import project_address

    content = (content or "").strip()
    project = (project or "unknown").strip()
    address = reason_address or project_address(project, content)
    metadata: Dict[str, Any] = {
        "content": content,
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "project": project,
        "reason_address": address,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    if meta:
        metadata.update(meta)
    audit_hash = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    artifact_id = hashlib.sha1(f"{address}:{audit_hash}".encode("utf-8")).hexdigest()
    return artifact_id, address, project, metadata["stored_at"], audit_hash, metadata


def insert_note(conn: sqlite3.Connection, record: Tuple[str, str, str, str, str, Dict[str, Any]]) -> None:
    artifact_id, address, project, deposited_at, audit_hash, metadata = record
    conn.execute(
        """
        INSERT OR REPLACE INTO warf_artifacts
        (artifact_id, address, domain, category, task, deposited_at, audit_hash, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            address,
            project,
            "handoff",
            address.rsplit("/", 1)[-1],
            deposited_at,
            audit_hash,
            json.dumps(metadata, ensure_ascii=True),
        ),
    )


#: Metadata keys that belong to one stored version and are not copied forward.
_PER_VERSION_KEYS = frozenset({"content", "tags", "project", "reason_address", "stored_at", "supersedes"})


def update_note(
    conn: sqlite3.Connection,
    ref: str,
    content: str,
    *,
    tags: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Save ``content`` as a new version of a note and hide the old version.

    The old version stays in the database (it can still be purged) and the new
    one records which address it replaces in ``meta.supersedes``.
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("The new text for the note is empty")
    old = find_note(conn, ref)
    if old["forgotten_at"]:
        raise NoteLookupError(f"Note {old['artifact_id']} was forgotten; save it again with remember")
    if old["superseded_by"]:
        raise NoteLookupError(
            f"Note {old['artifact_id']} was already replaced by {old['superseded_by']}; update that one"
        )
    old_meta = _load_meta(old) or {}
    if (old_meta.get("content") or "").strip() == content and tags is None:
        return {
            "status": "unchanged",
            "id": old["artifact_id"],
            "address": old["address"],
        }
    carried = {k: v for k, v in old_meta.items() if k not in _PER_VERSION_KEYS}
    carried["supersedes"] = old["address"]
    record = build_note(
        content,
        tags=list(tags) if tags is not None else old_meta.get("tags") or [],
        project=old["domain"],
        meta=carried,
    )
    new_address = record[1]
    if new_address == old["address"]:
        # Same text with new tags: replace the row in place.
        record = build_note(
            content,
            tags=list(tags) if tags is not None else old_meta.get("tags") or [],
            project=old["domain"],
            meta={k: v for k, v in carried.items() if k != "supersedes"},
            reason_address=old["address"],
        )
        with conn:
            insert_note(conn, record)
        return {"status": "updated", "id": record[0], "address": record[1], "replaces": None}
    with conn:
        insert_note(conn, record)
        conn.execute(
            "UPDATE warf_artifacts SET superseded_by = ? WHERE artifact_id = ?",
            (new_address, old["artifact_id"]),
        )
    return {
        "status": "updated",
        "id": record[0],
        "address": new_address,
        "project": record[2],
        "replaces": old["address"],
        "replaced_id": old["artifact_id"],
    }
