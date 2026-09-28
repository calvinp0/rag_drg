"""Hybrid retrieval: BM25 (FTS5) + optional dense vectors, fused with RRF.

Keyword search matters a lot here: agents ask about exact tokens such as
``%maxcore``, ``opt=(calcfc,ts)``, ``ts_guess_level`` or ``def2-TZVP``, which
embedding models blur. Dense vectors help with paraphrased questions
("how do I restart a crashed optimisation"). Curated cards and lessons get
a ranking boost because they encode what the group has already verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .embeddings import Embedder, make_embedder
from .store import Store, StoredChunk

RRF_K = 60
CANDIDATES = 60

# Multipliers applied after fusion. Curated knowledge wins ties against raw manuals.
TYPE_BOOST = {
    "lesson": 1.4,
    "gotcha": 1.35,
    "card": 1.25,
    "template": 1.2,
    "schema": 1.2,
    "reference": 1.0,
    "paper": 1.0,
    "code": 0.95,
    "scaffold": 0.6,  # fill-in-the-blanks _TEMPLATE cards
}
STATUS_BOOST = {"verified": 1.1, "unreviewed": 0.9, "outdated": 0.5}

_STOP = set(
    """a an and are as at be but by can do does for from how i if in into is it its of on or
    should so that the their then there these this to use used using what when where which
    who why will with you your we our me my""".split()
)
_WORD = re.compile(r"[A-Za-z0-9]+")
# Things like %maxcore, opt=calcfc, ts_guess_level, def2-TZVP, DLPNO-CCSD(T), B3LYP-D3BJ
_IDENT = re.compile(r"[%$]?[A-Za-z0-9][A-Za-z0-9_%=().+\-/*]*")


def fts_query(query: str) -> str:
    words = []
    for w in _WORD.findall(query):
        lw = w.lower()
        if lw in _STOP or lw in words:
            continue
        words.append(lw)
    return " OR ".join(f'"{w}"' for w in words)


def identifiers(query: str) -> list[str]:
    out = []
    for m in _IDENT.findall(query):
        tok = m.strip("().,")
        if len(tok) < 3:
            continue
        has_special = bool(re.search(r"[_%=\-+(/*]", tok)) or (
            re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok)
        )
        if has_special and tok.lower() not in out:
            out.append(tok.lower())
    return out


@dataclass
class Hit:
    chunk: StoredChunk
    score: float
    via: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.chunk.to_dict()
        d["score"] = round(self.score, 5)
        d["via"] = self.via
        return d


class Searcher:
    def __init__(self, cfg: Config, store: Store | None = None, embedder: Embedder | None | bool = True):
        self.cfg = cfg
        self.store = store or Store(cfg.index_path, readonly=True)
        if embedder is True:
            embedder = None
            index_model = self.store.get_meta("embedding_model")
            if index_model and cfg.embeddings.provider.lower() not in ("none", "", "off", "bm25"):
                try:
                    embedder = make_embedder(cfg.embeddings)
                except Exception:  # noqa: BLE001 - fall back to keyword search
                    embedder = None
                if embedder is not None and embedder.name != index_model:
                    embedder = None  # vectors in the index come from another model
        self.embedder = embedder or None

    def search(
        self,
        query: str,
        k: int = 6,
        domain: str | list[str] | None = None,
        software: str | list[str] | None = None,
        version: str | None = None,
        doc_type: str | list[str] | None = None,
        source: str | list[str] | None = None,
        max_per_file: int = 2,
    ) -> list[Hit]:
        filters = dict(domain=domain, software=software, version=version, doc_type=doc_type, source=source)
        scores: dict[int, float] = {}
        via: dict[int, list[str]] = {}

        match = fts_query(query)
        if match:
            for rank, (cid, _) in enumerate(self.store.fts_search(match, CANDIDATES, **filters)):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                via.setdefault(cid, []).append("keyword")

        # Identifiers (GEOM_MAXITER, ts_guess_level, def2-TZVP) as FTS phrases: a third ranked
        # list, so a chunk containing the exact token sequence wins over one that merely
        # mentions "geom" and "maxiter" separately.
        idents = identifiers(query)
        phrases = [" ".join(_WORD.findall(t)) for t in idents]
        phrase_match = " OR ".join(f'"{p}"' for p in phrases if " " in p)
        if phrase_match:
            for rank, (cid, _) in enumerate(self.store.fts_search(phrase_match, CANDIDATES, **filters)):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                via.setdefault(cid, []).append("phrase")

        if self.embedder is not None:
            ids, mat = self.store.all_embeddings()
            if len(ids):
                allowed = self.store.filtered_ids(**filters)
                qv = self.embedder.embed([query], is_query=True)[0]
                qv = qv / (np.linalg.norm(qv) or 1.0)
                sims = mat @ qv
                if allowed is not None:
                    mask = np.isin(ids, np.fromiter(allowed, dtype=np.int64, count=len(allowed)))
                    sims = np.where(mask, sims, -np.inf)
                top = np.argsort(-sims)[:CANDIDATES]
                for rank, idx in enumerate(top):
                    if not np.isfinite(sims[idx]):
                        break
                    cid = int(ids[idx])
                    scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                    via.setdefault(cid, []).append("semantic")

        if not scores:
            return []
        chunks = self.store.get_many(list(scores))
        hits: list[Hit] = []
        for cid, base in scores.items():
            c = chunks.get(cid)
            if c is None:
                continue
            s = base * TYPE_BOOST.get(c.doc_type or "reference", 1.0) * STATUS_BOOST.get(c.status or "", 1.0)
            if idents:
                hay = f"{c.title}\n{c.text}".lower()
                n = sum(1 for t in idents if t in hay)
                if n:
                    s *= 1.0 + min(0.6, 0.3 * n)
                    via[cid].append("exact")
            hits.append(Hit(c, s, via[cid]))
        hits.sort(key=lambda h: h.score, reverse=True)

        out: list[Hit] = []
        per_file: dict[tuple[str, str], int] = {}
        for h in hits:
            key = (h.chunk.source, h.chunk.path)
            if per_file.get(key, 0) >= max_per_file:
                continue
            per_file[key] = per_file.get(key, 0) + 1
            out.append(h)
            if len(out) >= k:
                break
        return out

    def context(self, chunk_id: int, neighbors: int = 1) -> list[StoredChunk]:
        c = self.store.get(chunk_id)
        if c is None:
            return []
        siblings = self.store.file_chunks(c.source, c.path)
        return [s for s in siblings if abs(s.ordinal - c.ordinal) <= neighbors]


def format_hits(hits: list[Hit], max_chars: int = 2500) -> str:
    if not hits:
        return "No results. Try different keywords, drop filters, or check `list_knowledge_sources`."
    blocks = []
    for i, h in enumerate(hits, 1):
        c = h.chunk
        scope = "/".join(p for p in (c.domain, c.software) if p) or "general"
        if c.version:
            scope += f" v{c.version.replace('|', ',')}"
        badges = [c.doc_type or "reference"]
        if c.status:
            badges.append(c.status)
        text = c.text if len(c.text) <= max_chars else c.text[:max_chars] + "\n[... truncated; use get_context]"
        ref = f"{c.source}:{c.path}"
        if c.url:
            ref += f"  <{c.url}>"
        blocks.append(
            f"[{i}] {c.title}\n"
            f"    scope: {scope} | {' · '.join(badges)} | chunk_id={c.id}\n"
            f"    from: {ref}\n\n{text}"
        )
    return "\n\n---\n\n".join(blocks)
