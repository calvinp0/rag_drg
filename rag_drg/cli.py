"""Command-line interface: ``rag-drg <command>``.

    rag-drg fetch [--source NAME ...]        clone/download remote sources
    rag-drg ingest [--source NAME ...] [--fetch] [--no-embed]
    rag-drg search "query" [--software orca --version 6 --k 6 --json]
    rag-drg serve [--transport stdio|http] [--host H --port P] [--readonly]
    rag-drg lesson --title ... --mistake ... --correction ... --domain ess --software orca
    rag-drg stats
    rag-drg sources
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import load_config


def _add_filters(p: argparse.ArgumentParser):
    p.add_argument("--software", "-s", help="orca, gaussian, qchem, psi4, molpro, pyscf, arc, slurm, ...")
    p.add_argument("--version", "-v", help="e.g. 6, 16, 2024")
    p.add_argument("--domain", "-d", help="ess, arc, hpc, project, literature")
    p.add_argument("--doc-type", help="lesson, gotcha, card, template, schema, reference, theory, code, paper")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-drg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", "-c", help="path to rag_drg.yaml")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="clone/download remote sources into the cache")
    p.add_argument("--source", action="append")

    p = sub.add_parser("ingest", help="(re)build the index incrementally")
    p.add_argument("--source", action="append", help="only these sources (repeatable)")
    p.add_argument("--fetch", action="store_true", help="fetch remote sources first")
    p.add_argument("--no-embed", action="store_true", help="skip computing embeddings")
    p.add_argument("--drop", action="append", default=[], help="remove a source from the index")

    p = sub.add_parser("search", help="query the index")
    p.add_argument("query", nargs="+")
    p.add_argument("--k", type=int, default=6)
    p.add_argument("--json", action="store_true", help="machine-readable output (for non-MCP agents)")
    _add_filters(p)

    p = sub.add_parser("serve", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio")
    p.add_argument("--host", default=os.environ.get("RAG_DRG_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("RAG_DRG_PORT", "8765")))
    p.add_argument("--readonly", action="store_true", help="open the index read-only; disables record_lesson")

    p = sub.add_parser("lesson", help="record a lesson learned (a correction)")
    p.add_argument("--title", required=True)
    p.add_argument("--mistake", required=True)
    p.add_argument("--correction", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--software")
    p.add_argument("--version")
    p.add_argument("--evidence")
    p.add_argument("--tag", action="append", dest="tags")

    p = sub.add_parser("check-pdf", help="is a PDF text-searchable, or does it need OCR? (and does it have bookmarks)")
    p.add_argument("paths", nargs="+")

    p = sub.add_parser("level", help="which ESS supports a level of theory and how to write it")
    p.add_argument("name", nargs="?", default="", help="e.g. wB97X-D, 'DLPNO-CCSD(T)/cc-pVTZ'; empty lists all")
    p.add_argument("--software", "-s")

    sub.add_parser("lint", help="validate front matter of curated cards and lessons (for CI / PR review)")
    sub.add_parser("stats", help="show index contents")
    sub.add_parser("sources", help="list configured sources")

    from .plugins import plugin_modules

    plugin_handlers: dict = {}
    for mod in plugin_modules():
        if hasattr(mod, "register_cli"):
            plugin_handlers.update(mod.register_cli(sub) or {})

    args = parser.parse_args(argv)
    # stdout is the MCP channel for stdio transport; keep logs on stderr.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr, format="%(message)s")
    cfg = load_config(args.config)
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    if args.cmd in plugin_handlers:
        return int(plugin_handlers[args.cmd](args, cfg) or 0)

    if args.cmd == "fetch":
        from .ingest import fetch_source

        for src in cfg.sources:
            if src.type == "local" or (args.source and src.name not in args.source):
                continue
            if not src.enabled and not args.source:
                log(f"[{src.name}] disabled, skipping (pass --source {src.name} to force)")
                continue
            log(f"[{src.name}] fetching {src.url or f'{len(src.urls)} urls'} -> {src.path}")
            fetch_source(src)
        return 0

    if args.cmd == "ingest":
        from .ingest import ingest
        from .store import Store

        if args.drop:
            st = Store(cfg.index_path)
            for name in args.drop:
                log(f"[{name}] dropped {st.drop_source(name)} chunks")
            st.close()
        report = ingest(cfg, only=args.source, fetch=args.fetch, embed=not args.no_embed, progress=log)
        log(json.dumps(report, indent=2))
        return 0

    if args.cmd == "search":
        from .search import Searcher, format_hits

        searcher = Searcher(cfg)
        hits = searcher.search(
            " ".join(args.query), k=args.k, domain=args.domain, software=args.software,
            version=args.version, doc_type=args.doc_type,
        )
        if args.json:
            print(json.dumps([h.to_dict() for h in hits], indent=2, default=str))
        else:
            print(format_hits(hits))
        return 0

    if args.cmd == "serve":
        from .mcp_server import serve

        serve(cfg, transport=args.transport, host=args.host, port=args.port, readonly=args.readonly)
        return 0

    if args.cmd == "lesson":
        from .lessons import find_similar, index_lesson, lesson_text, write_lesson
        from .search import Searcher
        from .store import Store
        from .tools.lessons_workflow import submit_lesson

        author = os.environ.get("RAG_DRG_AUTHOR") or os.environ.get("USER")
        st = Store(cfg.index_path)
        similar = find_similar(
            cfg, Searcher(cfg, store=st, embedder=False), lesson_text(args.title, args.mistake, args.correction),
            software=args.software, domain=args.domain,
        )
        path = write_lesson(
            cfg, title=args.title, mistake=args.mistake, correction=args.correction, domain=args.domain,
            software=args.software, version=args.version, evidence=args.evidence, tags=args.tags,
            author=author, similar=[s.path for s in similar],
        )
        index_lesson(cfg, st, path)
        st.close()
        print(f"Wrote {path} (status: unreviewed).")
        if similar:
            print("possibly duplicates: " + ", ".join(f"{s.path} ({s.score:.2f})" for s in similar))
        pr = submit_lesson(cfg, path, author=author)
        print("Commit it and open a PR for review." if pr is None else pr.summary())
        return 0

    if args.cmd == "check-pdf":
        from pathlib import Path

        from .chunking import pdf_quality

        advice = {
            "ok": "text layer is fine; ingest as is.",
            "partial": "some pages are images only; `ocrmypdf --skip-text in.pdf out.pdf` OCRs just those pages.",
            "scanned": "no usable text: run `ocrmypdf --skip-text in.pdf out.pdf` and ingest the output instead.",
            "garbled": "text is unreadable (font encoding): run `ocrmypdf --force-ocr in.pdf out.pdf`.",
        }
        worst = 0
        for p_ in args.paths:
            files = sorted(Path(p_).rglob("*.pdf")) if Path(p_).is_dir() else [Path(p_)]
            if not files or not all(f.is_file() for f in files):
                print(f"{p_}: no PDF found")
                worst = max(worst, 2)
                continue
            for f in files:
                q = pdf_quality(f)
                print(f"{q['file']}\n  pages: {q['pages']}, without text: {q['pages_without_text']}, "
                      f"garbled: {q['pages_garbled']}, bookmarks: {q['bookmarks']}")
                if q["first_pages_without_text"]:
                    print(f"  pages without text (first): {q['first_pages_without_text']}")
                print(f"  verdict: {q['verdict']} - {advice[q['verdict']]}")
                if not q["bookmarks"]:
                    print("  no bookmarks: it will be indexed page by page (still searchable, coarser titles).")
                if q["sample"]:
                    print(f"  sample: {q['sample'][:160]}...")
                worst = max(worst, 0 if q["verdict"] == "ok" else 1)
        return worst

    if args.cmd == "level":
        from .levels import lookup

        print(lookup(cfg, args.name or None, args.software))
        return 0

    if args.cmd == "lint":
        from .lint import lint

        problems = lint(cfg)
        for p in problems:
            print(p)
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1 if problems else 0

    if args.cmd == "stats":
        from .store import Store, dumps

        print(dumps(Store(cfg.index_path, readonly=True).stats()))
        return 0

    if args.cmd == "sources":
        for s in cfg.sources:
            where = s.url or (f"{len(s.urls)} urls" if s.urls else str(s.path))
            flag = "" if s.enabled else "  (disabled)"
            print(f"{s.name:24s} {s.type:6s} {s.domain or '-':10s} {s.software or '-':10s} {where}{flag}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
