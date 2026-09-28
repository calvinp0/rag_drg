"""Retrieval evaluation: does search put the right document on top for real questions?

    rag-drg eval [--qa eval/qa.yaml] [--k 6] [--min-hit-rate 0.8] [--tags t1 t2] [--ids a b]
                 [--json] [--show-failures]

`eval/qa.yaml` is a list of items::

    - id: orca-maxcore
      q: "Is ORCA %maxcore total memory or per core?"
      filters: {software: orca}                      # optional: software/version/domain/doc_type/source
      expect: ["curated:ess/orca/orca-essentials.md"] # any of these; see below
      requires: [curated]                             # optional; default = sources named in `expect`
      tags: [memory]

An `expect` entry is either a string ``"<source>:<path prefix>"`` (``"arc:examples/"`` matches
any ARC example) or a mapping with any of ``source``, ``path`` (prefix) and ``title_contains``
(case-insensitive), all of which must match. An item-level ``title_contains: "..."`` is a
shortcut for one more expect entry. A hit counts if *any* expect entry matches a result.

Items are skipped (not failed) when a required source is not in the index (e.g. CI indexes only
the curated cards) or when `expect` still contains the placeholder ``TODO``.

Metrics: hit@1, hit@k (fraction of items with a matching result in the top k) and MRR (mean of
1/rank of the first matching result, 0 when none), overall and per tag.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FILTER_KEYS = ("software", "version", "domain", "doc_type", "source")
DEFAULT_QA = "eval/qa.yaml"


# --------------------------------------------------------------------------- data


@dataclass
class QAItem:
    id: str
    q: str
    expect: list[dict]
    filters: dict = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    todo: bool = False


def _parse_expect(entry: Any) -> dict:
    if isinstance(entry, str):
        if entry.strip().upper() == "TODO":
            return {"todo": True}
        src, sep, path = entry.partition(":")
        if not sep:
            raise ValueError(f"expect entry {entry!r} must look like 'source:path/prefix'")
        return {"source": src.strip(), "path": path.strip()}
    if isinstance(entry, dict):
        unknown = set(entry) - {"source", "path", "title_contains"}
        if unknown or not entry:
            raise ValueError(f"expect entry {entry!r}: use source / path / title_contains")
        return {k: str(v) for k, v in entry.items()}
    raise ValueError(f"expect entry {entry!r} must be a string or a mapping")


def parse_items(raw: Any) -> list[QAItem]:
    """Validate and convert the YAML list; raises ValueError with the item id on problems."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("qa file must contain a YAML list of items")
    items: list[QAItem] = []
    seen: set[str] = set()
    for i, d in enumerate(raw):
        if not isinstance(d, dict):
            raise ValueError(f"item #{i + 1} is not a mapping")
        iid = str(d.get("id") or f"item-{i + 1}")
        if iid in seen:
            raise ValueError(f"duplicate id {iid!r}")
        seen.add(iid)
        q = d.get("q")
        if not q or not isinstance(q, str):
            raise ValueError(f"{iid}: missing question 'q'")
        try:
            expect = [_parse_expect(e) for e in (d.get("expect") or [])]
        except ValueError as e:
            raise ValueError(f"{iid}: {e}") from None
        if d.get("title_contains"):
            expect.append({"title_contains": str(d["title_contains"])})
        if not expect:
            raise ValueError(f"{iid}: needs 'expect' and/or 'title_contains'")
        filters = d.get("filters") or {}
        if not isinstance(filters, dict) or set(filters) - set(FILTER_KEYS):
            raise ValueError(f"{iid}: filters may only use {', '.join(FILTER_KEYS)}")
        todo = any(e.get("todo") for e in expect)
        expect = [e for e in expect if not e.get("todo")]
        requires = d.get("requires")
        if requires is None:
            requires = sorted({e["source"] for e in expect if e.get("source")})
        items.append(QAItem(
            id=iid, q=q.strip(), expect=expect,
            filters={k: (str(v) if k == "version" else v) for k, v in filters.items() if v is not None},
            requires=[str(r) for r in (requires if isinstance(requires, list) else [requires])],
            tags=[str(t) for t in (d.get("tags") or [])], todo=todo,
        ))
    return items


