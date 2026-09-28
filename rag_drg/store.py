"""SQLite-backed chunk store with an FTS5 keyword index and optional vectors.

A single file holds everything, so the index can live on a shared
filesystem (e.g. the group's HPC home/project space) and be opened
read-only by many agents at once.

Concurrency: writable connections switch the database to WAL mode, so readers (the MCP
server answering searches) are not blocked while `rag-drg ingest` writes, and every
connection waits up to BUSY_TIMEOUT seconds for a lock instead of failing at once. WAL needs
shared memory between the processes, i.e. all of them on one host. For an index on a
network filesystem written from several hosts, set RAG_DRG_SQLITE_JOURNAL=delete (the
classic rollback journal; readers then wait for the writer instead).
Read-only connections need to create the `-shm` file next to a WAL database; if the index
directory is not writable for the reader and no writer is running, they fall back to
opening the file as immutable (a snapshot without locking).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .chunking import Chunk

log = logging.getLogger(__name__)

BUSY_TIMEOUT = 30.0  # seconds to wait for a lock held by another connection (e.g. a running ingest)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    chunk_key   TEXT UNIQUE NOT NULL,
    source      TEXT NOT NULL,
    path        TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    title       TEXT,
    text        TEXT NOT NULL,
    domain      TEXT,
    software    TEXT,
    version     TEXT,
    doc_type    TEXT,
    tags        TEXT,
    url         TEXT,
    status      TEXT,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(source, path, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_filter ON chunks(domain, software);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, text, tags,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, text, tags) VALUES (new.id, new.title, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, text, tags) VALUES ('delete', old.id, old.title, old.text, old.tags);
    DELETE FROM embeddings WHERE chunk_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, text, tags) VALUES ('delete', old.id, old.title, old.text, old.tags);
    INSERT INTO chunks_fts(rowid, title, text, tags) VALUES (new.id, new.title, new.text, new.tags);
    DELETE FROM embeddings WHERE chunk_id = old.id;
END;

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY,
    vec      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class StoredChunk:
    id: int
    source: str
    path: str
    ordinal: int
    title: str
    text: str
    domain: str | None
    software: str | None
    version: str | None
    doc_type: str | None
    tags: list[str]
    url: str | None
    status: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StoredChunk":
        return cls(
            id=row["id"], source=row["source"], path=row["path"], ordinal=row["ordinal"],
            title=row["title"] or "", text=row["text"], domain=row["domain"],
            software=row["software"], version=row["version"], doc_type=row["doc_type"],
            tags=[t for t in (row["tags"] or "").split(" ") if t], url=row["url"],
            status=row["status"],
        )

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(f"Index not found at {self.path}. Run `rag-drg ingest` first.")
            self.conn = self._connect_readonly()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT, check_same_thread=False)
            self.conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
            journal = os.environ.get("RAG_DRG_SQLITE_JOURNAL", "wal").strip().lower() or "wal"
            if journal not in ("wal", "delete", "truncate", "persist"):
                raise ValueError(f"RAG_DRG_SQLITE_JOURNAL must be wal or delete, got {journal!r}")
            self.conn.execute(f"PRAGMA journal_mode={journal}")
            if not self._has_schema():  # skip the DDL when present: it would need the write lock
                self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row
        self._vec_cache: tuple[np.ndarray, np.ndarray] | None = None

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn
        except sqlite3.OperationalError as e:
            # A WAL database whose -shm file does not exist and cannot be created here (read-only
            # directory, no writer running): read it as an immutable snapshot instead.
            if _is_lock_error(e):
                raise
            conn.close()
            log.warning("index %s: %s; opening it as an immutable snapshot", self.path, e)
            conn = sqlite3.connect(f"{uri}&immutable=1", uri=True, check_same_thread=False)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn

    def _has_schema(self) -> bool:
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        return {"chunks", "chunks_fts", "embeddings", "meta", "chunks_ai", "chunks_ad", "chunks_au",
                "idx_chunks_source", "idx_chunks_path", "idx_chunks_filter"} <= names

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ------------------------------------------------------------- writing
    def sync_source(self, source: str, chunks: Sequence[Chunk]) -> dict:
        """Make the stored chunks for `source` match `chunks` exactly.

        Unchanged chunks keep their id (and embedding), changed chunks are
        updated (dropping their stale embedding), removed ones are deleted.
        """
        existing = {
            r["chunk_key"]: (r["id"], r["content_hash"])
            for r in self.conn.execute("SELECT id, chunk_key, content_hash FROM chunks WHERE source=?", (source,))
        }
        seen = set()
        added = updated = unchanged = 0
        cur = self.conn.cursor()
        for c in chunks:
            key = c.key
            if key in seen:
                continue
            seen.add(key)
            h = c.content_hash
            values = (
                c.source, c.path, c.ordinal, c.title, c.text, c.domain, c.software, c.version,
                c.doc_type, " ".join(c.tags), c.url, c.status, h,
            )
            if key in existing:
                cid, old_hash = existing[key]
                if old_hash == h:
                    unchanged += 1
                    continue
                cur.execute(
                    """UPDATE chunks SET source=?, path=?, ordinal=?, title=?, text=?, domain=?, software=?,
                       version=?, doc_type=?, tags=?, url=?, status=?, content_hash=? WHERE id=?""",
                    values + (cid,),
                )
                updated += 1
            else:
                cur.execute(
                    """INSERT INTO chunks (source, path, ordinal, title, text, domain, software, version,
                       doc_type, tags, url, status, content_hash, chunk_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values + (key,),
                )
                added += 1
        stale = [existing[k][0] for k in existing.keys() - seen]
        for i in range(0, len(stale), 500):
            batch = stale[i:i + 500]
            cur.execute(f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(batch))})", batch)
        self.conn.commit()
        self._vec_cache = None
        return {"added": added, "updated": updated, "unchanged": unchanged, "removed": len(stale)}

    def upsert_chunks(self, source: str, path: str, chunks: Sequence[Chunk]):
        """Replace the chunks of a single file within a source (used for lessons)."""
        removed = self.conn.execute("DELETE FROM chunks WHERE source=? AND path=?", (source, path)).rowcount
        for c in chunks:
            self.conn.execute(
                """INSERT INTO chunks (source, path, ordinal, title, text, domain, software, version,
                   doc_type, tags, url, status, content_hash, chunk_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c.source, c.path, c.ordinal, c.title, c.text, c.domain, c.software, c.version,
                 c.doc_type, " ".join(c.tags), c.url, c.status, c.content_hash, c.key),
            )
        self.conn.commit()
        self._vec_cache = None
        return removed

    def drop_source(self, source: str) -> int:
        n = self.conn.execute("DELETE FROM chunks WHERE source=?", (source,)).rowcount
        self.conn.commit()
        self._vec_cache = None
        return n

    # ----------------------------------------------------------- embeddings
    def chunks_missing_embeddings(self) -> list[tuple[int, str]]:
        rows = self.conn.execute(
            """SELECT c.id, c.title, c.text FROM chunks c
               LEFT JOIN embeddings e ON e.chunk_id = c.id WHERE e.chunk_id IS NULL"""
        ).fetchall()
        return [(r["id"], f"{r['title']}\n{r['text']}") for r in rows]

    def put_embeddings(self, ids: Iterable[int], vecs: np.ndarray):
        vecs = np.asarray(vecs, dtype=np.float32)
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings(chunk_id, vec) VALUES (?, ?)",
            [(int(i), v.tobytes()) for i, v in zip(ids, vecs)],
        )
        self.conn.commit()
        self._vec_cache = None

    def clear_embeddings(self):
        self.conn.execute("DELETE FROM embeddings")
        self.conn.commit()
        self._vec_cache = None

    def all_embeddings(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (ids, L2-normalised matrix). Cached until the store changes.

        The cache is also checked against a cheap signature so a long-running server picks
        up a re-ingest done by another process.
        """
        sig = tuple(self.conn.execute("SELECT COUNT(*), MAX(chunk_id), MIN(chunk_id) FROM embeddings").fetchone())
        if self._vec_cache is not None and getattr(self, "_vec_sig", None) != sig:
            self._vec_cache = None
        self._vec_sig = sig
        if self._vec_cache is None:
            rows = self.conn.execute("SELECT chunk_id, vec FROM embeddings ORDER BY chunk_id").fetchall()
            if not rows:
                self._vec_cache = (np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32))
            else:
                ids = np.array([r["chunk_id"] for r in rows], dtype=np.int64)
                mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                self._vec_cache = (ids, mat / norms)
        return self._vec_cache

    # --------------------------------------------------------------- reading
    def get(self, chunk_id: int) -> StoredChunk | None:
        row = self.conn.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return StoredChunk.from_row(row) if row else None

    def get_many(self, ids: Sequence[int]) -> dict[int, StoredChunk]:
        out: dict[int, StoredChunk] = {}
        ids = list(ids)
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            for row in self.conn.execute(f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(batch))})", batch):
                out[row["id"]] = StoredChunk.from_row(row)
        return out

    def file_chunks(self, source: str, path: str) -> list[StoredChunk]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE source=? AND path=? ORDER BY ordinal", (source, path)
        ).fetchall()
        return [StoredChunk.from_row(r) for r in rows]

    def find_files(self, path_query: str, limit: int = 20) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT DISTINCT source, path FROM chunks WHERE path LIKE ? ORDER BY source, path LIMIT ?",
            (f"%{path_query}%", limit),
        ).fetchall()
        return [(r["source"], r["path"]) for r in rows]

    def list_files(self, domain: str | None = None, software: str | None = None,
                   doc_type: str | None = None, limit: int = 200) -> list[dict]:
        where, args = _filters(domain=domain, software=software, doc_type=doc_type)
        rows = self.conn.execute(
            # SQLite returns bare columns from the MIN(ordinal) row, i.e. the file's first chunk.
            f"""SELECT source, path, MIN(ordinal) AS first, title, domain, software, version, doc_type, status,
                       COUNT(*) AS n
                FROM chunks {where} GROUP BY source, path ORDER BY domain, software, path LIMIT ?""",
            args + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def fts_search(self, match: str, limit: int, **filters) -> list[tuple[int, float]]:
        where, args = _filters(prefix="c.", **filters)
        where = where.replace("WHERE", "AND", 1) if where else ""
        try:
            rows = self.conn.execute(
                f"""SELECT c.id AS id, bm25(chunks_fts, 5.0, 1.0, 2.0) AS score
                    FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ? {where}
                    ORDER BY score LIMIT ?""",
                [match] + args + [limit],
            ).fetchall()
        except sqlite3.OperationalError as e:
            # Only a malformed MATCH expression means "no keyword hits"; a locked/busy database
            # or a missing table is a real error and must not look like an empty result.
            if _is_lock_error(e) or "no such table" in str(e):
                raise
            return []
        # bm25() is "lower is better"; flip sign so higher is better.
        return [(r["id"], -r["score"]) for r in rows]

    def filtered_ids(self, **filters) -> set[int] | None:
        where, args = _filters(**filters)
        if not where:
            return None
        return {r["id"] for r in self.conn.execute(f"SELECT id FROM chunks {where}", args)}

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        emb = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        by = lambda col: {  # noqa: E731
            (r[0] or "-"): r[1]
            for r in self.conn.execute(f"SELECT {col}, COUNT(*) FROM chunks GROUP BY {col} ORDER BY 2 DESC")
        }
        versions = {}
        for r in self.conn.execute(
            "SELECT software, version, COUNT(*) FROM chunks WHERE version IS NOT NULL GROUP BY software, version"
        ):
            versions.setdefault(r[0] or "-", []).append(r[1])
        return {
            "chunks": total,
            "embedded": emb,
            "embedding_model": self.get_meta("embedding_model"),
            "by_source": by("source"),
            "by_domain": by("domain"),
            "by_software": by("software"),
            "by_doc_type": by("doc_type"),
            "versions": versions,
        }


def _is_lock_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


def _filters(prefix: str = "", domain=None, software=None, version=None, doc_type=None, source=None):
    clauses, args = [], []

    def add_in(col, value):
        values = value if isinstance(value, (list, tuple, set)) else [value]
        values = [v.lower() for v in values if v]
        if values:
            clauses.append(f"LOWER({prefix}{col}) IN ({','.join('?' * len(values))})")
            args.extend(values)

    add_in("domain", domain)
    add_in("software", software)
    add_in("doc_type", doc_type)
    add_in("source", source)
    if version:
        # Chunks without a version apply to every version. Multi-version chunks are
        # stored as "5|6"; a filter of "6" matches "6", "6.0", "5|6.1", ... and "9" matches "09".
        v = str(version).lower().strip()
        variants = {v, v.lstrip("0") or v}
        if v.isdigit() and len(v) == 1:
            variants.add("0" + v)
        likes = " OR ".join(f"('|' || LOWER({prefix}version)) LIKE ?" for _ in variants)
        clauses.append(f"({prefix}version IS NULL OR {prefix}version = '' OR {likes})")
        args.extend(f"%|{x}%" for x in sorted(variants))
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
