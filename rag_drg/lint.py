"""Validate curated cards and lessons so bad metadata never reaches the index."""

from __future__ import annotations

from pathlib import Path

from .chunking import split_front_matter
from .config import Config

DOMAINS = {"ess", "arc", "hpc", "project", "literature"}
DOC_TYPES = {"card", "gotcha", "template", "schema", "lesson", "reference", "paper", "scaffold"}
STATUSES = {"draft", "unreviewed", "verified", "outdated"}
LESSON_SECTIONS = ("## Mistake", "## Correct approach")


def lint(cfg: Config) -> list[str]:
    roots: list[Path] = [s.path for s in cfg.sources if s.type == "local" and s.name == "curated" and s.path]
    roots.append(cfg.lessons_dir)
    problems: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*.md")):
            if f in seen or (f.name.lower() == "readme.md" and f.parent in roots):
                continue
            seen.add(f)
            rel = f.relative_to(cfg.root) if f.is_relative_to(cfg.root) else f
            meta, body = split_front_matter(f.read_text(errors="replace"))
            if not meta:
                problems.append(f"{rel}: missing YAML front matter")
                continue
            if not meta.get("title"):
                problems.append(f"{rel}: missing 'title'")
            if meta.get("domain") not in DOMAINS:
                problems.append(f"{rel}: domain {meta.get('domain')!r} not in {sorted(DOMAINS)}")
            doc_type = meta.get("doc_type")
            if doc_type not in DOC_TYPES:
                problems.append(f"{rel}: doc_type {doc_type!r} not in {sorted(DOC_TYPES)}")
            if meta.get("status") not in STATUSES:
                problems.append(f"{rel}: status {meta.get('status')!r} not in {sorted(STATUSES)}")
            if meta.get("domain") == "ess" and not meta.get("software") and "capabilit" not in f.name:
                problems.append(f"{rel}: ESS cards need 'software'")
            if meta.get("tags") is not None and not isinstance(meta.get("tags"), list):
                problems.append(f"{rel}: 'tags' must be a list")
            is_lesson = doc_type == "lesson" or f.is_relative_to(cfg.lessons_dir)
            if is_lesson and not all(s in body for s in LESSON_SECTIONS):
                problems.append(f"{rel}: lessons need '## Mistake' and '## Correct approach' sections")
            if not body.strip():
                problems.append(f"{rel}: empty body")
    return problems
