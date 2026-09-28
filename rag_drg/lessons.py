"""Lessons learned: the fix for "the agent makes the same mistake every time".

When an agent (or a person) gets corrected, the correction is written as a
small Markdown card under ``knowledge/lessons/<domain>/[<software>/]`` with
``status: unreviewed`` and indexed immediately, so the next agent that
searches the topic sees it. The group reviews new cards through normal
git pull requests and flips them to ``status: verified``; verified lessons
are ranked above everything else in search results.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import yaml

from .chunking import chunk_file
from .config import Config, SourceConfig

LESSONS_SOURCE = "lessons"


def lessons_source(cfg: Config) -> SourceConfig:
    return SourceConfig(
        name=LESSONS_SOURCE, type="local", path=cfg.lessons_dir,
        include=["**/*.md"], doc_type="lesson",
    )


def _slug(text: str, n: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "lesson"


def write_lesson(
    cfg: Config,
    title: str,
    mistake: str,
    correction: str,
    domain: str,
    software: str | None = None,
    version: str | None = None,
    evidence: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
) -> Path:
    domain = _slug(domain, 30)
    folder = cfg.lessons_dir / domain
    if software:
        folder = folder / _slug(software, 30)
    folder.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    path = folder / f"{today}-{_slug(title)}.md"
    i = 2
    while path.exists():
        path = folder / f"{today}-{_slug(title)}-{i}.md"
        i += 1

    meta = {
        "title": title.strip(),
        "domain": domain,
        "software": software.lower() if software else None,
        "version": version,
        "doc_type": "lesson",
        "status": "unreviewed",
        "tags": tags or [],
        "author": author,
        "date": today,
    }
    meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
    body = [f"# {title.strip()}", "", "## Mistake", "", mistake.strip(), "", "## Correct approach", "", correction.strip(), ""]
    if evidence:
        body += ["## Evidence / source", "", evidence.strip(), ""]
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + "\n".join(body))
    return path


def index_lesson(cfg: Config, store, path: Path) -> int:
    """Index a single lesson file right away (without a full re-ingest)."""
    from .ingest import _versions  # local import to avoid a cycle

    src = lessons_source(cfg)
    rel = path.relative_to(cfg.lessons_dir).as_posix()
    meta, chunks = chunk_file(path, rel, cfg.chunk_size, cfg.chunk_overlap)
    for c in chunks:
        c.source = src.name
        c.domain = meta.get("domain")
        c.software = meta.get("software")
        c.version = _versions(meta.get("version"))
        c.doc_type = "lesson"
        c.tags = [str(t) for t in meta.get("tags") or []]
        c.status = meta.get("status")
    store.upsert_chunks(src.name, rel, chunks)
    return len(chunks)
