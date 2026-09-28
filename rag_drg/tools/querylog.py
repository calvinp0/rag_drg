"""Query log: what agents actually ask the MCP server, and what they got back.

The server emits one event per tool call (see `ServerContext.emit` and `mcp_server.py`); this
plugin appends each one as a JSON line to ``query_log.path`` (default ``index/query_log.jsonl``,
git-ignored). Configure it in ``conf.d/querylog.yaml``::

    query_log: {enabled: true, path: index/query_log.jsonl, max_mb: 20, keep: 1}

Reports turn the log into work for the group:

    rag-drg queries report [--since 7d] [--json]      usage, empty / low-confidence queries, ...
    rag-drg queries to-qa  [--since 30d] [--empty-only] candidate eval/qa.yaml entries

Record format (one JSON object per line)::

    {"ts": "2026-09-28T12:00:00+00:00", "tool": "search_knowledge", "user": "alice" | null,
     "args": {...}, "n_results": 6,
     "results": [{"source", "path", "title", "doc_type", "score"}, ...max 5],
     "path": "knowledge/lessons/..."   # record_lesson only}
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger(__name__)

DEFAULT_PATH = "index/query_log.jsonl"
DEFAULT_MAX_MB = 20.0
MAX_RESULTS = 5
RESULT_KEYS = ("source", "path", "title", "doc_type", "score")
# Sources that hold group-reviewed knowledge; a top hit from anywhere else means the answer
# came from a raw manual/repo and may be worth a card.
DEFAULT_CURATED_SOURCES = ("curated", "lessons")
# Search scores are reciprocal-rank-fusion sums, not relevance: 1/(60 + rank + 1) per ranked list
# (keyword, identifier phrase, embeddings), times the doc-type/status boosts in search.py. A top
# hit that is first in the keyword list alone scores 1/61 = 0.0164 (x0.95 for manual code/theory
# chunks, x1.25-1.35 for curated cards). A second agreeing list (exact identifier phrase or
# embeddings) or an exact-identifier match lifts it to >= ~0.025. Measured on this index
# (keyword only, 2026-09): 7 of 8 off-topic or unanswerable probes ("ORCA broken symmetry" -> a PySCF
# example, "gaussian counterpoise correction" -> a Psi4 tutorial) topped out at 0.0156-0.0197, while
# 42 of the 51 eval/qa.yaml questions score >= 0.020. Below 0.02 therefore means "one loose
# keyword match on a non-curated chunk": worth a look. Tune it with `query_log.low_score` or
# `--low-score`, and re-measure when embeddings are enabled (scores rise when lists agree).
DEFAULT_LOW_SCORE = 0.02


# ---------------------------------------------------------------------------- config


def log_settings(cfg) -> dict:
    raw = (cfg.extra or {}).get("query_log") or {}
    path = Path(os.path.expandvars(os.path.expanduser(str(raw.get("path") or DEFAULT_PATH))))
    if not path.is_absolute():
        path = cfg.root / path
    return {
        "enabled": bool(raw.get("enabled", True)),
        "path": path,
        "max_bytes": int(float(raw.get("max_mb", DEFAULT_MAX_MB)) * 1024 * 1024),
        "keep": max(1, int(raw.get("keep", 1))),
        "low_score": float(raw.get("low_score", DEFAULT_LOW_SCORE)),
        "curated_sources": list(raw.get("curated_sources") or DEFAULT_CURATED_SOURCES),
    }


# ---------------------------------------------------------------------------- writer


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def make_record(event: dict) -> dict:
    results = []
    for r in (event.get("results") or [])[:MAX_RESULTS]:
        results.append({k: r.get(k) for k in RESULT_KEYS})
    rec = {
        "ts": _utcnow(),
        "tool": event.get("tool"),
        "user": event.get("user"),
        "args": event.get("args") or {},
        "n_results": event.get("n_results", len(event.get("results") or [])),
        "results": results,
    }
    if event.get("path"):
        rec["path"] = str(event["path"])
    return rec


class QueryLogger:
    """Appends records as JSON lines; rotates `path` -> `path.1` (-> `.2` ...) past `max_bytes`."""

    def __init__(self, path: str | Path, max_bytes: int = int(DEFAULT_MAX_MB * 1024 * 1024), keep: int = 1):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        self.write(make_record(event))

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and self.max_bytes > 0 and size + len(data) > self.max_bytes:
                self._rotate()
            # One write() of the whole line in append mode, so lines never interleave.
            with open(self.path, "ab") as f:
                f.write(data)

    def _rotate(self) -> None:
        for i in range(self.keep, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            dst = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, dst)


def register_mcp(mcp, ctx) -> None:
    s = log_settings(ctx.cfg)
    if not s["enabled"]:
        return
    logger = QueryLogger(s["path"], s["max_bytes"], s["keep"])
    ctx.subscribe(logger)
    log.info("query log: %s", s["path"])


# ---------------------------------------------------------------------------- reading


def parse_since(value: str | None, now: _dt.datetime | None = None) -> _dt.datetime | None:
    """'7d', '12h', '2w', '30m' or an ISO date/datetime -> UTC datetime; None/'all' -> None."""
    if not value or value == "all":
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", value)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        return now - _dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]: n})
    try:
        t = _dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"--since {value!r}: use e.g. 7d, 24h, 2w or 2026-09-01") from None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def _ts(rec: dict) -> _dt.datetime | None:
    try:
        t = _dt.datetime.fromisoformat(str(rec.get("ts")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def log_files(path: Path) -> list[Path]:
    """Rotated files first (oldest first), then the live file."""
    rotated = [p for p in path.parent.glob(path.name + ".*") if p.suffix[1:].isdigit()]
    rotated.sort(key=lambda p: -int(p.suffix[1:]))
    return rotated + ([path] if path.exists() else [])


def read_records(path: Path, since: _dt.datetime | None = None) -> list[dict]:
    out = []
    for f in log_files(Path(path)):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut by a crash; skip
                t = _ts(rec)
                if since is not None and (t is None or t < since):
                    continue
                out.append(rec)
    return out


# ---------------------------------------------------------------------------- report


def normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _filters_of(args: dict) -> dict:
    return {k: args[k] for k in ("software", "version", "domain", "doc_type") if args.get(k)}


def build_report(records: Iterable[dict], low_score: float = DEFAULT_LOW_SCORE,
                 curated_sources: Iterable[str] = DEFAULT_CURATED_SOURCES, top: int = 20) -> dict:
    curated = set(curated_sources)
    records = list(records)
    by_tool, by_day, by_user, by_software = Counter(), Counter(), Counter(), Counter()
    queries: dict[str, dict] = {}
    empty: dict[str, dict] = {}
    low: dict[str, dict] = {}
    docs = Counter()
    lessons = []
    for rec in records:
        tool = rec.get("tool") or "?"
        args = rec.get("args") or {}
        by_tool[tool] += 1
        by_day[str(rec.get("ts", ""))[:10]] += 1
        by_user[rec.get("user") or "(anonymous)"] += 1
        if tool == "record_lesson":
            lessons.append({"ts": rec.get("ts"), "user": rec.get("user"), "title": args.get("title"),
                            "domain": args.get("domain"), "software": args.get("software"),
                            "path": rec.get("path")})
            continue
        if tool == "search_knowledge":
            by_software[args.get("software") or "(no filter)"] += 1
            q = args.get("query") or ""
        elif tool == "lookup_level_of_theory":
            q = args.get("name") or ""
        else:
            continue
        key = f"{tool}\t{normalize_query(q)}"
        entry = queries.setdefault(key, {"tool": tool, "query": q, "count": 0, "filters": _filters_of(args),
                                         "users": set()})
        entry["count"] += 1
        entry["users"].add(rec.get("user") or "(anonymous)")
        results = rec.get("results") or []
        for r in results:
            docs[f"{r.get('source')}:{r.get('path')}"] += 1
        n = rec.get("n_results")
        if n is None:
            n = len(results)
        if n == 0:
            e = empty.setdefault(key, {"tool": tool, "query": q, "count": 0, "filters": _filters_of(args)})
            e["count"] += 1
            continue
        if tool != "search_knowledge" or not results:
            continue
        top_hit = results[0]
        reasons = []
        score = top_hit.get("score")
        if isinstance(score, (int, float)) and score < low_score:
            reasons.append(f"top score {score:.4f} < {low_score}")
        if top_hit.get("source") not in curated:
            reasons.append("top result not curated")
        if reasons:
            e = low.setdefault(key, {"tool": tool, "query": q, "count": 0, "filters": _filters_of(args),
                                     "reasons": reasons, "top": f"{top_hit.get('source')}:{top_hit.get('path')}",
                                     "top_title": top_hit.get("title"), "score": score})
            e["count"] += 1

    def ranked(d: dict) -> list[dict]:
        rows = sorted(d.values(), key=lambda e: (-e["count"], e["query"].lower()))
        for e in rows:
            if isinstance(e.get("users"), set):
                e["users"] = sorted(e["users"])
        return rows

    return {
        "n_events": len(records),
        "first": min((str(r.get("ts")) for r in records), default=None),
        "last": max((str(r.get("ts")) for r in records), default=None),
        "by_tool": dict(by_tool.most_common()),
        "by_day": dict(sorted(by_day.items())),
        "by_user": dict(by_user.most_common()),
        "by_software_filter": dict(by_software.most_common()),
        "top_queries": ranked(queries)[:top],
        "empty_queries": ranked(empty),
        "low_confidence": ranked(low)[: top * 2],
        "low_score_threshold": low_score,
        "top_documents": [{"doc": d, "count": c} for d, c in docs.most_common(top)],
        "lessons": lessons,
    }


def _fmt_filters(f: dict) -> str:
    return (" [" + ", ".join(f"{k}={v}" for k, v in f.items()) + "]") if f else ""


def format_report(rep: dict) -> str:
    L = [f"{rep['n_events']} event(s) from {rep['first'] or '-'} to {rep['last'] or '-'}", ""]

    def counts(title: str, d: dict):
        L.append(title)
        L.extend(f"  {v:6d}  {k}" for k, v in d.items())
        if not d:
            L.append("  (none)")
        L.append("")

    counts("Per tool:", rep["by_tool"])
    counts("Per day (UTC):", rep["by_day"])
    counts("Per user:", rep["by_user"])
    counts("search_knowledge per software filter:", rep["by_software_filter"])
    L.append("Top queries:")
    L.extend(f"  {e['count']:6d}  {e['query']}{_fmt_filters(e['filters'])}"
             + ("" if e["tool"] == "search_knowledge" else f"  ({e['tool']})") for e in rep["top_queries"])
    if not rep["top_queries"]:
        L.append("  (none)")
    L.append("")
    L.append("EMPTY results (nothing in the index; candidates for new cards):")
    L.extend(f"  {e['count']:6d}  {e['query']}{_fmt_filters(e['filters'])}  ({e['tool']})" for e in rep["empty_queries"])
    if not rep["empty_queries"]:
        L.append("  (none)")
    L.append("")
    L.append(f"Low confidence (top score < {rep['low_score_threshold']} or top result not curated):")
    for e in rep["low_confidence"]:
        L.append(f"  {e['count']:6d}  {e['query']}{_fmt_filters(e['filters'])}")
        L.append(f"          -> {e['top']} ({e['score']}); {'; '.join(e['reasons'])}")
    if not rep["low_confidence"]:
        L.append("  (none)")
    L.append("")
    L.append("Most returned documents:")
    L.extend(f"  {e['count']:6d}  {e['doc']}" for e in rep["top_documents"])
    if not rep["top_documents"]:
        L.append("  (none)")
    L.append("")
    L.append("record_lesson events (review these PRs):")
    L.extend(f"  {e['ts']}  {e['user'] or '(anonymous)'}  {e['title']}  -> {e['path']}" for e in rep["lessons"])
    if not rep["lessons"]:
        L.append("  (none)")
    return "\n".join(L)


# ---------------------------------------------------------------------------- to-qa


def _slug(text: str, n: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "q"


def to_qa(records: Iterable[dict], empty_only: bool = False, existing: Iterable[str] = (),
          existing_ids: Iterable[str] = ()) -> str:
    """Candidate eval/qa.yaml entries (expect: [TODO]) for search_knowledge queries in the log."""
    seen_q = {normalize_query(q) for q in existing}
    ids = set(existing_ids)
    groups: dict[str, dict] = {}
    for rec in records:
        if rec.get("tool") != "search_knowledge":
            continue
        args = rec.get("args") or {}
        q = (args.get("query") or "").strip()
        nq = normalize_query(q)
        if not nq or nq in seen_q:
            continue
        g = groups.setdefault(nq, {"q": q, "count": 0, "empty": 0, "filters": _filters_of(args), "top": None})
        g["count"] += 1
        results = rec.get("results") or []
        if (rec.get("n_results") if rec.get("n_results") is not None else len(results)) == 0:
            g["empty"] += 1
        elif results and g["top"] is None:
            g["top"] = f"{results[0].get('source')}:{results[0].get('path')}"
    rows = [g for g in groups.values() if g["empty"] or not empty_only]
    rows.sort(key=lambda g: (-g["empty"], -g["count"], g["q"].lower()))
    out = [
        "# Candidate eval/qa.yaml entries from the query log (rag-drg queries to-qa).",
        "# For each one worth keeping: replace TODO with the document that answers it",
        "# (\"source:path\"), write the card first if nothing answers it, then paste it into eval/qa.yaml.",
        "",
    ]
    if not rows:
        out.append("[]")
        return "\n".join(out) + "\n"
    for g in rows:
        base = iid = _slug(g["q"])
        i = 2
        while iid in ids:
            iid = f"{base}-{i}"
            i += 1
        ids.add(iid)
        note = f"asked {g['count']}x" + (f", {g['empty']}x with no results" if g["empty"] else "")
        if g["top"]:
            note += f"; top result was {g['top']}"
        item: dict[str, Any] = {"id": iid, "q": g["q"]}
        if g["filters"]:
            item["filters"] = g["filters"]
        item["expect"] = ["TODO"]
        item["tags"] = ["from-query-log"] + (["empty"] if g["empty"] else [])
        out.append(f"# {note}")
        out.append(yaml.safe_dump([item], sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None).rstrip())
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------- CLI


def register_cli(subparsers) -> dict:
    p = subparsers.add_parser("queries", help="reports on the MCP query log (what agents ask)")
    qs = p.add_subparsers(dest="queries_cmd", required=True)
    r = qs.add_parser("report", help="usage, empty and low-confidence queries, top documents, lessons")
    r.add_argument("--since", default="7d", help="7d, 24h, 2w, an ISO date, or 'all' (default 7d)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--low-score", type=float, help=f"low-confidence threshold (default {DEFAULT_LOW_SCORE})")
    r.add_argument("--top", type=int, default=20, help="rows in the top-N lists")
    r.add_argument("--log", help="log file (default: query_log.path from the config)")
    t = qs.add_parser("to-qa", help="print candidate eval/qa.yaml entries for queries in the log")
    t.add_argument("--since", default="30d")
    t.add_argument("--empty-only", action="store_true", help="only queries that returned nothing")
    t.add_argument("--log", help="log file (default: query_log.path from the config)")
    return {"queries": cmd_queries}


def cmd_queries(args: argparse.Namespace, cfg) -> int:
    s = log_settings(cfg)
    path = Path(args.log) if getattr(args, "log", None) else s["path"]
    try:
        since = parse_since(args.since)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not log_files(path):
        print(f"No query log at {path} (is query_log enabled on the server?)", file=sys.stderr)
    records = read_records(path, since)
    if args.queries_cmd == "report":
        rep = build_report(records, low_score=args.low_score if args.low_score is not None else s["low_score"],
                           curated_sources=s["curated_sources"], top=args.top)
        rep["log"] = str(path)
        rep["since"] = since.isoformat() if since else None
        print(json.dumps(rep, indent=2, default=str) if args.json else format_report(rep))
        return 0
    if args.queries_cmd == "to-qa":
        existing_q, existing_ids = [], []
        try:
            from .evaluation import load_items, qa_path

            for it in load_items(qa_path(cfg)):
                existing_q.append(it.q)
                existing_ids.append(it.id)
        except (OSError, ValueError, yaml.YAMLError):
            pass
        print(to_qa(records, empty_only=args.empty_only, existing=existing_q, existing_ids=existing_ids), end="")
        return 0
    return 1
