"""Versioned memory with declared sources and locally verifiable integrity.

Hashes detect inconsistent edits against the stored chain and head. They are not
an external trust anchor: an operator who rewrites the database and every hash
can construct another consistent history. Legacy databases are migrated in one
transaction, preserving their facts and recording the new integrity baseline.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
GENESIS_HASH = "0" * 64
SCHEMA_VERSION = "3"
_ID_OK = re.compile(r"^[a-zA-Z0-9_]{1,40}$")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_HASH_OK = re.compile(r"^[a-f0-9]{64}$")
_STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does for from had has
have how i in into is it its me my of on or our please so that the their them
then there these they this those to us was we were what when where which who
why will with would you your about remember note actually correction fix oops
au aux avec ce ces chez dans de des du elle elles en et eux je la le les leur
lui ma mais me mes moi mon ne nos notre nous ou par pas pour que quel quelle
quelles quels qui sa se ses son sur ta te tes toi ton tu un une vos votre vous
c est ca ceci cela concernant propos peux pouvez dire dis merci
""".split())

_CREATE_MEMORIES = """CREATE TABLE memories (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, current_version INTEGER NOT NULL,
    head_hash TEXT NOT NULL)"""
_CREATE_VERSIONS = """CREATE TABLE versions (
    memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL,
    content TEXT NOT NULL, source_refs TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL,
    PRIMARY KEY (memory_id, version))"""
_CREATE_MAP = """CREATE TABLE IF NOT EXISTS fts_map (
    fts_rowid INTEGER PRIMARY KEY, memory_id TEXT NOT NULL)"""
_CREATE_FTS = """CREATE VIRTUAL TABLE memory_fts USING fts5(
    title, content, tokenize='unicode61')"""
_CREATE_LEGACY = """CREATE TABLE IF NOT EXISTS legacy_receipts (
    memory_id TEXT NOT NULL, version INTEGER NOT NULL, hash_format TEXT NOT NULL,
    prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL,
    PRIMARY KEY(memory_id, version))"""
_CURRENT = """SELECT m.id AS memory_id, m.title, m.created_at AS memory_created_at,
    m.updated_at, m.current_version, m.head_hash, v.*
    FROM memories m JOIN versions v
    ON v.memory_id=m.id AND v.version=m.current_version"""


class VersionConflict(Exception):
    """The caller's expected version is no longer current."""


class NotFound(Exception):
    """The requested memory does not exist."""


