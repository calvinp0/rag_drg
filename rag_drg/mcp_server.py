"""MCP server exposing the knowledge base to Claude Code and other MCP clients.

Run locally over stdio (one process per agent session)::

    claude mcp add rag-drg -- rag-drg serve

or once for the whole group over HTTP (recommended: one writer, shared lessons)::

    rag-drg serve --transport http --host 0.0.0.0 --port 8765
    claude mcp add --transport http rag-drg http://<host>:8765/mcp
"""

from __future__ import annotations

import os
import threading

from .config import Config
from .ingest import embed_missing
from .levels import lookup
from .plugins import ServerContext, plugin_modules
from .lessons import index_lesson, write_lesson
from .search import Searcher, format_hits
from .store import Store, dumps

INSTRUCTIONS = """\
Research-group knowledge base: electronic structure software (ORCA 5/6, Gaussian 09/16,
Q-Chem 6.1, Psi4, Molpro 2024/2026, PySCF), the ARC codebase (input/output schema, settings,
capabilities), HPC cluster usage (submit scripts, queues, quotas) and project literature.

Use it BEFORE you:
- write or edit any ESS input file or ESS keyword/option (check exact syntax and version),
- write ARC input YAML, touch ARC settings/servers, or parse ARC output,
- write a submit script or run scheduler/quota commands on a cluster,
- make claims about what a program/method can or cannot do.

Pass `software` (orca, gaussian, qchem, psi4, molpro, pyscf, arc) and `version` when you know them.
Use `doc_type="reference"` for keywords/syntax and `doc_type="theory"` for method background.
Use `lookup_level_of_theory` before translating a method/functional between codes.
Results tagged `lesson` or `gotcha` are corrections the group has already made - follow them.
When a human corrects you on something this knowledge base should have told you, call
`record_lesson` so the next agent does not repeat the mistake.
"""