def load_items(path: str | Path) -> list[QAItem]:
    return parse_items(yaml.safe_load(Path(path).read_text()))


def qa_path(cfg, explicit: str | None = None) -> Path:
    """`--qa` is relative to the working directory; the default (or `eval: {qa: ...}` in the
    config) is relative to the config file's directory."""
    if explicit:
        return Path(explicit).resolve()
    p = Path((cfg.extra.get("eval") or {}).get("qa") or DEFAULT_QA)
    return p if p.is_absolute() else cfg.root / p


# ------------------------------------------------------------------------ matching


def matches(chunk, expect: dict) -> bool:
    if "source" in expect and chunk.source != expect["source"]:
        return False
    if "path" in expect and not chunk.path.startswith(expect["path"]):
        return False
    if "title_contains" in expect and expect["title_contains"].lower() not in (chunk.title or "").lower():
        return False
    return True


def first_match_rank(hits, expect: list[dict]) -> int | None:
    """1-based rank of the first hit that matches any expect entry."""
    for rank, h in enumerate(hits, 1):
        if any(matches(h.chunk, e) for e in expect):
            return rank
    return None


# ------------------------------------------------------------------------- running


def _metrics(ranks: list[int | None], k: int) -> dict:
    n = len(ranks)
    if not n:
        return {"n": 0, "hit@1": None, f"hit@{k}": None, "mrr": None}
    return {
        "n": n,
        "hit@1": round(sum(1 for r in ranks if r == 1) / n, 4),
        f"hit@{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 4),
        "mrr": round(sum(1.0 / r for r in ranks if r) / n, 4),
    }


def run_eval(searcher, items: list[QAItem], k: int = 6, indexed_sources: set[str] | None = None) -> dict:
    """Search every item, return per-item results plus overall and per-tag metrics."""
    if indexed_sources is None:
        indexed_sources = set(searcher.store.stats()["by_source"])
    results, skipped = [], []
    for it in items:
        if it.todo:
            skipped.append({"id": it.id, "reason": "expect is TODO (not curated yet)"})
            continue
        missing = [s for s in it.requires if s not in indexed_sources]
        if missing:
            skipped.append({"id": it.id, "reason": f"source(s) not indexed: {', '.join(missing)}"})
            continue
        hits = searcher.search(it.q, k=k, **it.filters)
        rank = first_match_rank(hits, it.expect)
        results.append({
            "id": it.id, "q": it.q, "tags": it.tags, "filters": it.filters, "rank": rank,
            "hit": rank is not None,
            "top": [{"source": h.chunk.source, "path": h.chunk.path, "title": h.chunk.title,
                     "doc_type": h.chunk.doc_type, "score": round(h.score, 5)} for h in hits[:3]],
            "expect": it.expect,
        })
    by_tag: dict[str, list] = {}
    for r in results:
        for t in r["tags"] or ["(untagged)"]:
            by_tag.setdefault(t, []).append(r["rank"])
    return {
        "k": k,
        "overall": _metrics([r["rank"] for r in results], k),
        "by_tag": {t: _metrics(v, k) for t, v in sorted(by_tag.items())},
        "results": results,
        "skipped": skipped,
    }


def _fmt(v) -> str:
    return "  -  " if v is None else f"{v:5.3f}"


