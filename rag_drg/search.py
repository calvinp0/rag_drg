"""Hybrid retrieval: BM25 (FTS5) + optional dense vectors, fused with RRF.

Keyword search matters a lot here: agents ask about exact tokens such as
``%maxcore``, ``opt=(calcfc,ts)``, ``ts_guess_level`` or ``def2-TZVP``, which
embedding models blur. Dense vectors help with paraphrased questions
("how do I restart a crashed optimisation"). Curated cards and lessons get
a ranking boost because they encode what the group has already verified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

from .config import Config
from .embeddings import Embedder, make_embedder
from .store import Store, StoredChunk

RRF_K = 60
CANDIDATES = 60
LESSONS_DOMAIN = "lessons"  # pseudo-domain for search filters; equals lessons.LESSONS_SOURCE

# Multipliers applied after fusion. Curated knowledge wins ties against raw manuals.
TYPE_BOOST = {
    "lesson": 1.4,
    "gotcha": 1.35,
    "card": 1.25,
    "template": 1.2,
    "schema": 1.2,
    "reference": 1.0,
    "theory": 0.95,
    "paper": 1.0,
    "code": 0.95,
    "scaffold": 0.6,
    "error": 1.0,     # errors.yaml entries: exact-match hits are strong on their own; no boost so
                      # they don't crowd out cards for questions that merely mention an error word  # fill-in-the-blanks _TEMPLATE cards
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
        rerank: Callable[[str, list[str]], Sequence[float]] | None = None,
    ) -> list[Hit]:
        """Hybrid search. `rerank(query, texts) -> scores` (optional, e.g. a cross-encoder from
        :func:`make_reranker`) re-orders the best `rerank.top_n` (default 30) fused candidates."""
        # `domain="lessons"` is a pseudo-domain: lesson chunks carry their real domain (ess, hpc,
        # ...), so it means "only the recorded lessons", i.e. the lessons source.
        if isinstance(domain, str) and domain.strip().lower() == LESSONS_DOMAIN or (
                isinstance(domain, (list, tuple)) and [str(d).lower() for d in domain] == [LESSONS_DOMAIN]):
            domain = None
            if source is None:
                source = LESSONS_DOMAIN
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
        verbatim = " ".join(query.lower().split())
        if len(verbatim) < 12 or len(verbatim.split()) < 3:
            verbatim = ""
        hits: list[Hit] = []
        for cid, base in scores.items():
            c = chunks.get(cid)
            if c is None:
                continue
            s = base * TYPE_BOOST.get(c.doc_type or "reference", 1.0) * STATUS_BOOST.get(c.status or "", 1.0)
            hay = f"{c.title}\n{c.text}".lower()
            if idents:
                n = sum(1 for t in idents if t in hay)
                if n:
                    s *= 1.0 + min(0.6, 0.3 * n)
                    via[cid].append("exact")
            # The whole query appears word for word (typically a pasted error message or keyword line).
            if verbatim and verbatim in " ".join(hay.split()):
                s *= 1.6
                via[cid].append("verbatim")
            hits.append(Hit(c, s, via[cid]))
        hits.sort(key=lambda h: h.score, reverse=True)
        if rerank is not None and hits:
            hits = _apply_rerank(rerank, query, hits)

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


def _apply_rerank(rerank, query: str, hits: list[Hit]) -> list[Hit]:
    """Re-order the top `rerank.top_n` hits by reranker score; the curated/status boosts
    stay as a mild prior (log-boost added to the reranker logit). The rest keep their order."""
    top_n = max(1, int(getattr(rerank, "top_n", 30) or 30))
    head, tail = hits[:top_n], hits[top_n:]
    try:
        scores = list(rerank(query, [f"{h.chunk.title}\n{h.chunk.text}" for h in head]))
    except Exception:  # noqa: BLE001 - a broken reranker must not break search
        return hits
    if len(scores) != len(head):
        return hits
    for h, sc in zip(head, scores):
        prior = TYPE_BOOST.get(h.chunk.doc_type or "reference", 1.0) * STATUS_BOOST.get(h.chunk.status or "", 1.0)
        h.score = float(sc) + math.log(prior)
        h.via.append("rerank")
    head.sort(key=lambda h: h.score, reverse=True)
    return head + tail


class CrossEncoderReranker:
    """`rerank(query, texts) -> scores` backed by a sentence-transformers CrossEncoder
    (or any object with a compatible `predict(list[tuple[str, str]])`)."""

    def __init__(self, model, top_n: int = 30, max_chars: int = 2000):
        self.model = model
        self.top_n = top_n
        self.max_chars = max_chars

    def __call__(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        pairs = [(query, t[: self.max_chars]) for t in texts]
        return [float(x) for x in self.model.predict(pairs)]


_RERANKERS: dict[tuple[str, int], CrossEncoderReranker | None] = {}


def make_reranker(cfg: Config) -> CrossEncoderReranker | None:
    """The reranker configured under `rerank:` in rag_drg.yaml, or None (the default).

        rerank:
          model: cross-encoder/ms-marco-MiniLM-L-6-v2
          top_n: 30

    Needs `pip install 'rag-drg[st]'`; if sentence-transformers is missing or the model
    can't be loaded, search silently stays un-reranked. Loaded once per process.
    """
    rc = (getattr(cfg, "extra", None) or {}).get("rerank") or {}
    if not isinstance(rc, dict) or not rc.get("model") or rc.get("enabled") is False:
        return None
    key = (str(rc["model"]), int(rc.get("top_n", 30)))
    if key not in _RERANKERS:
        try:
            from sentence_transformers import CrossEncoder

            _RERANKERS[key] = CrossEncoderReranker(CrossEncoder(key[0]), top_n=key[1])
        except Exception as e:  # noqa: BLE001 - optional dependency / offline model
            import logging

            logging.getLogger(__name__).warning("reranker %s unavailable (%s); searching without it", key[0], e)
            _RERANKERS[key] = None
    return _RERANKERS[key]


# --- compact output for local models (format_hits(..., max_tokens=N)) ----------------------

CHARS_PER_TOKEN = 4
# Curated corrections first: they are short and the most likely to prevent a mistake.
_PRIORITY_TYPES = ("lesson", "gotcha", "card")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _query_terms(query: str) -> list[str]:
    terms = identifiers(query)
    for w in _WORD.findall(query):
        lw = w.lower()
        if len(lw) >= 3 and lw not in _STOP and lw not in terms:
            terms.append(lw)
    return terms


def _segments(text: str) -> list[tuple[str, bool]]:
    """Split into (segment, is_code): fenced code blocks stay whole, other text line by line."""
    segs: list[tuple[str, bool]] = []
    block: list[str] | None = None
    for line in text.splitlines():
        if block is not None:
            block.append(line)
            if _FENCE.match(line):
                segs.append(("\n".join(block), True))
                block = None
            continue
        if _FENCE.match(line):
            block = [line]
            continue
        if line.strip():
            segs.append((line, line.startswith(("    ", "\t"))))
    if block is not None:
        segs.append(("\n".join(block), True))
    return segs


def trim_to_relevant(text: str, terms: list[str], max_chars: int) -> str:
    """The lines of `text` most relevant to `terms` (plus code blocks next to them), in their
    original order, within `max_chars`. Gaps are marked with '…'."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    segs = _segments(text)
    if not segs:
        return text[:max_chars]
    scores = []
    for seg, _ in segs:
        low = seg.lower()
        scores.append(sum(2 if (" " not in t and any(c in t for c in "_%=-(/")) else 1 for t in terms if t in low))
    ranked = []
    for i, (seg, is_code) in enumerate(segs):
        sc = float(scores[i])
        if is_code:
            # Input examples are what agents need: keep code that matches, or that directly
            # follows a matching line (the line introducing it).
            sc += 1.0 if (scores[i] or (i > 0 and scores[i - 1])) else 0.2
        if i == 0:
            sc += 0.3  # the opening line usually states what the section is about
        ranked.append((sc, -i, i))
    ranked.sort(reverse=True)
    if not any(scores):
        ranked.sort(key=lambda r: r[2])  # nothing matches: keep the opening lines, in order
    chosen: set[int] = set()
    used = 0
    for sc, _, i in ranked:
        if any(scores) and sc < 1:
            break  # only lines that match (and code next to matches) once something matched
        cost = len(segs[i][0]) + 3  # + a possible "…" gap marker
        if used + cost <= max_chars:
            chosen.add(i)
            used += cost
    if not chosen:  # the best segment alone is too long: cut it
        best = segs[ranked[0][2]][0]
        return best[: max(0, max_chars - 2)].rstrip() + " …"
    out: list[str] = []
    prev = -1
    for i in sorted(chosen):
        if i != prev + 1:
            out.append("…")
        out.append(segs[i][0])
        prev = i
    if prev != len(segs) - 1:
        out.append("…")
    return "\n".join(out)