def _server_class():
    """MCP Python SDK 2.x calls it MCPServer; 1.x called it FastMCP."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer, 2
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP, 1
    except ImportError as e:  # pragma: no cover - optional dep
        raise SystemExit("The MCP server needs `pip install 'rag-drg[mcp]'`") from e


def build_server(cfg: Config, readonly: bool = False, host: str = "127.0.0.1", port: int = 8765):
    server_cls, sdk_major = _server_class()
    store = Store(cfg.index_path, readonly=readonly)
    searcher = Searcher(cfg, store=store)
    lock = threading.Lock()
    if sdk_major >= 2:
        mcp = server_cls("rag-drg", instructions=INSTRUCTIONS)
    else:
        mcp = server_cls("rag-drg", instructions=INSTRUCTIONS, host=host, port=port)
    mcp._rag_drg_sdk_major = sdk_major  # used by serve()
    ctx = ServerContext(cfg=cfg, store=store, searcher=searcher, lock=lock, readonly=readonly)
    mcp._rag_drg_ctx = ctx

    @mcp.tool()
    def search_knowledge(
        query: str,
        software: str | None = None,
        version: str | None = None,
        domain: str | None = None,
        doc_type: str | None = None,
        k: int = 6,
    ) -> str:
        """Search the group knowledge base (hybrid keyword + semantic).

        Args:
            query: Natural-language question or exact keywords, e.g. "ORCA TS optimisation
                with numerical Hessian" or "%maxcore".
            software: Restrict to one program: orca, gaussian, qchem, psi4, molpro, pyscf, arc,
                or a scheduler such as slurm / pbs.
            version: Restrict to a version, e.g. "6" (ORCA 6.x), "16" or "09" (Gaussian), "6.1" (Q-Chem), "2024".
                Chunks without a version always match.
            domain: ess | arc | hpc | project | literature | lessons
            doc_type: lesson | gotcha | card | template | schema | reference (keywords/usage) |
                theory (method background from manuals) | code | paper
            k: Number of results (default 6, max 20).
        """
        with lock:
            hits = searcher.search(
                query, k=max(1, min(int(k), 20)), domain=domain, software=software,
                version=version, doc_type=doc_type,
            )
        ctx.emit({
            "tool": "search_knowledge",
            "args": {"query": query, "software": software, "version": version, "domain": domain,
                     "doc_type": doc_type, "k": k},
            "n_results": len(hits),
            "results": [{"source": h.chunk.source, "path": h.chunk.path, "title": h.chunk.title,
                         "doc_type": h.chunk.doc_type, "score": round(h.score, 5)} for h in hits],
        })
        return format_hits(hits)

    @mcp.tool()
    def get_context(chunk_id: int, neighbors: int = 2) -> str:
        """Return the chunk with `chunk_id` plus `neighbors` chunks before/after it from the same file."""
        with lock:
            chunks = searcher.context(int(chunk_id), max(0, min(int(neighbors), 10)))
        if not chunks:
            return f"No chunk with id {chunk_id}."
        head = f"{chunks[0].source}:{chunks[0].path}"
        return head + "\n\n" + "\n\n".join(f"## {c.title} (chunk {c.id})\n{c.text}" for c in chunks)

    @mcp.tool()
    def read_document(path: str, source: str | None = None) -> str:
        """Return a whole document (e.g. a curated card or submit-script template) by path.

        `path` may be a full indexed path (as shown in search results after `source:`)
        or a unique substring of it, e.g. "slurm_orca".
        """
        with lock:
            matches = store.find_files(path, limit=20)
            if source:
                matches = [m for m in matches if m[0] == source]
            exact = [m for m in matches if m[1] == path]
            if exact:
                matches = exact
            if not matches:
                return f"No document matching '{path}'."
            if len(matches) > 1:
                return "Ambiguous, candidates:\n" + "\n".join(f"- {s}:{p}" for s, p in matches)
            src_name, rel = matches[0]
            # Prefer the file on disk for local sources (exact formatting of templates).
            for s in cfg.sources + [_lessons(cfg)]:
                if s.name == src_name and s.type == "local" and s.path and (s.path / rel).is_file():
                    f = s.path / rel
                    if f.suffix.lower() != ".pdf":
                        return f"{src_name}:{rel}\n\n" + f.read_text(errors="replace")
            chunks = store.file_chunks(src_name, rel)
        return f"{src_name}:{rel}\n\n" + "\n\n".join(c.text for c in chunks)

    @mcp.tool()
    def list_documents(
        domain: str | None = None, software: str | None = None, doc_type: str | None = None, limit: int = 100
    ) -> str:
        """List indexed documents (one line per file), optionally filtered. Useful to browse
        curated cards, templates and lessons for a program before searching."""
        with lock:
            rows = store.list_files(domain=domain, software=software, doc_type=doc_type, limit=min(int(limit), 500))
        if not rows:
            return "No documents match."
        lines = []
        for r in rows:
            scope = "/".join(x for x in (r["domain"], r["software"]) if x) or "-"
            v = f" v{r['version'].replace('|', ',')}" if r["version"] else ""
            st = f" [{r['status']}]" if r["status"] else ""
            lines.append(f"- {r['source']}:{r['path']}  ({scope}{v}, {r['doc_type']}{st}) {(r['title'] or '').split(' > ')[0]}")
        return "\n".join(lines)

    @mcp.tool()
    def lookup_level_of_theory(name: str = "", software: str | None = None) -> str:
        """Which ESS supports a method/functional/dispersion/solvation model, and how each writes it.

        Use this before translating a level of theory between codes (e.g. an ARC level
        'wb97xd/def2tzvp' to ORCA) or choosing a code for a method. Names are matched loosely
        ("wB97X-D", "wb97xd", "DLPNO-CCSD(T)/cc-pVTZ"). Empty name lists all known entries.

        Args:
            name: Method, functional or model name, optionally with '/basis'.
            software: Only show this code's row (gaussian, orca, qchem, psi4, molpro, pyscf),
                plus which other codes support it as-is.
        """
        out = lookup(cfg, name or None, software)
        ctx.emit({"tool": "lookup_level_of_theory", "args": {"name": name, "software": software},
                  "n_results": 0 if out.startswith(("No ", "'")) else 1})
        return out

    @mcp.tool()
    def list_knowledge_sources() -> str:
        """Show what the index contains: chunk counts per source, domain, software, doc type and versions."""
        with lock:
            return dumps(store.stats())

    if not readonly:

        @mcp.tool()
        def record_lesson(
            title: str,
            mistake: str,
            correction: str,
            domain: str,
            software: str | None = None,
            version: str | None = None,
            evidence: str | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Record a correction so future agents don't repeat a mistake.

            Call this when a user corrects you about ESS syntax/capabilities, ARC usage, HPC
            usage or project conventions - or when you discover the knowledge base itself was
            wrong or missing something. Keep it short and concrete.

            Args:
                title: One line, e.g. "ORCA 6: use %maxcore per core in MB, not total".
                mistake: What was done/assumed wrongly (include the wrong snippet).
                correction: The correct approach (include the right snippet).
                domain: ess | arc | hpc | project
                software: orca | gaussian | psi4 | molpro | pyscf | arc | slurm | pbs | ...
                version: Version it applies to, if specific (e.g. "6", "16", "2024").
                evidence: Manual section, URL, error message or who confirmed it.
                tags: Extra keywords that will help retrieval.
            """
            from .lessons import find_similar, lesson_text
            from .tools.lessons_workflow import submit_lesson

            author = ctx.current_user() or os.environ.get("RAG_DRG_AUTHOR") or os.environ.get("USER")
            with lock:
                similar = find_similar(
                    cfg, searcher, lesson_text(title, mistake, correction), software=software, domain=domain,
                )
                path = write_lesson(
                    cfg, title=title, mistake=mistake, correction=correction, domain=domain,
                    software=software, version=version, evidence=evidence, tags=tags, author=author,
                    similar=[s.path for s in similar],
                )
                index_lesson(cfg, store, path)
                try:
                    embed_missing(cfg, store, progress=lambda *_: None)
                except Exception:  # noqa: BLE001 - keyword search still finds it
                    pass
            rel = path.relative_to(cfg.root) if path.is_relative_to(cfg.root) else path
            # Git/GitHub work happens outside the index lock and never raises.
            pr = submit_lesson(cfg, path, author=author)
            ctx.emit({"tool": "record_lesson", "args": {"title": title, "domain": domain, "software": software},
                      "path": str(rel), "n_results": 1, "similar": [s.path for s in similar],
                      "pr": pr.pr_url if pr else None, "pr_error": pr.error if pr else None})
            out = f"Lesson saved to {rel} (status: unreviewed) and indexed."
            if similar:
                out += "\npossibly duplicates: " + ", ".join(f"{s.path} ({s.score:.2f})" for s in similar)
                out += ("\nIf one of these already says the same thing, tell the user; the reviewer can merge them "
                        "(they are listed in the lesson's `similar:` front matter).")
            if pr is None:
                out += "\nAsk the user to commit it and open a PR so the group can review it."
            else:
                out += "\n" + pr.summary()
            return out

    for mod in plugin_modules():
        if hasattr(mod, "register_mcp"):
            mod.register_mcp(mcp, ctx)

    return mcp


def _lessons(cfg: Config):
    from .lessons import lessons_source

    return lessons_source(cfg)


def serve(cfg: Config, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765, readonly: bool = False):
    mcp = build_server(cfg, readonly=readonly, host=host, port=port)
    kwargs = {"host": host, "port": port} if mcp._rag_drg_sdk_major >= 2 else {}
    if transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http", **kwargs)
    elif transport == "sse":
        mcp.run(transport="sse", **kwargs)
    else:
        mcp.run()