def format_report(rep: dict, show_failures: bool = False) -> str:
    k = rep["k"]
    o = rep["overall"]
    lines = [f"Evaluated {o['n']} item(s), skipped {len(rep['skipped'])}.", ""]
    lines.append(f"{'':24s} {'n':>4s}  {'hit@1':>5s}  {f'hit@{k}':>5s}  {'MRR':>5s}")
    lines.append(f"{'OVERALL':24s} {o['n']:4d}  {_fmt(o['hit@1'])}  {_fmt(o[f'hit@{k}'])}  {_fmt(o['mrr'])}")
    for t, m in rep["by_tag"].items():
        lines.append(f"  {t[:22]:22s} {m['n']:4d}  {_fmt(m['hit@1'])}  {_fmt(m[f'hit@{k}'])}  {_fmt(m['mrr'])}")
    failures = [r for r in rep["results"] if not r["hit"]]
    if failures:
        lines += ["", f"Failures (no expected document in the top {k}):"]
        for r in failures:
            lines.append(f"  - {r['id']}: {r['q']}")
            if show_failures:
                want = ", ".join(
                    ":".join(x for x in (e.get("source"), e.get("path")) if x)
                    + (f" [title~{e['title_contains']!r}]" if e.get("title_contains") else "")
                    for e in r["expect"]
                )
                lines.append(f"      expected: {want}")
                for i, t in enumerate(r["top"], 1):
                    lines.append(f"      {i}. {t['source']}:{t['path']}  ({t['doc_type']}, {t['score']})  {t['title'][:70]}")
                if not r["top"]:
                    lines.append("      (no results)")
    late = [r for r in rep["results"] if r["hit"] and r["rank"] > 1]
    if late and show_failures:
        lines += ["", "Found, but not at rank 1:"]
        lines += [f"  - {r['id']}: rank {r['rank']}" for r in late]
    if rep["skipped"]:
        lines += ["", "Skipped:"]
        lines += [f"  - {s['id']}: {s['reason']}" for s in rep["skipped"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- CLI


def register_cli(subparsers) -> dict:
    p = subparsers.add_parser(
        "eval", help="measure retrieval quality on the question set in eval/qa.yaml",
        description="Run every question in eval/qa.yaml through search and report hit@1, hit@k and MRR.",
    )
    p.add_argument("--qa", help=f"question file (default: {DEFAULT_QA} next to rag_drg.yaml)")
    p.add_argument("--k", type=int, default=6, help="results per query, as agents get them (default 6)")
    p.add_argument("--min-hit-rate", type=float, default=0.8, help="exit 1 if hit@k is below this (default 0.8)")
    p.add_argument("--tags", nargs="+", help="only items carrying any of these tags")
    p.add_argument("--ids", nargs="+", help="only these item ids")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.add_argument("--show-failures", action="store_true", help="show the top-3 results of each failure")
    return {"eval": cmd_eval}


def cmd_eval(args: argparse.Namespace, cfg) -> int:
    from ..search import Searcher

    path = qa_path(cfg, args.qa)
    try:
        items = load_items(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"Cannot read {path}: {e}", file=sys.stderr)
        return 2
    if args.tags:
        items = [it for it in items if set(it.tags) & set(args.tags)]
    if args.ids:
        items = [it for it in items if it.id in set(args.ids)]
    try:
        searcher = Searcher(cfg)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    rep = run_eval(searcher, items, k=args.k)
    rep["qa"] = str(path)
    rep["embeddings"] = searcher.embedder.name if searcher.embedder is not None else None
    rep["min_hit_rate"] = args.min_hit_rate
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"{path} | k={args.k} | ranking: "
              + (f"keyword + embeddings ({rep['embeddings']})" if rep["embeddings"] else "keyword only"))
        print(format_report(rep, show_failures=args.show_failures))
    n = rep["overall"]["n"]
    if n == 0:
        print("No item was evaluated (all skipped or filtered out).", file=sys.stderr)
        return 1
    hit_k = rep["overall"][f"hit@{args.k}"]
    if hit_k < args.min_hit_rate:
        print(f"FAIL: hit@{args.k} = {hit_k:.3f} < {args.min_hit_rate}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------- lint


def lint(cfg) -> list[str]:
    path = qa_path(cfg)
    if not path.is_file():
        return []
    rel = path.relative_to(cfg.root) if path.is_relative_to(cfg.root) else path
    try:
        load_items(path)
    except (ValueError, yaml.YAMLError) as e:
        return [f"{rel}: {e}"]
    return []