class ChainBroken(Exception):
    """Existing storage is inconsistent; no automatic repair was performed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn").lower()


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be non-empty text")
    return value.strip()


def _refs(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("source_refs required: provide at least one source reference")
    return [_text(v, "each source reference") for v in values]


def _integer(value: int, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + " is outside its allowed range")
    return value


def _memory_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_OK.fullmatch(value):
        raise ValueError("invalid memory id")
    return value


def _match_expression(query: str) -> str | None:
    terms = dict.fromkeys(t for t in _TOKEN.findall(_fold(query))
                          if len(t) > 1 and t not in _STOPWORDS)
    # Tokens contain letters/digits only; the whole MATCH value is also bound.
    return " AND ".join('"' + t + '"*' for t in terms) or None


def _legacy_hash(prev: str, mid: str, row: sqlite3.Row) -> str:
    material = "|".join([prev, mid, str(row["version"]), row["content"],
                         json.dumps(json.loads(row["source_refs"])), row["created_at"]])
    return hashlib.sha256(material.encode()).hexdigest()


def _hash_entry(prev: str, mid: str, version: int, content: str,
                refs: list[str], created_at: str, title: str,
                memory_created_at: str) -> str:
    material = json.dumps({"format": SCHEMA_VERSION, "prev_hash": prev,
        "memory_id": mid, "version": version, "content": content,
        "source_refs": refs, "created_at": created_at, "title": title,
        "memory_created_at": memory_created_at}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class MemoryStore:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self._lock = threading.RLock()
        self.read_only = read_only
        if read_only:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                self._initialize()
            except BaseException:
                self.db.close()
                raise

    def close(self) -> None:
        with self._lock:
            self.db.close()

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise ValueError("this memory store is read-only")
        with self._lock:
            # Reserve the writer before reading a version, including across
            # separate MemoryStore connections to the same SQLite database.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _columns(self, table: str) -> set[str]:
        # These table names are fixed internal constants, never caller input.
        return {r["name"] for r in self.db.execute("PRAGMA table_info(" + table + ")")}

    def _initialize(self) -> None:
        with self._transaction():
            tables = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if ("memories" in tables) != ("versions" in tables):
                raise ChainBroken("incomplete storage schema; no automatic repair performed")
            fresh = "memories" not in tables
            migrated = False
            if fresh:
                self.db.execute(_CREATE_MEMORIES)
                self.db.execute(_CREATE_VERSIONS)
            else:
                mcols, vcols = self._columns("memories"), self._columns("versions")
                if not {"id", "title", "created_at", "updated_at", "current_version"} <= mcols:
                    raise ChainBroken("unsupported memories schema")
                if not {"memory_id", "version", "content", "source_refs", "status", "created_at"} <= vcols:
                    raise ChainBroken("unsupported versions schema")
                has_hashes = {"prev_hash", "entry_hash"} <= vcols
                if bool({"prev_hash", "entry_hash"} & vcols) != has_hashes:
                    raise ChainBroken("incomplete legacy hash schema")
                if "head_hash" not in mcols:
                    self._migrate_legacy(has_hashes)
                    migrated = True
                elif not has_hashes:
                    raise ChainBroken("head commitment exists without receipt hashes")
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if fresh or migrated:
                self.db.execute(_CREATE_LEGACY)
                self.db.execute("INSERT OR REPLACE INTO schema_meta VALUES ('hash_format', ?)", (SCHEMA_VERSION,))
                archive_count = self.db.execute("SELECT COUNT(*) FROM legacy_receipts").fetchone()[0]
                self.db.execute("INSERT OR REPLACE INTO schema_meta VALUES ('legacy_receipt_count', ?)", (str(archive_count),))
                if migrated:
                    self.db.execute("INSERT OR REPLACE INTO schema_meta VALUES ('legacy_baseline', ?)",
                        ("Legacy records preserved; integrity baseline established at " + _now(),))
            fmt = self.db.execute("SELECT value FROM schema_meta WHERE key='hash_format'").fetchone()
            if fmt is None or fmt[0] != SCHEMA_VERSION:
                raise ChainBroken("unsupported receipt format; no automatic repair performed")
            self.db.execute(_CREATE_MAP)
            fts = self.db.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts'").fetchone()
            contentless = fts is not None and "content=''" in fts[0].replace(" ", "").lower()
            if fresh or migrated or fts is None or contentless or "fts_map" not in tables:
                self.db.execute("DROP TABLE IF EXISTS memory_fts")
                self.db.execute(_CREATE_FTS)
                self.db.execute("DELETE FROM fts_map")
                for row in self.db.execute(_CURRENT).fetchall():
                    self._index(row["memory_id"], row["title"], row["content"])

    def _migrate_legacy(self, has_hashes: bool) -> None:
        # Check the old chain before replacing its format. A detected v2
        # mismatch must never be turned into a newly valid baseline.
        orphan = self.db.execute("SELECT v.memory_id FROM versions v LEFT JOIN memories m ON m.id=v.memory_id WHERE m.id IS NULL LIMIT 1").fetchone()
        if orphan:
            raise ChainBroken("legacy version has no parent memory")
        memories = self.db.execute("SELECT * FROM memories ORDER BY id").fetchall()
        histories = []
        for mem in memories:
            rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (mem["id"],)).fetchall()
            problem = self._structure_problem(mem, rows, check_head=False)
            if problem:
                raise ChainBroken("legacy memory " + mem["id"] + ": " + problem)
            prev = GENESIS_HASH
            for row in rows:
                if has_hashes and (row["prev_hash"] != prev or row["entry_hash"] != _legacy_hash(prev, mem["id"], row)):
                    raise ChainBroken("legacy hash mismatch at " + mem["id"] + " version " + str(row["version"]))
                if has_hashes:
                    prev = row["entry_hash"]
            histories.append((mem, rows))
        if not has_hashes:
            self.db.execute("ALTER TABLE versions ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE versions ADD COLUMN entry_hash TEXT NOT NULL DEFAULT ''")
        self.db.execute(_CREATE_LEGACY)
        if has_hashes:
            for mem, rows in histories:
                for row in rows:
                    self.db.execute("INSERT INTO legacy_receipts VALUES (?,?,?,?,?)",
                        (mem["id"], row["version"], "2", row["prev_hash"], row["entry_hash"]))
        self.db.execute("ALTER TABLE memories ADD COLUMN head_hash TEXT NOT NULL DEFAULT ''")
        for mem, rows in histories:
            prev = GENESIS_HASH
            for row in rows:
                digest = _hash_entry(prev, mem["id"], row["version"], row["content"],
                    json.loads(row["source_refs"]), row["created_at"], mem["title"], mem["created_at"])
                self.db.execute("UPDATE versions SET prev_hash=?,entry_hash=? WHERE memory_id=? AND version=?",
                                (prev, digest, mem["id"], row["version"]))
                prev = digest
            self.db.execute("UPDATE memories SET head_hash=? WHERE id=?", (prev, mem["id"]))

    @staticmethod
    def _structure_problem(mem, rows, *, check_head: bool = True) -> str | None:
        if not rows:
            return "memory has no versions"
        try:
            _text(mem["title"], "title")
            _integer(mem["current_version"], "current_version", 1)
            if [r["version"] for r in rows] != list(range(1, len(rows) + 1)):
                return "version sequence is not contiguous from version 1"
            if mem["current_version"] != rows[-1]["version"]:
                return "current_version does not identify the latest stored version"
            if mem["created_at"] != rows[0]["created_at"] or mem["updated_at"] != rows[-1]["created_at"]:
                return "memory timestamps disagree with version history"
            for i, row in enumerate(rows):
                _text(row["content"], "content")
                _refs(json.loads(row["source_refs"]))
                stamp = datetime.fromisoformat(row["created_at"])
                if stamp.tzinfo is None:
                    return "version timestamp has no timezone"
                expected_status = STATUS_CURRENT if i == len(rows) - 1 else STATUS_SUPERSEDED
                if row["status"] != expected_status:
                    return "version status disagrees with its position in history"
            if check_head and mem["head_hash"] != rows[-1]["entry_hash"]:
                return "stored head commitment disagrees with the last version"
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            return "invalid stored fields: " + str(exc)
        return None

    def _index(self, mem_id: str, title: str, content: str) -> None:
        cur = self.db.execute("INSERT INTO memory_fts(title,content) VALUES (?,?)", (_fold(title), _fold(content)))
        self.db.execute("INSERT INTO fts_map(fts_rowid,memory_id) VALUES (?,?)", (cur.lastrowid, mem_id))

    def _reindex(self, mem_id: str, title: str, content: str) -> None:
        rows = self.db.execute("SELECT fts_rowid FROM fts_map WHERE memory_id=?", (mem_id,)).fetchall()
        for row in rows:
            self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (row[0],))
        self.db.execute("DELETE FROM fts_map WHERE memory_id=?", (mem_id,))
        self._index(mem_id, title, content)

    def remember(self, title: str, content: str, source_refs: list[str]) -> dict:
        title, content, refs = _text(title, "title"), _text(content, "content"), _refs(source_refs)
        mem_id, now = "mem_" + secrets.token_hex(8), _now()
        digest = _hash_entry(GENESIS_HASH, mem_id, 1, content, refs, now, title, now)
        with self._transaction():
            self.db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?)", (mem_id, title, now, now, 1, digest))
            self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)", (mem_id, 1, content, json.dumps(refs), STATUS_CURRENT, now, GENESIS_HASH, digest))
            self._index(mem_id, title, content)
        return {"memory_id": mem_id, "version": 1, "status": "recorded", "provenance_kept": True,
                "recorded_at": now, "entry_hash": digest}

    @staticmethod
    def _view(row, relevance: float = 0.0) -> dict:
        return {"memory_id": row["memory_id"], "title": row["title"], "version": row["version"],
            "status": row["status"], "content": row["content"], "source_refs": json.loads(row["source_refs"]),
            "updated_at": row["created_at"], "relevance": relevance}

    def _with_ancestors(self, results: list[dict]) -> list[dict]:
        expanded = []
        for result in results:
            expanded.append(result)
            rows = self.db.execute("SELECT v.*,m.title FROM versions v JOIN memories m ON m.id=v.memory_id WHERE memory_id=? AND status=? ORDER BY version",
                                   (result["memory_id"], STATUS_SUPERSEDED)).fetchall()
            expanded.extend(self._view(row) for row in rows)
        return expanded

    def recall(self, query: str, limit: int = 5, include_superseded: bool = False) -> dict:
        query = _text(query, "query")
        _integer(limit, "limit", 1, 20)
        match = _match_expression(query)
        with self._lock:
            rows = [] if match is None else self.db.execute(
                "SELECT v.*,m.title FROM memory_fts f JOIN fts_map fm ON fm.fts_rowid=f.rowid "
                "JOIN memories m ON m.id=fm.memory_id JOIN versions v ON v.memory_id=m.id AND v.version=m.current_version "
                "WHERE memory_fts MATCH ? ORDER BY rank,m.created_at,m.id LIMIT ?", (match, limit)).fetchall()
            results = [self._view(row, round(1 / (1 + pos), 2)) for pos, row in enumerate(rows)]
            if include_superseded:
                results = self._with_ancestors(results)
        return {"query": query, "results": results, "count": len(results),
            "note": "includes superseded versions for matched current memories" if include_superseded
                    else "current knowledge only; meaningful search terms must all match"}

    def list_memories(self, limit: int = 20, offset: int = 0, include_superseded: bool = False) -> dict:
        """Enumerate memories without a keyword filter.

        Pagination counts memories, not ancestor rows added by audit mode.
        """
        _integer(limit, "limit", 1, 100)
        _integer(offset, "offset", 0)
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            rows = self.db.execute(_CURRENT + " ORDER BY m.created_at,m.id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            results = [self._view(row) for row in rows]
            if include_superseded:
                results = self._with_ancestors(results)
        next_offset = offset + len(rows)
        more = next_offset < total
        return {"results": results, "count": len(results), "total_memories": total,
            "offset": offset, "limit": limit, "has_more": more,
            "next_offset": next_offset if more else None}

    def correct(self, memory_id: str, expected_version: int, content: str,
                source_refs: list[str] | None = None) -> dict:
        memory_id = _memory_id(memory_id)
        _integer(expected_version, "expected_version", 1)
        content, refs = _text(content, "content"), _refs(source_refs)
        with self._transaction():
            mem = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if mem is None:
                raise NotFound("no memory " + memory_id)
            if mem["current_version"] != expected_version:
                raise VersionConflict("stale write: memory " + memory_id + " is at version " +
                    str(mem["current_version"]) + ", you expected " + str(expected_version) + "; re-read it and retry")
            integrity = self.verify(memory_id)
            if not integrity["ok"]:
                raise ChainBroken(integrity["note"])
            now, version = _now(), expected_version + 1
            digest = _hash_entry(mem["head_hash"], memory_id, version, content, refs, now,
                                 mem["title"], mem["created_at"])
            self.db.execute("UPDATE versions SET status=? WHERE memory_id=? AND version=?", (STATUS_SUPERSEDED, memory_id, expected_version))
            self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)", (memory_id, version, content, json.dumps(refs), STATUS_CURRENT, now, mem["head_hash"], digest))
            self.db.execute("UPDATE memories SET current_version=?,updated_at=?,head_hash=? WHERE id=?", (version, now, digest, memory_id))
            self._reindex(memory_id, mem["title"], content)
        return {"memory_id": memory_id, "version": version, "superseded_version": expected_version,
                "status": "corrected", "corrected_at": now, "entry_hash": digest}

    def history(self, memory_id: str) -> dict:
        memory_id = _memory_id(memory_id)
        with self._lock:
            mem = self.db.execute("SELECT title FROM memories WHERE id=?", (memory_id,)).fetchone()
            if mem is None:
                raise NotFound("no memory " + memory_id)
            rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (memory_id,)).fetchall()
            legacy = {r["version"]: {"hash_format": r["hash_format"],
                "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"]}
                for r in self.db.execute("SELECT * FROM legacy_receipts WHERE memory_id=?", (memory_id,))}
        return {"memory_id": memory_id, "title": mem["title"], "chain": [
            {"version": r["version"], "status": r["status"], "content": r["content"],
             "source_refs": json.loads(r["source_refs"]), "recorded_at": r["created_at"],
             "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"],
             "legacy_receipt": legacy.get(r["version"])} for r in rows]}

    def verify(self, memory_id: str | None = None) -> dict:
        """Check local receipt consistency, not authenticity against its operator."""
        def broken(note, mid=None, version=None, checked=0):
            return {"ok": False, "checked": checked, "broken_at": {"memory_id": mid, "version": version}, "note": note}
        with self._lock:
            try:
                if memory_id is not None:
                    memory_id = _memory_id(memory_id)
                if not {"head_hash"} <= self._columns("memories") or not {"prev_hash", "entry_hash"} <= self._columns("versions"):
                    return broken("legacy or unsupported schema: migrate with the normal application before verification", memory_id)
                fmt = self.db.execute("SELECT value FROM schema_meta WHERE key='hash_format'").fetchone()
                if fmt is None or fmt[0] != SCHEMA_VERSION:
                    return broken("unsupported receipt hash format", memory_id)
                archive_count = self.db.execute("SELECT value FROM schema_meta WHERE key='legacy_receipt_count'").fetchone()
                actual_count = self.db.execute("SELECT COUNT(*) FROM legacy_receipts").fetchone()[0]
                if archive_count is None or int(archive_count[0]) != actual_count:
                    return broken("legacy receipt archive disagrees with its stored count", memory_id)
                if memory_id is None:
                    orphan = self.db.execute("SELECT v.memory_id,v.version FROM versions v LEFT JOIN memories m ON m.id=v.memory_id WHERE m.id IS NULL LIMIT 1").fetchone()
                    if orphan:
                        return broken("version has no parent memory", orphan[0], orphan[1])
                    memories = self.db.execute("SELECT * FROM memories ORDER BY created_at,id").fetchall()
                else:
                    memories = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchall()
                    if not memories:
                        return broken("memory not found", memory_id)
                checked = 0
                for mem in memories:
                    rows = self.db.execute("SELECT * FROM versions WHERE memory_id=? ORDER BY version", (mem["id"],)).fetchall()
                    problem = self._structure_problem(mem, rows)
                    if problem:
                        return broken(problem, mem["id"], None, checked)
                    prev = GENESIS_HASH
                    for row in rows:
                        expected = _hash_entry(prev, mem["id"], row["version"], row["content"], json.loads(row["source_refs"]),
                            row["created_at"], mem["title"], mem["created_at"])
                        if row["prev_hash"] != prev or not _HASH_OK.fullmatch(row["entry_hash"]) or row["entry_hash"] != expected:
                            return broken("receipt hash or previous-hash link mismatch", mem["id"], row["version"], checked)
                        prev = expected
                        checked += 1
                    legacy_prev = GENESIS_HASH
                    by_version = {r["version"]: r for r in rows}
                    archived = self.db.execute("SELECT * FROM legacy_receipts WHERE memory_id=? ORDER BY version", (mem["id"],)).fetchall()
                    for pos, receipt in enumerate(archived, 1):
                        original = by_version.get(receipt["version"])
                        if (receipt["version"] != pos or receipt["hash_format"] != "2" or original is None
                                or receipt["prev_hash"] != legacy_prev
                                or receipt["entry_hash"] != _legacy_hash(legacy_prev, mem["id"], original)):
                            return broken("archived legacy receipt mismatch", mem["id"], receipt["version"], checked)
                        legacy_prev = receipt["entry_hash"]
                return {"ok": True, "checked": checked, "scope": "local chain, metadata and head consistency; no external trust anchor"}
            except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, IndexError) as exc:
                return broken("invalid storage: " + str(exc), memory_id)

    def stats(self) -> dict:
        with self._lock:
            return {"memories": self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                "versions": self.db.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
                "superseded": self.db.execute("SELECT COUNT(*) FROM versions WHERE status=?", (STATUS_SUPERSEDED,)).fetchone()[0]}


__all__ = ["MemoryStore", "VersionConflict", "NotFound", "ChainBroken",
           "GENESIS_HASH", "STATUS_CURRENT", "STATUS_SUPERSEDED"]
