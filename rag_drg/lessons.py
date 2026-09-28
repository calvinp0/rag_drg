"""Lessons learned: the fix for "the agent makes the same mistake every time".

When an agent (or a person) gets corrected, the correction is written as a
small Markdown card under ``knowledge/lessons/<domain>/[<software>/]`` with
``status: unreviewed`` and indexed immediately, so the next agent that
searches the topic sees it. The group reviews new cards through normal
git pull requests and flips them to ``status: verified``; verified lessons
are ranked above everything else in search results.

Before a lesson is written, :func:`find_similar` looks for existing lessons and
curated cards on the same topic; matches are stored as ``similar: [paths]`` in
the new lesson's front matter so reviewers can merge instead of duplicating.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .chunking import chunk_file, split_front_matter
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
    similar: list[str] | None = None,
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
        "similar": list(similar or []),
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


# --------------------------------------------------------------------------- #
# Reading / updating lesson files
# --------------------------------------------------------------------------- #

def read_lesson(path: Path) -> tuple[dict, str]:
    return split_front_matter(Path(path).read_text(errors="replace"))


def iter_lessons(cfg: Config) -> list[Path]:
    root = cfg.lessons_dir
    if not root.exists():
        return []
    return sorted(f for f in root.rglob("*.md") if f.name.lower() != "readme.md")


def repo_rel(cfg: Config, path: Path) -> str:
    """Path as stored in `similar:` lists and shown to reviewers: relative to the config root."""
    path = Path(path)
    return path.relative_to(cfg.root).as_posix() if path.is_relative_to(cfg.root) else str(path)


def lesson_date(path: Path, meta: dict) -> _dt.date | None:
    """`date:` from the front matter, else the YYYY-MM-DD prefix of the file name."""
    raw = meta.get("date")
    if isinstance(raw, _dt.date):
        return raw
    for cand in (str(raw or ""), Path(path).name):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", cand)
        if m:
            try:
                return _dt.date.fromisoformat(m.group(1))
            except ValueError:
                pass
    return None


# --------------------------------------------------------------------------- #
# Near-duplicate detection
# --------------------------------------------------------------------------- #
#
# Two steps. (1) Candidate retrieval with the normal hybrid Searcher (BM25 over
# title + mistake + correction), restricted to lessons and curated cards of the
# same software, so we only score chunks that already rank for the topic.
# (2) A score that is easy to explain to a reviewer, computed per chunk and kept
# as the maximum per file:
#
#     score = 0.4 * overlap + 0.3 * jaccard + 0.3 * identifiers
#
#   overlap     |A & B| / min(|A|, |B|) over content words (stop words removed,
#               plural "s" folded). A lesson is short and a card section is
#               longer; the overlap coefficient does not punish that size
#               difference the way Jaccard does.
#   jaccard     |A & B| / |A | B|, which does punish a chunk that is much broader.
#   identifiers fraction of the new text's identifiers (%maxcore, def2-TZVP,
#               memory,500,m, ts_guess_level, ...) that also occur in the chunk.
#               Sharing exact keywords is the strongest duplicate signal here.
#
# Default threshold 0.35 (configurable as lessons.similar.threshold); see
# tests/test_lessons_workflow.py for the calibration cases.

_WORD = re.compile(r"[A-Za-z0-9]+")
# Words from the lesson template itself ("## Mistake", "## Correct approach", ...).
_TEMPLATE_WORDS = {"mistake", "correct", "approach", "evidence", "source", "lesson", "agent", "wrote", "instead"}


def _content_words(text: str) -> set[str]:
    from .search import _STOP

    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP or w in _TEMPLATE_WORDS or len(w) < 2:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _idents(text: str) -> set[str]:
    from .search import identifiers

    return set(identifiers(text))


def similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two texts (see the comment block above)."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    overlap = inter / min(len(wa), len(wb))
    jaccard = inter / len(wa | wb)
    ia = _idents(a)
    ident = (sum(1 for t in ia if t in b.lower()) / len(ia)) if ia else 0.0
    if not ia:  # no identifiers to compare: rescale the word-based part
        return round((0.4 * overlap + 0.3 * jaccard) / 0.7, 4)
    return round(0.4 * overlap + 0.3 * jaccard + 0.3 * ident, 4)


@dataclass
class Similar:
    path: str  # relative to the config root, e.g. knowledge/ess/orca/orca.md
    score: float
    title: str

    def to_dict(self) -> dict:
        return {"path": self.path, "score": self.score, "title": self.title}


def similar_settings(cfg: Config) -> dict:
    raw = ((cfg.extra.get("lessons") or {}).get("similar") or {})
    return {
        "threshold": float(raw.get("threshold", 0.35)),
        "max": int(raw.get("max", 5)),
        "sources": list(raw.get("sources") or ["curated"]),
    }


def _source_file(cfg: Config, source: str, rel: str) -> Path | None:
    if source == LESSONS_SOURCE:
        return cfg.lessons_dir / rel
    for s in cfg.sources:
        if s.name == source and s.type == "local" and s.path:
            return s.path / rel
    return None


def find_similar(
    cfg: Config,
    searcher,
    text: str,
    software: str | None = None,
    domain: str | None = None,
    exclude: Path | None = None,
    threshold: float | None = None,
) -> list[Similar]:
    """Existing lessons / curated cards that look like a near-duplicate of `text`."""
    opts = similar_settings(cfg)
    threshold = opts["threshold"] if threshold is None else threshold
    filters: dict = {"source": [LESSONS_SOURCE] + opts["sources"]}
    if software:
        filters["software"] = software.lower()
    elif domain:
        filters["domain"] = domain.lower()
    try:
        hits = searcher.search(text, k=30, max_per_file=4, **filters)
    except Exception:  # noqa: BLE001 - an empty/missing index must not block recording
        return []
    best: dict[str, Similar] = {}
    excl = Path(exclude).resolve() if exclude else None
    for h in hits:
        f = _source_file(cfg, h.chunk.source, h.chunk.path)
        if f is None or (excl is not None and f.resolve() == excl):
            continue
        score = similarity(text, f"{h.chunk.title}\n{h.chunk.text}")
        rel = repo_rel(cfg, f)
        if score >= threshold and (rel not in best or score > best[rel].score):
            best[rel] = Similar(rel, score, h.chunk.title.split(" > ")[0])
    return sorted(best.values(), key=lambda s: -s.score)[: opts["max"]]


def lesson_text(title: str, mistake: str = "", correction: str = "") -> str:
    return "\n".join(x for x in (title, mistake, correction) if x)


def similar_for_file(cfg: Config, searcher, path: Path) -> list[Similar]:
    meta, body = read_lesson(path)
    return find_similar(
        cfg, searcher, lesson_text(str(meta.get("title") or ""), body),
        software=meta.get("software"), domain=meta.get("domain"), exclude=path,
    )
