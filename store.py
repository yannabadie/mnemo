"""Mnemo's memory store: versioned facts with provenance, on plain SQLite.

Design rules that matter for trust:
- Every memory carries source references and timestamps ("memory with receipts").
- Corrections never overwrite history: they add a version and supersede the old one.
- correct() uses optimistic concurrency (expected_version); a stale writer is
  refused instead of silently clobbering a newer correction.
- recall() returns current knowledge; superseded versions surface via history().

Every SQL statement below is fully parameterized; each query is a single
string literal so no concatenation is ever involved.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    current_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    memory_id   TEXT NOT NULL REFERENCES memories(id),
    version     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (memory_id, version)
);
CREATE TABLE IF NOT EXISTS fts_map (
    fts_rowid INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, content, content='', tokenize='unicode61'
);
"""

SQL_INSERT_MEMORY = "INSERT INTO memories(id,title,created_at,updated_at,current_version) VALUES (?,?,?,?,1)"
SQL_INSERT_VERSION = "INSERT INTO versions(memory_id,version,content,source_refs,status,created_at) VALUES (?,?,?,?,?,?)"
SQL_INSERT_FTS = "INSERT INTO memory_fts(title, content) VALUES (?, ?)"
SQL_INSERT_FTS_MAP = "INSERT INTO fts_map(fts_rowid, memory_id) VALUES (?, ?)"
SQL_RECALL = "SELECT m.id AS mid, m.title AS mtitle, v.version, v.content, v.source_refs, v.created_at FROM memory_fts f JOIN fts_map ON fts_map.fts_rowid = f.rowid JOIN memories m ON m.id = fts_map.memory_id JOIN versions v ON v.memory_id = m.id AND v.version = m.current_version WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?"
SQL_SUPERSEDED_OF = "SELECT version, content, source_refs, created_at FROM versions WHERE memory_id = ? AND status = ? ORDER BY version"
SQL_GET_VERSION = "SELECT current_version FROM memories WHERE id = ?"
SQL_MARK_SUPERSEDED = "UPDATE versions SET status = ? WHERE memory_id = ? AND version = ?"
SQL_BUMP_MEMORY = "UPDATE memories SET current_version = ?, updated_at = ? WHERE id = ?"
SQL_GET_TITLE = "SELECT title FROM memories WHERE id = ?"
SQL_HISTORY = "SELECT version, content, source_refs, status, created_at FROM versions WHERE memory_id = ? ORDER BY version"
SQL_COUNT_MEMORIES = "SELECT COUNT(*) AS c FROM memories"
SQL_COUNT_VERSIONS = "SELECT COUNT(*) AS c FROM versions"
SQL_COUNT_SUPERSEDED = "SELECT COUNT(*) AS c FROM versions WHERE status = ?"

