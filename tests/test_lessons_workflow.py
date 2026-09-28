"""Lesson review workflow: duplicate hints, branch/GitHub PRs, daily batches, report, lint."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from rag_drg.cli import main as cli_main
from rag_drg.config import load_config
from rag_drg.ingest import ingest
from rag_drg.lessons import find_similar, index_lesson, lesson_text, read_lesson, similarity, write_lesson
from rag_drg.search import Searcher
from rag_drg.store import Store
from rag_drg.tools import lessons_workflow as wf

TODAY = dt.date.today().isoformat()

ORCA_DUP = dict(
    title="ORCA maxcore is per core", mistake="set %maxcore 64000 for 16 cores",
    correction="%maxcore is MB per core: %maxcore 3000 for 16 cores with 4 GB each",
    domain="ess", software="orca",
)
ORCA_OTHER = dict(
    title="ORCA: RIJCOSX needs an auxiliary basis", mistake="used ! RIJCOSX def2-TZVP without def2/J",
    correction="add def2/J: ! RIJCOSX def2-TZVP def2/J", domain="ess", software="orca",
)


def quiet(*_):
    pass


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "test@example.org")
    monkeypatch.setenv("RAG_DRG_AUTHOR", "alice")


def configure(project, **pr) -> "object":
    """Write conf.d/lessons.yaml into the temp project and reload the config."""
    (project.root / "conf.d").mkdir(exist_ok=True)
    (project.root / "conf.d" / "lessons.yaml").write_text(yaml.safe_dump({"lessons": {"pr": pr}}))
    return load_config(project.root / "rag_drg.yaml")


def make_repo(project, tmp_path) -> Path:
    """Turn the temp project into a git checkout on `main` with a bare `origin`."""
    root = project.root
    (root / ".gitignore").write_text("index/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    bare = root.parent / f"{root.name}-origin.git"  # outside the checkout (project.root is tmp_path)
    git(root.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "origin", "main")
    return bare


def server_and_call(cfg):
    from rag_drg.mcp_server import build_server

    mcp = build_server(cfg)

    def call(name, **args) -> str:
        res = asyncio.run(mcp.call_tool(name, args))
        if isinstance(res, tuple):  # SDK 1.x returns (content, structured)
            res = res[0]
        content = getattr(res, "content", res)
        return "\n".join(c.text for c in content if hasattr(c, "text"))

    return mcp, call


class FakeGitHub:
    """Tiny stand-in for api.github.com that records requests."""

    def __init__(self):
        self.requests: list[dict] = []
        self.open_prs: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                if self.command == "GET" and self.path.startswith("/repos/o/r/pulls"):
                    return self._reply(200, outer.open_prs)
                if self.command == "POST" and self.path == "/repos/o/r/pulls":
                    pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "body": body["body"],
                          "head": {"ref": body["head"]}}
                    outer.open_prs.append(pr)
                    return self._reply(201, pr)
                if self.command == "PATCH" and self.path == "/repos/o/r/pulls/7":
                    outer.open_prs[0]["body"] = body["body"]
                    return self._reply(200, outer.open_prs[0])
                if self.command == "POST" and self.path.endswith("/labels"):
                    return self._reply(200, [])
                return self._reply(404, {"message": "Not Found"})

            do_GET = do_POST = do_PATCH = _handle

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def github():
    gh = FakeGitHub()
    yield gh
    gh.close()


# --------------------------------------------------------------------------- similarity

def test_similarity_score_separates_duplicates():
    card = "ORCA essentials > Memory\n`%maxcore` is MB per core, not total memory. Use about 75% of the memory per core."
    dup = lesson_text(ORCA_DUP["title"], ORCA_DUP["mistake"], ORCA_DUP["correction"])
    other = lesson_text(ORCA_OTHER["title"], ORCA_OTHER["mistake"], ORCA_OTHER["correction"])
    assert similarity(dup, card) > 0.5
    assert similarity(other, card) < 0.2


def test_record_lesson_flags_duplicates(project):
    ingest(project, progress=quiet)
    _, call = server_and_call(project)

    out = call("record_lesson", **ORCA_DUP)
    assert "possibly duplicates: knowledge/ess/orca/orca.md" in out
    first = next((project.lessons_dir / "ess" / "orca").glob("*.md"))
    meta, _ = read_lesson(first)
    assert meta["similar"] == ["knowledge/ess/orca/orca.md"] and meta["author"] == "alice"
    assert "commit it and open a PR" in out  # mode none

    out2 = call("record_lesson", **{**ORCA_DUP, "title": "ORCA: %maxcore is per core, in MB"})
    rel_first = first.relative_to(project.root).as_posix()
    assert rel_first in out2  # the earlier lesson is found as well

    out3 = call("record_lesson", **ORCA_OTHER)
    assert "possibly duplicates" not in out3

    # `rag-drg lessons similar PATH` (the lesson itself is excluded)
    code = cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "similar", rel_first, "--json"])
    assert code == 0


def test_lessons_similar_cli(project, capsys):
    ingest(project, progress=quiet)
    path = write_lesson(project, **ORCA_DUP)
    st = Store(project.index_path)
    index_lesson(project, st, path)
    st.close()
    rel = path.relative_to(project.root).as_posix()
    assert cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "similar", rel, "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in found] == ["knowledge/ess/orca/orca.md"]
    assert found[0]["score"] >= 0.35


def test_find_similar_without_index(project):
    # No index yet: recording must still work (no hints).
    assert find_similar(project, Searcher(project, store=Store(project.index_path)), "maxcore", software="orca") == []


# --------------------------------------------------------------------------- branch mode

def test_branch_mode_pushes_without_touching_checkout(project, tmp_path):
    bare = make_repo(project, tmp_path)
    cfg = configure(project, mode="branch", remote="origin", base="main")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "config")
    git(project.root, "push", "-q", "origin", "main")
    ingest(cfg, progress=quiet)
    head_before = git(project.root, "rev-parse", "HEAD")

    _, call = server_and_call(cfg)
    out = call("record_lesson", **ORCA_DUP)
    lesson = next((project.lessons_dir / "ess" / "orca").glob("*.md"))
    rel = lesson.relative_to(project.root).as_posix()
    branch = f"lessons/{lesson.stem}"
    assert f"Pushed to branch {branch}" in out

    # Branch on the remote: one commit on top of main, containing exactly the lesson.
    assert git(bare, "show", f"{branch}:{rel}") == lesson.read_text().strip()
    assert git(bare, "rev-parse", f"{branch}^") == git(bare, "rev-parse", "main")
    assert git(bare, "diff", "--name-only", "main", branch) == rel
    assert git(bare, "log", "-1", "--format=%s", branch).startswith("Lesson: ORCA maxcore is per core")

    # Serving checkout untouched: same HEAD and branch, no local lesson branch, lesson untracked.
    assert git(project.root, "rev-parse", "HEAD") == head_before
    assert git(project.root, "branch", "--show-current") == "main"
    assert git(project.root, "branch", "--list", "lessons/*") == ""
    assert git(project.root, "status", "--porcelain", "--untracked-files=all") == f"?? {rel}"
    assert wf.load_state(cfg)[rel]["branch"] == branch

    # Already pushed -> `lessons pr --all-unreviewed` has nothing to do.
    assert cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "pr", "--all-unreviewed"]) == 0

    # After the PR is merged, `lessons tidy` removes the local copy so `git pull --ff-only` works.
    git(bare, "update-ref", "refs/heads/main", branch)
    assert wf.tidy(cfg) == [rel]
    assert not lesson.exists()
    git(project.root, "pull", "-q", "--ff-only", "origin", "main")
    assert lesson.exists()


def test_lessons_pr_cli_for_existing_lessons(project, tmp_path, capsys):
    bare = make_repo(project, tmp_path)
    cfg = configure(project, mode="branch")
    p1 = write_lesson(cfg, **ORCA_OTHER, author="bob")
    code = cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "pr", "--all-unreviewed"])
    assert code == 0
    assert git(bare, "branch", "--list", f"lessons/{p1.stem}").strip().endswith(p1.stem)
    capsys.readouterr()
    cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "pr", "--all-unreviewed"])
    assert "Nothing to do" in capsys.readouterr().out


# --------------------------------------------------------------------------- github mode

def test_github_mode_opens_pr(project, tmp_path, github, monkeypatch):
    make_repo(project, tmp_path)
    monkeypatch.setenv("MY_GH_TOKEN", "sekrit-token")
    cfg = configure(project, mode="github", repo="o/r", token_env="MY_GH_TOKEN", api_url=github.url,
                    labels=["lesson"])
    ingest(cfg, progress=quiet)
    _, call = server_and_call(cfg)
    out = call("record_lesson", **ORCA_DUP)
    lesson = next((project.lessons_dir / "ess" / "orca").glob("*.md"))
    rel = lesson.relative_to(project.root).as_posix()

    assert "opened PR https://github.com/o/r/pull/7" in out
    assert "sekrit" not in out
    assert all(r["auth"] == "Bearer sekrit-token" for r in github.requests)
    get, post, labels = github.requests
    assert get["method"] == "GET" and get["path"].startswith("/repos/o/r/pulls?")
    assert f"head=o%3Alessons%2F{lesson.stem}" in get["path"]
    assert post["method"] == "POST" and post["path"] == "/repos/o/r/pulls"
    body = post["body"]
    assert body["title"] == "Lesson: ORCA maxcore is per core"
    assert body["head"] == f"lessons/{lesson.stem}" and body["base"] == "main"
    for text in (rel, "alice", "knowledge/ess/orca/orca.md", "Reviewer checklist", "status: verified",
                 "%maxcore 3000"):
        assert text in body["body"]
    assert labels["path"] == "/repos/o/r/issues/7/labels" and labels["body"] == {"labels": ["lesson"]}
    assert wf.load_state(cfg)[rel]["pr"] == "https://github.com/o/r/pull/7"


def test_daily_batch_reuses_branch_and_pr(project, tmp_path, github, monkeypatch):
    bare = make_repo(project, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    cfg = configure(project, mode="github", repo="o/r", api_url=github.url, batch="daily")
    _, call = server_and_call(cfg)
    out1 = call("record_lesson", **ORCA_DUP)
    out2 = call("record_lesson", **ORCA_OTHER)
    assert "opened PR https://github.com/o/r/pull/7" in out1
    assert "added to the open PR https://github.com/o/r/pull/7" in out2

    branch = f"lessons/{TODAY}"
    files = git(bare, "diff", "--name-only", "main", branch).splitlines()
    assert len(files) == 2 and all(f.startswith("knowledge/lessons/ess/orca/") for f in files)
    assert int(git(bare, "rev-list", "--count", f"main..{branch}")) == 2

    posts = [r for r in github.requests if r["method"] == "POST"]
    patches = [r for r in github.requests if r["method"] == "PATCH"]
    assert len(posts) == 1 and posts[0]["body"]["title"] == f"Lessons: {TODAY}"
    assert len(patches) == 1 and all(f"`{f}`" in patches[0]["body"]["body"] for f in files)


# --------------------------------------------------------------------------- failures

def test_bad_remote_does_not_fail_the_tool(project, tmp_path):
    make_repo(project, tmp_path)
    git(project.root, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    cfg = configure(project, mode="branch")
    ingest(cfg, progress=quiet)
    _, call = server_and_call(cfg)
    out = call("record_lesson", **ORCA_DUP)
    assert "Lesson saved to" in out and "PR step failed" in out
    lesson = next((project.lessons_dir / "ess" / "orca").glob("*.md"))
    assert lesson.exists()
    hits = Searcher(cfg).search("maxcore per core", software="orca", doc_type="lesson")
    assert hits and hits[0].chunk.status == "unreviewed"
    assert wf.load_state(cfg) == {}


def test_github_errors_are_reported_not_raised(project, tmp_path, monkeypatch):
    make_repo(project, tmp_path)
    cfg = configure(project, mode="github", repo="o/r", token_env="NOPE_TOKEN")
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    res = wf.submit_lesson(cfg, write_lesson(cfg, **ORCA_OTHER))
    assert res.error and "NOPE_TOKEN" in res.error

    # API down: the token must not leak into the error.
    monkeypatch.setenv("NOPE_TOKEN", "leaky-token")
    cfg = configure(project, mode="github", repo="o/r", token_env="NOPE_TOKEN", api_url="http://127.0.0.1:9")
    res = wf.submit_lesson(cfg, write_lesson(cfg, **ORCA_DUP))
    assert res.error and "leaky" not in res.error and res.pushed


def test_not_a_git_checkout(project):
    cfg = configure(project, mode="branch")
    res = wf.submit_lesson(cfg, write_lesson(cfg, **ORCA_OTHER))
    assert res.error and "git" in res.error


# --------------------------------------------------------------------------- report + lint

def _lesson(project, rel, **meta):
    p = project.lessons_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    base = {"title": rel, "domain": "ess", "doc_type": "lesson", "status": "unreviewed"}
    p.write_text("---\n" + yaml.safe_dump({**base, **meta}) + "---\n\n# t\n\n## Mistake\n\nx\n\n## Correct approach\n\ny\n")
    return p


def test_report(project, capsys):
    _lesson(project, "ess/orca/2026-01-01-old.md", software="orca", author="bob", date="2026-01-01")
    _lesson(project, "ess/orca/2026-03-01-newer.md", software="orca", author="alice", date="2026-03-01",
            similar=["knowledge/ess/orca/orca.md"])
    _lesson(project, "ess/orca/2026-02-01-ok.md", software="orca", author="alice", status="verified")
    _lesson(project, "hpc/2026-02-02-q.md", domain="hpc", author="alice", status="verified", date="2026-02-02")
    rep = wf.build_report(project, today=dt.date(2026, 3, 10))

    assert [e["path"].rsplit("/", 1)[1] for e in rep["unreviewed"]] == ["2026-01-01-old.md", "2026-03-01-newer.md"]
    assert [e["stale"] for e in rep["unreviewed"]] == [True, False]
    assert rep["verified_by_software"]["orca"][0]["fold_into"] == "knowledge/ess/orca/orca.md"
    assert rep["verified_by_software"]["hpc"][0]["fold_into"] == "knowledge/hpc/"
    assert [e["path"] for e in rep["with_similar"]] == ["knowledge/lessons/ess/orca/2026-03-01-newer.md"]
    assert rep["by_author"] == {"alice": 3, "bob": 1}
    assert rep["counts"] == {"unreviewed": 2, "verified": 2}

    text = wf.format_report(rep)
    assert "!  68d  knowledge/lessons/ess/orca/2026-01-01-old.md" in text
    assert "fold into knowledge/ess/orca/orca.md" in text

    assert cli_main(["-c", str(project.root / "rag_drg.yaml"), "lessons", "report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["by_author"]["alice"] == 3


def test_lint_checks_pr_and_similar(project):
    good = _lesson(project, "ess/orca/2026-01-01-good.md", software="orca",
                   pr="https://github.com/o/r/pull/12", similar=["knowledge/ess/orca/orca.md"])
    assert wf.lint(project) == []
    _lesson(project, "ess/orca/2026-01-02-bad.md", software="orca", pr="github.com/o/r/pulls",
            similar=["knowledge/ess/orca/gone.md"])
    _lesson(project, "ess/orca/2026-01-03-bad2.md", software="orca", similar="knowledge/ess/orca/orca.md")
    problems = wf.lint(project)
    assert len(problems) == 3
    assert any("'pr' must be a pull request URL" in p for p in problems)
    assert not any("gone.md" in p for p in problems)  # dangling hints are allowed
    assert any("must be a list" in p for p in problems)
    assert good.exists()

    from rag_drg.lint import lint as full_lint  # the plugin hook runs as part of `rag-drg lint`

    assert len([p for p in full_lint(project) if "bad" in p]) == 3
    bad_cfg = configure(project, mode="gitlab")
    assert any("lessons.pr.mode" in p for p in wf.lint(bad_cfg))
