"""Zotero library -> knowledge base (`type: zotero` sources). See docs/zotero.md.

    rag-drg zotero sync [--source NAME] [--full] [--ingest]
    rag-drg zotero status [--source NAME] [--json]

`rag-drg fetch` and `rag-drg ingest --fetch` sync zotero sources too (via ingest.FETCHERS).
The synced files live in the git-ignored `sources_cache/<name>/` and are indexed like a local
folder, with per-file `.meta.yaml` sidecars carrying the bibliographic metadata.
"""

from __future__ import annotations

import argparse
import json
import sys

from rag_drg.ingest import FETCHERS, IGNORED_FILES

from ._zotero.settings import ZoteroError, parse_settings, raw_source_entry
from ._zotero.sync import STATE_FILE, load_state, sync_source

SOURCE_TYPE = "zotero"


def _fetch(src, cfg) -> None:
    sync_source(src, cfg, progress=lambda m: print(m, file=sys.stderr))


FETCHERS[SOURCE_TYPE] = _fetch
IGNORED_FILES.add(STATE_FILE)


def _sources(cfg, names: list[str] | None, include_disabled: bool = False) -> list:
    out = [s for s in cfg.sources if s.type == SOURCE_TYPE]
    if names:
        unknown = set(names) - {s.name for s in out}
        if unknown:
            raise ZoteroError(f"not zotero sources: {sorted(unknown)} (known: {[s.name for s in out]})")
        return [s for s in out if s.name in names]
    return out if include_disabled else [s for s in out if s.enabled]


def register_cli(subparsers) -> dict:
    p = subparsers.add_parser("zotero", help="sync the group's Zotero library (papers, notes) into the index")
    zsub = p.add_subparsers(dest="zotero_cmd", required=True)
    ps = zsub.add_parser("sync", help="download new/changed items, notes and files; remove deleted ones")
    ps.add_argument("--source", action="append", help="zotero source name (default: all enabled ones)")
    ps.add_argument("--full", action="store_true", help="re-read all metadata instead of changes since the last sync")
    ps.add_argument("--ingest", action="store_true", help="re-index the synced sources afterwards")
    pst = zsub.add_parser("status", help="last sync, counts, linked files and failed downloads")
    pst.add_argument("--source", action="append")
    pst.add_argument("--json", action="store_true")
    return {"zotero": _cli}


def _cli(args: argparse.Namespace, cfg) -> int:
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    try:
        if args.zotero_cmd == "sync":
            srcs = _sources(cfg, args.source)
            if not srcs:
                log("No enabled zotero sources (set `enabled: true` in conf.d/zotero.yaml, or pass --source NAME)")
                return 0
            failed = 0
            for src in srcs:
                try:
                    sync_source(src, cfg, full=args.full, progress=log)
                except ZoteroError as e:
                    log(f"[{src.name}] sync failed: {e}")
                    failed += 1
            if args.ingest:
                from rag_drg.ingest import ingest

                ingest(cfg, only=[s.name for s in srcs], progress=log)
            return 1 if failed else 0
        if args.zotero_cmd == "status":
            return _status(cfg, _sources(cfg, args.source, include_disabled=True), args.json)
    except ZoteroError as e:
        log(str(e))
        return 1
    return 2


def _status(cfg, srcs, as_json: bool) -> int:
    rows = []
    for src in srcs:
        settings, problems = parse_settings(raw_source_entry(cfg, src.name), cfg.root)
        st = load_state(src.path) if src.path else {}
        rep = st.get("report") or {}
        rows.append({
            "source": src.name, "enabled": src.enabled, "mode": settings.mode,
            "library": settings.identity, "cache": str(src.path), "config_problems": problems,
            "library_version": st.get("version"), "last_sync": st.get("last_sync"),
            "counts": rep.get("counts") or {}, "linked": rep.get("linked") or [],
            "failed": rep.get("failed") or {}, "unknown_collections": rep.get("unknown_collections") or [],
        })
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No zotero sources configured (see conf.d/zotero.yaml).")
    for r in rows:
        print(f"{r['source']}  ({'enabled' if r['enabled'] else 'disabled'}, {r['mode']})  {r['library']}")
        for p in r["config_problems"]:
            print(f"  config problem: {p}")
        if not r["last_sync"]:
            print("  never synced: run `rag-drg zotero sync --source " + r["source"] + "`")
            continue
        c = r["counts"]
        print(f"  last sync {r['last_sync']}, library version {r['library_version']}")
        print(f"  {c.get('items', 0)} items, {c.get('files', 0)} files, {c.get('notes', 0)} notes, "
              f"{c.get('abstracts', 0)} abstract-only, {c.get('filtered_out', 0)} outside the selected collections")
        if r["unknown_collections"]:
            print(f"  collections not found in the library: {r['unknown_collections']}")
        if r["linked"]:
            print(f"  {len(r['linked'])} linked attachments (not downloadable; store them in Zotero instead):")
            for x in r["linked"][:20]:
                print(f"    {x['item']} {x['title'][:60]!r}: {x['link_mode']} {x['target']}")
        if r["failed"]:
            print(f"  {len(r['failed'])} failed downloads (retried on the next sync):")
            for k, x in list(r["failed"].items())[:20]:
                print(f"    {k} ({x['title'][:60]!r}): {x['error']}")
    return 0


def lint(cfg) -> list[str]:
    problems = []
    for src in cfg.sources:
        if src.type != SOURCE_TYPE:
            continue
        _, errs = parse_settings(raw_source_entry(cfg, src.name), cfg.root)
        if not src.enabled:  # placeholders are fine until enabled; a key in the config never is
            errs = [e for e in errs if "api_key must not" in e]
        problems += [f"source {src.name}: {e}" for e in errs]
    return problems