def _compact_header(n: int, c: StoredChunk) -> str:
    badges = c.doc_type or "reference"
    if c.status:
        badges += f", {c.status}"
    scope = "/".join(p for p in (c.software, f"v{c.version.replace('|', ',')}" if c.version else None) if p)
    return (f"[{n}] {c.title} ({badges}{', ' + scope if scope else ''})\n"
            f"src: {c.source}:{c.path} chunk_id={c.id}" + (f" <{c.url}>" if c.url else "") + "\n")


def compact_hits(hits: list[Hit], query: str, max_tokens: int, reserve: int = 0) -> list[tuple[Hit, str]]:
    """Pick and trim hits to fit ~`max_tokens` (4 chars/token, header included): curated
    lessons/gotchas/cards first, then the rest in rank order; each body cut down to its most
    relevant lines. Returns [(hit, trimmed_text)] in output order."""
    budget = max(200, int(max_tokens) * CHARS_PER_TOKEN) - reserve
    terms = _query_terms(query or "")
    order = sorted(range(len(hits)),
                   key=lambda i: (0 if (hits[i].chunk.doc_type or "") in _PRIORITY_TYPES else 1, i))
    out: list[tuple[Hit, str]] = []
    remaining = budget
    for n, i in enumerate(order):
        h = hits[i]
        head_len = len(_compact_header(len(out) + 1, h.chunk)) + (2 if out else 0)
        share = max(160, remaining // (len(order) - n))
        room = min(remaining, share) - head_len
        if room < 80:
            room = remaining - head_len
            if room < 80:
                if out:
                    break
                room = max(40, room)  # always show the best hit, however small the budget
        body = trim_to_relevant(h.chunk.text, terms, room)
        out.append((h, body))
        remaining -= head_len + len(body)
    return out


def _format_compact(hits: list[Hit], query: str, max_tokens: int) -> str:
    footer = "\n[{n}/{m} results, trimmed; get_context(chunk_id) gives full text]"
    reserve = len(footer)
    picked = compact_hits(hits, query, max_tokens, reserve=reserve)
    if not picked:
        return "No results fit in max_tokens; raise it or search without max_tokens."
    text = "\n\n".join(_compact_header(n, h.chunk) + body for n, (h, body) in enumerate(picked, 1))
    tail = footer.format(n=len(picked), m=len(hits))
    limit = max(200, int(max_tokens) * CHARS_PER_TOKEN) - len(tail)
    if len(text) > limit:  # tiny budgets: cut the body, keep the citation footer
        text = text[: limit - 2].rstrip() + " …"
    return text + tail


def format_hits(hits: list[Hit], max_chars: int = 2500, *, query: str | None = None,
                max_tokens: int | None = None) -> str:
    """Render hits for an agent. With `max_tokens` (≈4 chars/token), return a compact view
    for small-context local models: lessons/gotchas/cards first, each chunk trimmed to the
    lines that mention the query terms (and nearby code blocks), citations kept."""
    if not hits:
        return "No results. Try different keywords, drop filters, or check `list_knowledge_sources`."
    if max_tokens is not None:
        return _format_compact(hits, query or "", max_tokens)
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
