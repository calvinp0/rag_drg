"""Plain JSON REST API for clients without MCP (Open WebUI tools, Ollama / llama.cpp
function-calling scripts, shell scripts).

Mounted under ``/api`` on the same HTTP server as the MCP endpoint (``rag-drg serve
--transport http``) and protected by the same bearer tokens (``/api/health`` is public)::

    GET /api/search?q=...&software=&version=&domain=&doc_type=&k=6&max_tokens=
    GET /api/level?name=wb97xd/def2tzvp&software=orca
    GET /api/context?chunk_id=123&neighbors=1
    GET /api/health
    POST /api/check_input   {"content", "filename", "submit_content"?, "submit_filename"?,
                             "client_user"?, "client_groups"?}   -> findings (+ "text")
    POST /api/diagnose      {"content", "filename"?, "software"?}  -> diagnosis (+ "text")
    POST /api/check_basis   {"basis", "elements"? | "smiles"? | "xyz"?, "software"?}
    POST /api/compose       {"spec": {...}, "protocol"?: {...}, "step"?, "client_user"?, "client_groups"?}
                            -> composed input + submit script (see docs/compose.md)

Search results are JSON (``{"query", "results": [...], "text"?}``); with ``max_tokens`` the
results are the compact selection (curated knowledge first, each text trimmed to its most
relevant lines) and ``text`` holds a ready-to-paste rendering. ``format=text`` returns just
that text as ``text/plain``.
"""

from __future__ import annotations

import contextlib
import json

import anyio

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

    async def _json_body(request: Request) -> dict:
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_BODY:
            raise _BadRequest(f"request body larger than {MAX_BODY // 1_000_000} MB; send the head and tail only")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise _BadRequest(f"request body larger than {MAX_BODY // 1_000_000} MB; send the head and tail only")
        try:
            data = json.loads(raw or b"{}")
        except ValueError as e:
            raise _BadRequest(f"body is not JSON: {e}") from e
        if not isinstance(data, dict):
            raise _BadRequest("body must be a JSON object")
        return data

    async def check_input_ep(request: Request):
        try:
            data = await _json_body(request)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        content, filename = data.get("content"), data.get("filename")
        if not isinstance(content, str) or not isinstance(filename, str) or not filename:
            return JSONResponse({"error": "need string fields 'content' and 'filename'"}, status_code=400)
        from .inputcheck import check_input, format_findings

        groups = data.get("client_groups")
        identity = (data.get("client_user") or None,
                    [str(g) for g in groups] if isinstance(groups, list) else None)

        def work():
            # Runs in a worker thread (CPU-heavy parsing must not block other users' requests);
            # the identity scope is set inside the thread so it can never leak between requests.
            with client_identity(identity):
                return check_input(content=content, filename=filename,
                                   submit_content=data.get("submit_content") or None, cfg=ctx.cfg)

        findings = await anyio.to_thread.run_sync(work)
        n = {sev: sum(1 for f in findings if f.severity == sev) for sev in ("error", "warning", "info")}
        ctx.emit({"tool": "check_input", "transport": "rest", "args": {"filename": filename},
                  "n_results": len(findings), "errors": n["error"], "warnings": n["warning"]})
        return JSONResponse({"filename": filename, "counts": n,
                             "findings": [f.to_dict() for f in findings],
                             "text": format_findings(findings, filename)})

    async def diagnose_ep(request: Request):
        try:
            data = await _json_body(request)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return JSONResponse({"error": "need a non-empty string field 'content'"}, status_code=400)
        from .diagnose import diagnose_output

        d = await anyio.to_thread.run_sync(lambda: diagnose_output(
            content=content, filename=data.get("filename") or None,
            software=data.get("software") or None, cfg=ctx.cfg))
        body = json.loads(json.dumps(d.to_dict(), default=str))
        body["text"] = d.format_text()
        ctx.emit({"tool": "diagnose_output", "transport": "rest", "args": {"filename": data.get("filename")},
                  "n_results": len(body.get("errors") or []), "status": body.get("status")})
        return JSONResponse(body)

    async def check_basis_ep(request: Request):
        try:
            data = await _json_body(request) if request.method == "POST" else dict(request.query_params)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        basis = data.get("basis")
        if not basis:
            return JSONResponse({"error": "missing 'basis'"}, status_code=400)
        from .basis import check_basis, format_report

        try:
            res = await anyio.to_thread.run_sync(lambda: check_basis(
                basis, elements=data.get("elements"), smiles=data.get("smiles"),
                xyz=data.get("xyz"), software=data.get("software")))
        except Exception as e:  # noqa: BLE001 - e.g. BSE / RDKit not installed on the server
            return JSONResponse({"error": str(e)}, status_code=422)
        res = dict(res)
        res["text"] = format_report(res)
        return JSONResponse(json.loads(json.dumps(res, default=str)))

    async def compose_ep(request: Request):
        try:
            data = await _json_body(request)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        spec, protocol = data.get("spec"), data.get("protocol")
        if not isinstance(spec, dict) or (protocol is not None and not isinstance(protocol, dict)):
            return JSONResponse({"error": "need 'spec' (object) and optional 'protocol' (object)"}, status_code=400)
        if data.get("step") is not None and not isinstance(data.get("step"), str):
            return JSONResponse({"error": "'step' must be a string"}, status_code=400)
        from .compose_ess import compose_ess_job

        groups = data.get("client_groups")
        identity = (data.get("client_user") or None,
                    [str(g) for g in groups] if isinstance(groups, list) else None)

        def work():
            with client_identity(identity):
                return compose_ess_job(spec, protocol, data.get("step"), cfg=ctx.cfg, allow_files=False)

        res = await anyio.to_thread.run_sync(work)
        ctx.emit({"tool": "compose_ess_job", "transport": "rest",
                  "args": {k: spec.get(k) for k in ("program", "job", "method", "basis")},
                  "n_results": int(bool(res.get("ok")))})
        return JSONResponse(json.loads(json.dumps(res, default=str)))

    def health(request: Request):
        return JSONResponse({"status": "ok", "readonly": bool(ctx.readonly)})

    return Starlette(routes=[
        Route("/search", search, methods=["GET"]),
        Route("/level", level, methods=["GET"]),
        Route("/context", context, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/check_input", check_input_ep, methods=["POST"]),
        Route("/diagnose", diagnose_ep, methods=["POST"]),
        Route("/check_basis", check_basis_ep, methods=["GET", "POST"]),
        Route("/compose", compose_ep, methods=["POST"]),
    ])


MAX_BODY = 8_000_000


@contextlib.contextmanager
def client_identity(identity: tuple[str | None, list[str] | None]):
    """Tell the cluster checks who the *requesting* user is (the server process is a service
    account). Uses rag_drg.tools.cluster_limits.CLIENT_IDENTITY when that plugin provides it."""
    try:
        from .cluster_limits import client_identity_scope
    except Exception:  # noqa: BLE001 - cluster registry plugin unavailable
        yield
        return
    if not any(identity):
        yield
        return
    with client_identity_scope(*identity):
        yield
