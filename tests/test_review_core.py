"""Regression tests for the core review findings: lesson validation, conf.d merging, SQLite
concurrency, `lessons tidy`, the `lessons` pseudo-domain and metadata-aware dedupe."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml

from rag_drg.cli import main as cli_main
from rag_drg.config import load_config
from rag_drg.ingest import chunks_for_source, ingest
from rag_drg.lessons import LESSONS_SOURCE, index_lesson, iter_lessons, write_lesson
from rag_drg.lint import lint
from rag_drg.search import Searcher
from rag_drg.store import Store
from rag_drg.tools import lessons_workflow as wf

from .test_lessons_workflow import ORCA_DUP, configure, git, make_repo, quiet, server_and_call

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "test@example.org")
    monkeypatch.setenv("RAG_DRG_AUTHOR", "alice")


# --------------------------------------------------------------------------- 1. lesson validation

@pytest.mark.parametrize("domain,software", [("chemistry", "orca"), ("lessons", None), ("ess", None), ("ess", "")])
def test_write_lesson_rejects_what_lint_would_reject(project, domain, software):
    with pytest.raises(ValueError, match="lesson not recorded"):
        write_lesson(project, **{**ORCA_DUP, "domain": domain, "software": software})
    assert iter_lessons(project) == []


def test_valid_lesson_passes_lint(project):
    write_lesson(project, **{**ORCA_DUP, "domain": "HPC", "software": None})
    write_lesson(project, **ORCA_DUP)
    assert [p for p in lint(project) if p.startswith("knowledge/lessons")] == []


def test_record_lesson_returns_validation_error(project):
    ingest(project, progress=quiet)
    _, call = server_and_call(project)
    out = call("record_lesson", **{**ORCA_DUP, "software": None})
    assert out.startswith("error:") and "software" in out
    out = call("record_lesson", **{**ORCA_DUP, "domain": "misc"})
    assert out.startswith("error:") and "'misc'" in out
    assert iter_lessons(project) == []
    assert [p for p in lint(project) if p.startswith("knowledge/lessons")] == []


def test_cli_lesson_rejects_bad_domain(project, capsys):
    code = cli_main(["-c", str(project.root / "rag_drg.yaml"), "lesson", "--title", "t", "--mistake", "m",
                     "--correction", "c", "--domain", "ess"])
    assert code != 0
    assert "software" in capsys.readouterr().err
    assert iter_lessons(project) == []


def test_refresh_script_does_not_abort_on_lint_or_tidy():
    lines = [ln.strip() for ln in (ROOT / "deploy" / "refresh.sh").read_text().splitlines()]
    assert "set -euo pipefail" in lines
    for cmd in ('"$RAG" lint', '"$RAG" lessons tidy'):
        line = next(ln for ln in lines if ln.startswith(cmd))
        assert "||" in line, line
    assert lines.index(next(ln for ln in lines if ln.startswith('"$RAG" lint'))) < \
        lines.index(next(ln for ln in lines if ln.startswith('"$RAG" ingest')))


# --------------------------------------------------------------------------- 2. conf.d merging

def test_conf_d_local_overrides_win_and_merge_deeply(project):
    d = project.root / "conf.d"
    d.mkdir()
    (d / "lessons.yaml").write_text(yaml.safe_dump(
        {"lessons": {"pr": {"mode": "none", "remote": "origin", "base": "main", "labels": ["a", "b"]},
                     "similar": {"threshold": 0.35}}}))
    # Sorts before lessons.yaml, but is a per-machine override: must be merged last.
    (d / "lessons.local.yaml").write_text(yaml.safe_dump({"lessons": {"pr": {"mode": "branch"}}}))
    (d / "zz-local.yaml").write_text(yaml.safe_dump({"lessons": {"pr": {"base": "develop", "labels": ["c"]}}}))
    (d / "extra.yaml").write_text(yaml.safe_dump({"sources": [{"name": "more", "type": "local", "path": "more"}]}))
    cfg = load_config(project.root / "rag_drg.yaml")
    pr = cfg.extra["lessons"]["pr"]
    assert pr == {"mode": "branch", "remote": "origin", "base": "develop", "labels": ["c"]}  # lists replace
    assert cfg.extra["lessons"]["similar"] == {"threshold": 0.35}
    assert [s.name for s in cfg.sources] == ["curated", "manual", "more"]  # sources still appended


def test_local_overrides_are_git_ignored():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "conf.d/*.local.yaml" in ignore and "conf.d/zz-local.yaml" in ignore


# --------------------------------------------------------------------------- 3. SQLite concurrency

def _hold_exclusive(path: Path, started: threading.Event, release: threading.Event):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("DELETE FROM meta WHERE key='nothing'")
    started.set()
    release.wait(20)
    conn.rollback()
    conn.close()


def test_reader_and_new_writer_not_blocked_by_running_ingest(project):
    ingest(project, progress=quiet)
    st = Store(project.index_path)
    assert st.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    searcher = Searcher(project, store=Store(project.index_path, readonly=True), embedder=False)
    started, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_exclusive, args=(project.index_path, started, release))
    t.start()
    try:
        assert started.wait(10)
        t0 = time.monotonic()
        hits = searcher.search("maxcore memory per core")
        assert hits, "search during a write transaction returned nothing"
        other = Store(project.index_path)  # opening a writable store must not need the write lock
        assert other.get_meta("x") is None
        other.close()
        assert time.monotonic() - t0 < 5
    finally:
        release.set()
        t.join()
    st.close()


def test_fts_search_raises_lock_errors_but_not_syntax_errors(project):
    ingest(project, progress=quiet)
    st = Store(project.index_path, readonly=True)
    assert st.fts_search('"unterminated', 5) == []  # malformed MATCH -> no keyword hits

    class Locked:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    st.conn = Locked()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        st.fts_search("maxcore", 5)


def test_readonly_store_opens_wal_index_with_and_without_writer(project):
    ingest(project, progress=quiet)
    w = Store(project.index_path)
    assert Store(project.index_path, readonly=True).stats()["chunks"] > 0
    w.close()
    assert Store(project.index_path, readonly=True).stats()["chunks"] > 0


# --------------------------------------------------------------------------- 4. lessons tidy

def _merge_folded(bare: Path, tmp_path: Path, branch: str, rel: str) -> None:
    """Reviewer folds the lesson into a card and deletes it in the PR, then merges (merge commit)."""
    work = tmp_path / "reviewer"
    git(tmp_path, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", "-b", "pr", f"origin/{branch}")
    git(work, "rm", "-q", rel)
    card = work / "knowledge" / "ess" / "orca" / "orca.md"
    card.write_text(card.read_text() + "\n## Folded lesson\n%maxcore is per core.\n")
    git(work, "commit", "-q", "-am", "fold lesson into card")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge PR", "pr")
    git(work, "push", "-q", "origin", "main")


def test_tidy_removes_lesson_folded_into_card(project, tmp_path):
    bare = make_repo(project, tmp_path)
    cfg = configure(project, mode="branch")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "config")
    git(project.root, "push", "-q", "origin", "main")
    path = write_lesson(cfg, **ORCA_DUP)
    res = wf.submit_lesson(cfg, path)
    assert res.pushed and not res.error
    rel = path.relative_to(project.root).as_posix()
    keep = write_lesson(cfg, **{**ORCA_DUP, "title": "not pushed"})  # never pushed: must stay

    _merge_folded(bare, tmp_path, res.branch, rel)
    assert wf.tidy(cfg, dry_run=True) == [rel] and path.exists()
    assert wf.tidy(cfg) == [rel]
    assert not path.exists() and keep.exists()
    assert rel not in wf.load_state(cfg)
    keep.unlink()
    git(project.root, "pull", "-q", "--ff-only", "origin", "main")
    assert "Folded lesson" in (project.root / "knowledge/ess/orca/orca.md").read_text()


def test_tidy_keeps_locally_edited_lesson(project, tmp_path):
    bare = make_repo(project, tmp_path)
    cfg = configure(project, mode="branch")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "config")
    git(project.root, "push", "-q", "origin", "main")
    path = write_lesson(cfg, **ORCA_DUP)
    res = wf.submit_lesson(cfg, path)
    _merge_folded(bare, tmp_path, res.branch, path.relative_to(project.root).as_posix())
    path.write_text(path.read_text() + "\nedited after the push\n")
    assert wf.tidy(cfg) == [] and path.exists()


def test_tidy_fetch_uses_token_env(project, tmp_path, monkeypatch):
    make_repo(project, tmp_path)
    monkeypatch.setenv("MY_TOKEN", "tok123")
    cfg = configure(project, mode="github", repo="o/r", token_env="MY_TOKEN")
    seen = []

    def fake_token_env(git_, remote, token):
        seen.append(token)
        return {}

    monkeypatch.setattr(wf, "_token_env_config", fake_token_env)
    assert wf.tidy(cfg) == []
    assert seen == ["tok123"]


def test_branch_names_include_folder(project):
    a = write_lesson(project, **ORCA_DUP)
    b = write_lesson(project, **{**ORCA_DUP, "domain": "hpc", "software": None})
    assert a.name == b.name
    ba = wf.branch_for(a, "per-lesson", lessons_dir=project.lessons_dir)
    bb = wf.branch_for(b, "per-lesson", lessons_dir=project.lessons_dir)
    assert ba == f"lessons/ess-orca-{a.stem}" and bb == f"lessons/hpc-{b.stem}"


# --------------------------------------------------------------------------- 5. domain="lessons"

def test_domain_lessons_filters_to_lessons_source(project):
    ingest(project, progress=quiet)
    st = Store(project.index_path)
    index_lesson(project, st, write_lesson(project, **ORCA_DUP))
    searcher = Searcher(project, store=st, embedder=False)
    hits = searcher.search("maxcore per core", domain="lessons")
    assert hits and all(h.chunk.source == LESSONS_SOURCE for h in hits)
    assert any(h.chunk.source != LESSONS_SOURCE for h in searcher.search("maxcore per core"))

    # REST endpoint helper goes through the same searcher.
    from rag_drg.tools.rest_api import run_search

    class Ctx:
        cfg, lock = project, threading.Lock()

        def emit(self, _):
            pass

    Ctx.searcher = searcher
    rows, _ = run_search(Ctx(), "maxcore per core", domain="lessons")
    assert rows and all(r["source"] == LESSONS_SOURCE for r in rows)


def test_mcp_search_domain_lessons(project):
    ingest(project, progress=quiet)
    _, call = server_and_call(project)
    call("record_lesson", **ORCA_DUP)
    out = call("search_knowledge", query="maxcore per core", domain="lessons")
    assert "lessons:" in out and "curated:" not in out


# --------------------------------------------------------------------------- dedupe with metadata

def test_identical_files_with_different_sidecar_versions_are_both_kept(project):
    man = project.root / "manual"
    for v in ("5", "6"):
        (man / f"v{v}").mkdir()
        (man / f"v{v}" / "maxiter.md").write_text("# MaxIter\n\nSet `MaxIter 200` in the %scf block.\n")
        (man / f"v{v}" / "maxiter.md.meta.yaml").write_text(f"version: '{v}'\n")
    (man / "copy").mkdir()
    (man / "copy" / "maxiter.md").write_text("# MaxIter\n\nSet `MaxIter 200` in the %scf block.\n")
    (man / "copy" / "maxiter.md.meta.yaml").write_text("version: '6'\n")  # a true duplicate of v6
    chunks = chunks_for_source(project, project.source("manual"))
    paths = sorted({c.path for c in chunks if c.path.endswith("maxiter.md")})
    assert paths == ["v5/maxiter.md", "v6/maxiter.md"] or paths == ["copy/maxiter.md", "v5/maxiter.md"]
    assert {c.version for c in chunks if c.path.endswith("maxiter.md")} == {"5", "6"}
