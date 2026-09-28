"""Plain JSON REST API for clients without MCP (Open WebUI tools, Ollama / llama.cpp
function-calling scripts, shell scripts).

Mounted under ``/api`` on the same HTTP server as the MCP endpoint (``rag-drg serve
--transport http``) and protected by the same bearer tokens (``/api/health`` is public)::

    GET /api/search?q=...&software=&version=&domain=&doc_type=&k=6&max_tokens=
    GET /api/level?name=wb97xd/def2tzvp&software=orca
    GET /api/context?chunk_id=123&neighbors=1
    GET /api/health

Search results are JSON (``{"query", "results": [...], "text"?}``); with ``max_tokens`` the
results are the compact selection (curated knowledge first, each text trimmed to its most
relevant lines) and ``text`` holds a ready-to-paste rendering. ``format=text`` returns just
that text as ``text/plain``.
"""

from __future__ import annotations

from typing import Any

MAX_K = 20


def _int(value: str | None, default: int | None, lo: int, hi: int) -> int | None:
    if value in (None, ""):
        return default
    try:
        return max(lo, min(int(value), hi))
    except ValueError:
        raise _BadRequest(f"not an integer: {value!r}") from None


class _BadRequest(ValueError):
    pass


def run_search(ctx, query: str, *, software=None, version=None, domain=None, doc_type=None,
               k: int = 6, max_tokens: int | None = None, transport: str = "rest") -> tuple[list, str | None]:
    """Search + event, shared by the REST endpoint. Returns (hits_or_pairs, compact_text)."""
    from ..search import compact_hits, format_hits, make_reranker

    with ctx.lock:
        hits = ctx.searcher.search(query, k=k, domain=domain, software=software, version=version,
                                   doc_type=doc_type, rerank=make_reranker(ctx.cfg))
    ctx.emit({
        "tool": "search_knowledge", "transport": transport,
        "args": {"query": query, "software": software, "version": version, "domain": domain,
                 "doc_type": doc_type, "k": k, "max_tokens": max_tokens},
        "n_results": len(hits),
        "results": [{"source": h.chunk.source, "path": h.chunk.path, "title": h.chunk.title,
                     "doc_type": h.chunk.doc_type, "score": round(h.score, 5)} for h in hits],
    })
    if max_tokens is None:
        return [h.to_dict() for h in hits], None
    rows = []
    for h, body in compact_hits(hits, query, max_tokens):
        d = h.to_dict()
        d["text"] = body
        rows.append(d)
    return rows, format_hits(hits, query=query, max_tokens=max_tokens)


def build_rest_app(ctx) -> Any:
    """Starlette app with the endpoints above (mount it at /api)."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    from ..levels import lookup

    def search(request: Request):
        qp = request.query_params
        q = (qp.get("q") or qp.get("query") or "").strip()
        if not q:
            return JSONResponse({"error": "missing query parameter 'q'"}, status_code=400)
        try:
            k = _int(qp.get("k"), 6, 1, MAX_K)
            max_tokens = _int(qp.get("max_tokens"), None, 50, 100_000)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        rows, text = run_search(
            ctx, q, software=qp.get("software") or None, version=qp.get("version") or None,
            domain=qp.get("domain") or None, doc_type=qp.get("doc_type") or None, k=k, max_tokens=max_tokens,
        )
        if qp.get("format") == "text":
            if text is None:
                text = "\n\n".join(f"[{i}] {r['title']}\nsrc: {r['source']}:{r['path']} chunk_id={r['id']}\n{r['text']}"
                                   for i, r in enumerate(rows, 1)) or "No results."
            return PlainTextResponse(text)
        body: dict = {"query": q, "results": rows}
        if text is not None:
            body["text"] = text
        return JSONResponse(body)

    def level(request: Request):
        qp = request.query_params
        name = qp.get("name") or ""
        software = qp.get("software") or None
        out = lookup(ctx.cfg, name or None, software)
        ctx.emit({"tool": "lookup_level_of_theory", "transport": "rest",
                  "args": {"name": name, "software": software},
                  "n_results": 0 if out.startswith(("No ", "'")) else 1})
        return JSONResponse({"name": name, "software": software, "text": out})

    def context(request: Request):
        qp = request.query_params
        try:
            cid = _int(qp.get("chunk_id"), None, 0, 2**62)
            neighbors = _int(qp.get("neighbors"), 1, 0, 10)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if cid is None:
            return JSONResponse({"error": "missing query parameter 'chunk_id'"}, status_code=400)
        with ctx.lock:
            chunks = ctx.searcher.context(cid, neighbors)
        if not chunks:
            return JSONResponse({"error": f"no chunk with id {cid}"}, status_code=404)
        rows = [c.to_dict() for c in chunks]
        return JSONResponse({"chunk_id": cid, "chunks": rows})

    def health(request: Request):
        return JSONResponse({"status": "ok", "readonly": bool(ctx.readonly)})

    return Starlette(routes=[
        Route("/search", search, methods=["GET"]),
        Route("/level", level, methods=["GET"]),
        Route("/context", context, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ])