_TERM_STRIP = re.compile(r"[^a-z0-9]+")
_ID_OK = re.compile(r"^[a-zA-Z0-9_]{1,40}$")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fold(text: str) -> str:
    """Lowercase + strip accents so French/English recall matches either way."""
    folded = unicodedata.normalize("NFD", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Mn").lower()


def _match_expression(query: str, operator: str = " AND ") -> str:
    """Build a safe FTS5 MATCH expression.

    Everything outside [a-z0-9] is stripped BEFORE quoting, so no FTS5 operator
    syntax can ever come from user input; the expression is then passed to
    SQLite as a bound parameter by the caller.
    """
    terms = [t for t in _TERM_STRIP.sub(" ", _fold(query)).split() if len(t) > 1]
    if not terms:
        raise ValueError("query has no usable terms")
    return operator.join('"' + t + '"*' for t in terms)


class VersionConflict(Exception):
    """Someone else corrected this memory since the caller last read it."""


class NotFound(Exception):
    """Unknown memory id."""


@dataclass
class MemoryView:
    id: str
    title: str
    version: int
    status: str
    content: str
    source_refs: list
    created_at: str
    updated_at: str


class MemoryStore:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _index(self, mem_id: str, title: str, content: str) -> None:
        cur = self.db.execute(SQL_INSERT_FTS, (_fold(title), _fold(content)))
        self.db.execute(SQL_INSERT_FTS_MAP, (cur.lastrowid, mem_id))

    def remember(self, title: str, content: str, source_refs: list[str]) -> dict:
        if not title.strip() or not content.strip():
            raise ValueError("title and content must be non-empty")
        if not source_refs:
            raise ValueError("source_refs required: memory without provenance is a rumor")
        mem_id = "mem_" + secrets.token_hex(4)
        now = _now()
        with self.db:
            self.db.execute(SQL_INSERT_MEMORY, (mem_id, title.strip(), now, now))
            self.db.execute(SQL_INSERT_VERSION,
                            (mem_id, 1, content.strip(), json.dumps(source_refs),
                             STATUS_CURRENT, now))
            self._index(mem_id, title, content)
        return {"memory_id": mem_id, "version": 1, "status": "recorded",
                "provenance_kept": True, "recorded_at": now}

    def recall(self, query: str, limit: int = 5, include_superseded: bool = False) -> dict:
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        # strict AND first; conversational queries carry stopwords
        # ("what about my lease"), so fall back to OR when AND finds nothing
        for op in (" AND ", " OR "):
            match = _match_expression(query, operator=op)
            rows = self.db.execute(SQL_RECALL, (match, limit)).fetchall()
            if rows:
                break
        results = [{
            "memory_id": row["mid"], "title": row["mtitle"],
            "version": row["version"], "status": STATUS_CURRENT,
            "content": row["content"],
            "source_refs": json.loads(row["source_refs"]),
            "updated_at": row["created_at"],
            "relevance": round(1.0 / (1.0 + pos), 2),
        } for pos, row in enumerate(rows)]
        if include_superseded:
            for r in list(results):
                sup = self.db.execute(SQL_SUPERSEDED_OF,
                                      (r["memory_id"], STATUS_SUPERSEDED)).fetchall()
                for s in sup:
                    results.append({
                        "memory_id": r["memory_id"], "title": r["title"],
                        "version": s["version"], "status": STATUS_SUPERSEDED,
                        "content": s["content"],
                        "source_refs": json.loads(s["source_refs"]),
                        "updated_at": s["created_at"], "relevance": 0.0})
        return {"query": query, "results": results, "count": len(results),
                "note": "includes superseded versions for audit" if include_superseded
                        else "current knowledge only; superseded versions live in history()"}

    def correct(self, memory_id: str, expected_version: int,
                content: str, source_refs: list[str] | None = None) -> dict:
        if not _ID_OK.match(memory_id):
            raise ValueError("invalid memory id")
        mem = self.db.execute(SQL_GET_VERSION, (memory_id,)).fetchone()
        if mem is None:
            raise NotFound("no memory " + memory_id)
        if mem["current_version"] != expected_version:
            raise VersionConflict(
                "stale write: memory " + memory_id + " is at version "
                + str(mem["current_version"]) + ", you expected "
                + str(expected_version) + "; re-read it and retry")
        now = _now()
        new_version = expected_version + 1
        with self.db:
            self.db.execute(SQL_MARK_SUPERSEDED,
                            (STATUS_SUPERSEDED, memory_id, expected_version))
            self.db.execute(SQL_INSERT_VERSION,
                            (memory_id, new_version, content.strip(),
                             json.dumps(source_refs if source_refs else ["correction:unspecified"]),
                             STATUS_CURRENT, now))
            self.db.execute(SQL_BUMP_MEMORY, (new_version, now, memory_id))
        return {"memory_id": memory_id, "version": new_version,
                "superseded_version": expected_version, "status": "corrected",
                "corrected_at": now}

    def history(self, memory_id: str) -> dict:
        if not _ID_OK.match(memory_id):
            raise ValueError("invalid memory id")
        mem = self.db.execute(SQL_GET_TITLE, (memory_id,)).fetchone()
        if mem is None:
            raise NotFound("no memory " + memory_id)
        rows = self.db.execute(SQL_HISTORY, (memory_id,)).fetchall()
        return {"memory_id": memory_id, "title": mem["title"],
                "chain": [{
                    "version": r["version"], "status": r["status"],
                    "content": r["content"], "source_refs": json.loads(r["source_refs"]),
                    "recorded_at": r["created_at"],
                } for r in rows]}

    def stats(self) -> dict:
        n_mem = self.db.execute(SQL_COUNT_MEMORIES).fetchone()["c"]
        n_ver = self.db.execute(SQL_COUNT_VERSIONS).fetchone()["c"]
        n_sup = self.db.execute(SQL_COUNT_SUPERSEDED, (STATUS_SUPERSEDED,)).fetchone()["c"]
        return {"memories": n_mem, "versions": n_ver, "superseded": n_sup}
