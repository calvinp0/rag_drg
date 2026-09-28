"""Lesson review workflow: near-duplicate hints, one PR per lesson (or per day), reports, lint.

Configured in ``conf.d/lessons.yaml`` (see docs/lessons.md)::

    lessons:
      pr:
        mode: none            # none | branch | github
        remote: origin
        base: main
        repo: owner/name      # github mode
        token_env: GITHUB_TOKEN
        batch: per-lesson     # per-lesson | daily

Git work never touches the serving checkout's working tree, index or current branch: the
lesson is committed with plumbing (a throw-away index file, ``commit-tree``) on top of
``<remote>/<base>`` (or the existing lesson branch) and pushed straight to
``refs/heads/lessons/...`` on the remote. No local branch is created.

CLI (``rag-drg lessons ...``): ``similar PATH``, ``pr [PATH ...] [--all-unreviewed]``,
``report [--json]``, ``tidy``.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..lessons import iter_lessons, lesson_date, read_lesson, repo_rel, similar_for_file

MODES = ("none", "branch", "github")
BATCHES = ("per-lesson", "daily")
PR_URL = re.compile(r"^https?://[^\s/]+/[\w.-]+/[\w.-]+/pull/\d+$")
STALE_DAYS = 14

_git_lock = threading.Lock()  # one PR update at a time (daily batches push to the same branch)


# --------------------------------------------------------------------------- #
# Config and state
# --------------------------------------------------------------------------- #

def pr_settings(cfg: Config) -> dict:
    raw = ((cfg.extra.get("lessons") or {}).get("pr") or {})
    out = {
        "mode": str(raw.get("mode", "none")).lower(),
        "remote": str(raw.get("remote", "origin")),
        "base": str(raw.get("base", "main")),
        "repo": raw.get("repo"),
        "token_env": str(raw.get("token_env", "GITHUB_TOKEN")),
        "batch": str(raw.get("batch", "per-lesson")).lower(),
        "labels": [str(x) for x in (raw.get("labels") or [])],
        "api_url": str(raw.get("api_url", "https://api.github.com")).rstrip("/"),
        "draft": bool(raw.get("draft", False)),
    }
    if out["mode"] not in MODES:
        raise ValueError(f"lessons.pr.mode must be one of {MODES}, got {out['mode']!r}")
    if out["batch"] not in BATCHES:
        raise ValueError(f"lessons.pr.batch must be one of {BATCHES}, got {out['batch']!r}")
    return out


def _state_path(cfg: Config) -> Path:
    return cfg.index_path.parent / "lessons_prs.json"


def load_state(cfg: Config) -> dict:
    """{lesson path relative to config root: {"branch", "commit", "pr", "updated"}}."""
    try:
        return json.loads(_state_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(cfg: Config, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def default_author() -> str | None:
    return os.environ.get("RAG_DRG_AUTHOR") or os.environ.get("USER")


# --------------------------------------------------------------------------- #
# Git (plumbing only; the serving checkout is never modified)
# --------------------------------------------------------------------------- #

class GitError(RuntimeError):
    pass


def _scrub(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, "***")
        text = text.replace(base64.b64encode(f"x-access-token:{secret}".encode()).decode(), "***")
    return text


class _Git:
    def __init__(self, cwd: Path, extra_env: dict | None = None, secret: str | None = None):
        self.cwd = cwd
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
        self.secret = secret

    def __call__(self, *args: str, env: dict | None = None, check: bool = True, timeout: int = 120) -> str:
        try:
            r = subprocess.run(
                ["git", *args], cwd=self.cwd, env={**self.env, **(env or {})},
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitError(_scrub(f"git {args[0]}: {e}", self.secret)) from None
        if check and r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()
            raise GitError(_scrub(f"git {args[0]} failed: {' | '.join(msg[-3:])}", self.secret))
        return r.stdout.strip()


def _token_env_config(git: _Git, remote: str, token: str | None) -> dict:
    """Let `git push/fetch` authenticate to an https GitHub remote with the token, passed through
    GIT_CONFIG_* environment variables so it never shows up in argv, logs or error messages."""
    if not token:
        return {}
    url = git("remote", "get-url", remote, check=False)
    if not url.startswith("https://"):
        return {}  # ssh remotes use the server's deploy key
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def _identity_env(git: _Git, author: str | None) -> dict:
    env = {}
    if not git("config", "user.name", check=False):
        env["GIT_COMMITTER_NAME"] = "rag-drg"
        env["GIT_AUTHOR_NAME"] = "rag-drg"
    if not git("config", "user.email", check=False):
        env["GIT_COMMITTER_EMAIL"] = "rag-drg@localhost"
        env["GIT_AUTHOR_EMAIL"] = "rag-drg@localhost"
    return env


def branch_for(path: Path, batch: str, today: str | None = None) -> str:
    if batch == "daily":
        return f"lessons/{today or _dt.date.today().isoformat()}"
    return f"lessons/{Path(path).stem}"  # the file name is already <date>-<slug>


@dataclass
class PRResult:
    lesson: str
    branch: str | None = None
    commit: str | None = None
    pushed: bool = False
    pr_url: str | None = None
    pr_number: int | None = None
    updated_existing: bool = False
    skipped: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.error:
            return f"PR step failed ({self.error}); the lesson file is kept and still searchable."
        if self.skipped:
            return f"PR step skipped: {self.skipped}."
        where = f"pushed to branch {self.branch}"
        if self.pr_url:
            verb = "added to the open PR" if self.updated_existing else "opened PR"
            where += f"; {verb} {self.pr_url}"
        elif self.pushed:
            where += "; open a pull request from it for review"
        return where[0].upper() + where[1:] + "."


def commit_and_push(cfg: Config, path: Path, author: str | None, settings: dict,
                    token: str | None = None, message: str | None = None) -> PRResult:
    """Commit `path` onto the lesson branch (created from <remote>/<base>) and push it."""
    path = Path(path).resolve()
    res = PRResult(lesson=repo_rel(cfg, path))
    git = _Git(path.parent, secret=token)
    top = Path(git("rev-parse", "--show-toplevel"))
    git = _Git(top, secret=token)
    rel = path.relative_to(top.resolve()).as_posix()
    remote, base = settings["remote"], settings["base"]
    net = _token_env_config(git, remote, token)
    branch = branch_for(path, settings["batch"])
    res.branch = branch

    git("fetch", "--no-tags", remote, f"+refs/heads/{base}:refs/remotes/{remote}/{base}", env=net)
    exists = bool(git("ls-remote", "--heads", remote, f"refs/heads/{branch}", env=net))
    if exists:
        git("fetch", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}", env=net)
        parent = git("rev-parse", f"refs/remotes/{remote}/{branch}")
    else:
        parent = git("rev-parse", f"refs/remotes/{remote}/{base}")

    with tempfile.TemporaryDirectory(prefix="rag-drg-lesson-") as tmp:
        idx = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", parent, env=idx)
        blob = git("hash-object", "-w", "--", str(path))
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=idx)
        tree = git("write-tree", env=idx)
    if tree == git("rev-parse", f"{parent}^{{tree}}"):
        if exists:
            res.commit, res.pushed = parent, True
            res.notes.append("branch already has this version of the lesson")
            return res
        res.skipped = f"{rel} is already identical on {remote}/{base}"
        return res
    meta, _ = read_lesson(path)
    msg = message or f"Lesson: {meta.get('title') or path.stem}\n\nRecorded by {author or 'unknown'} via rag-drg record_lesson."
    commit = git("commit-tree", tree, "-p", parent, "-m", msg, env=_identity_env(git, author))
    git("push", "--porcelain", remote, f"{commit}:refs/heads/{branch}", env=net)
    res.commit, res.pushed = commit, True
    return res


# --------------------------------------------------------------------------- #
# GitHub REST API (urllib only)
# --------------------------------------------------------------------------- #

class GitHubError(RuntimeError):
    pass


def _gh(settings: dict, token: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        settings["api_url"] + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rag-drg",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - https URL from config
            data = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("message", "")
        except ValueError:
            detail = ""
        raise GitHubError(_scrub(f"GitHub {method} {path}: HTTP {e.code} {detail}".strip(), token)) from None
    except (urllib.error.URLError, OSError) as e:
        raise GitHubError(_scrub(f"GitHub {method} {path}: {e}", token)) from None
    return json.loads(data) if data else None


def _lesson_section(cfg: Config, path: Path, author: str | None) -> str:
    meta, body = read_lesson(path)
    rel = repo_rel(cfg, path)
    lines = [f"### `{rel}`", "",
             f"- **Author:** {author or meta.get('author') or 'unknown'}",
             f"- **Scope:** {'/'.join(str(x) for x in (meta.get('domain'), meta.get('software')) if x)}"
             + (f" v{meta.get('version')}" if meta.get("version") else "")]
    sim = meta.get("similar") or []
    if sim:
        lines.append("- **Possibly duplicates** (merge instead of adding a new lesson?):")
        lines += [f"  - `{s}`" for s in sim]
    else:
        lines.append("- **Similar entries:** none found")
    lines += ["", "<details open><summary>Lesson</summary>", "", body.strip(), "", "</details>", ""]
    return "\n".join(lines)


CHECKLIST = """\
### Reviewer checklist

- [ ] Checked against the manual / a test calculation (cite the section in *Evidence*).
- [ ] Wording is short and concrete; the wrong and the right snippet are both shown.
- [ ] Not a duplicate of the *similar* entries above (if it is, merge them).
- [ ] Then either set `status: verified` in the lesson, **or** fold it into the matching card
      under `knowledge/` and delete the lesson file in this PR.
- [ ] `rag-drg lint` passes.
"""


def pr_title(cfg: Config, path: Path, settings: dict) -> str:
    if settings["batch"] == "daily":
        return f"Lessons: {_dt.date.today().isoformat()}"
    meta, _ = read_lesson(path)
    return f"Lesson: {meta.get('title') or Path(path).stem}"


def pr_body(cfg: Config, path: Path, author: str | None, settings: dict) -> str:
    intro = ("Lessons recorded by agents today (one section per lesson)."
             if settings["batch"] == "daily" else "A lesson recorded with `record_lesson`.")
    return (f"{intro} Opened automatically by rag-drg.\n\n{CHECKLIST}\n"
            f"## Lessons\n\n{_lesson_section(cfg, path, author)}")


def open_or_update_pr(cfg: Config, path: Path, author: str | None, settings: dict, token: str,
                      res: PRResult) -> PRResult:
    repo = settings["repo"]
    if not repo or "/" not in str(repo):
        raise GitHubError("lessons.pr.repo must be 'owner/name' in github mode")
    owner = str(repo).split("/")[0]
    q = urllib.parse.urlencode({"head": f"{owner}:{res.branch}", "state": "open", "base": settings["base"]})
    existing = _gh(settings, token, "GET", f"/repos/{repo}/pulls?{q}") or []
    if existing:
        pr = existing[0]
        rel = repo_rel(cfg, path)
        body = pr.get("body") or ""
        if f"`{rel}`" not in body:
            body = body.rstrip() + "\n\n" + _lesson_section(cfg, path, author)
            _gh(settings, token, "PATCH", f"/repos/{repo}/pulls/{pr['number']}", {"body": body})
        res.updated_existing = True
    else:
        pr = _gh(settings, token, "POST", f"/repos/{repo}/pulls", {
            "title": pr_title(cfg, path, settings), "head": res.branch, "base": settings["base"],
            "body": pr_body(cfg, path, author, settings), "draft": settings["draft"],
            "maintainer_can_modify": True,
        })
        if settings["labels"]:
            try:
                _gh(settings, token, "POST", f"/repos/{repo}/issues/{pr['number']}/labels",
                    {"labels": settings["labels"]})
            except GitHubError as e:  # labels are cosmetic
                res.notes.append(f"labels not set: {e}")
    res.pr_url, res.pr_number = pr.get("html_url"), pr.get("number")
    return res


# --------------------------------------------------------------------------- #
# Entry point used by record_lesson and `rag-drg lesson` / `rag-drg lessons pr`
# --------------------------------------------------------------------------- #

def submit_lesson(cfg: Config, path: Path, author: str | None = None) -> PRResult | None:
    """Push the lesson for review according to lessons.pr. Never raises: errors land in
    PRResult.error. Returns None when mode is `none`."""
    path = Path(path)
    try:
        settings = pr_settings(cfg)
    except ValueError as e:
        return PRResult(lesson=repo_rel(cfg, path), error=str(e))
    if settings["mode"] == "none":
        return None
    token = None
    if settings["mode"] == "github":
        token = os.environ.get(settings["token_env"]) or None
    res = PRResult(lesson=repo_rel(cfg, path))
    try:
        if settings["mode"] == "github" and not token:
            raise GitHubError(f"${settings['token_env']} is not set")
        with _git_lock:
            res = commit_and_push(cfg, path, author, settings, token=token)
            if settings["mode"] == "github" and res.pushed:
                open_or_update_pr(cfg, path, author, settings, token, res)
    except Exception as e:  # noqa: BLE001 - recording a lesson must never fail because of git/GitHub
        res.error = _scrub(str(e) or type(e).__name__, token)
        return res
    if res.pushed:
        try:
            state = load_state(cfg)
            state[res.lesson] = {"branch": res.branch, "commit": res.commit, "pr": res.pr_url,
                                 "updated": _dt.datetime.now().isoformat(timespec="seconds")}
            _save_state(cfg, state)
        except OSError as e:
            res.notes.append(f"state file not written: {e}")
    return res


def pr_for(cfg: Config, path: Path, meta: dict | None = None, state: dict | None = None) -> str | None:
    """The PR URL (or pushed branch) recorded for a lesson, if any."""
    if meta is None:
        meta, _ = read_lesson(path)
    if meta.get("pr"):
        return str(meta["pr"])
    entry = (state if state is not None else load_state(cfg)).get(repo_rel(cfg, path)) or {}
    return entry.get("pr") or (f"branch {entry['branch']}" if entry.get("branch") else None)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _card_dir(cfg: Config, domain: str | None, software: str | None) -> Path | None:
    curated = next((s.path for s in cfg.sources if s.name == "curated" and s.path), cfg.root / "knowledge")
    if domain == "ess" and software:
        return curated / "ess" / software
    if domain in ("arc", "hpc"):
        return curated / domain
    if domain == "project":
        return curated / "projects"
    return None


def fold_target(cfg: Config, meta: dict) -> str:
    """Where a verified lesson should be folded in: a card it was flagged similar to, else the
    matching card folder."""
    lessons_rel = repo_rel(cfg, cfg.lessons_dir)
    for s in meta.get("similar") or []:
        if not str(s).startswith(lessons_rel):
            return str(s)
    d = _card_dir(cfg, meta.get("domain"), meta.get("software"))
    if d is None:
        return "a matching card under knowledge/"
    cards = sorted(p for p in d.glob("*.md") if not p.name.startswith("_")) if d.is_dir() else []
    if len(cards) == 1:
        return repo_rel(cfg, cards[0])
    return repo_rel(cfg, d) + "/"


def build_report(cfg: Config, today: _dt.date | None = None, stale_days: int = STALE_DAYS) -> dict:
    today = today or _dt.date.today()
    state = load_state(cfg)
    unreviewed, verified, with_similar, authors = [], {}, [], {}
    counts: dict[str, int] = {}
    for f in iter_lessons(cfg):
        meta, _ = read_lesson(f)
        rel = repo_rel(cfg, f)
        d = lesson_date(f, meta)
        age = (today - d).days if d else None
        status = str(meta.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        author = str(meta.get("author") or "unknown")
        authors[author] = authors.get(author, 0) + 1
        entry = {"path": rel, "title": meta.get("title"), "software": meta.get("software"),
                 "domain": meta.get("domain"), "author": author, "date": d.isoformat() if d else None,
                 "age_days": age, "pr": pr_for(cfg, f, meta, state)}
        if status == "unreviewed":
            unreviewed.append({**entry, "stale": age is not None and age > stale_days})
        elif status == "verified":
            verified.setdefault(str(meta.get("software") or meta.get("domain") or "-"), []).append(
                {**entry, "fold_into": fold_target(cfg, meta)})
        if meta.get("similar"):
            with_similar.append({**entry, "similar": [str(s) for s in meta["similar"]]})
    unreviewed.sort(key=lambda e: (e["date"] or "9999", e["path"]))
    return {
        "generated": today.isoformat(), "stale_days": stale_days, "counts": counts,
        "unreviewed": unreviewed, "verified_by_software": dict(sorted(verified.items())),
        "with_similar": with_similar,
        "by_author": dict(sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def format_report(rep: dict) -> str:
    counts = ", ".join(f"{n} {s}" for s, n in sorted(rep["counts"].items())) or "no lessons"
    out = [f"Lessons report ({rep['generated']}): {counts}"]
    out += ["", f"## Unreviewed ({len(rep['unreviewed'])}, oldest first; ! = older than {rep['stale_days']} days)"]
    for e in rep["unreviewed"]:
        flag = "!" if e["stale"] else " "
        age = f"{e['age_days']:>3}d" if e["age_days"] is not None else "  ?d"
        out.append(f"{flag} {age}  {e['path']}  ({e['author']}) {e['title']}"
                   + (f"  [{e['pr']}]" if e["pr"] else "  [no PR]"))
    out += ["", "## Verified: fold into cards"]
    for sw, items in rep["verified_by_software"].items():
        out.append(f"- {sw}:")
        for e in items:
            out.append(f"    {e['path']}  ->  fold into {e['fold_into']}, then delete the lesson")
    out += ["", f"## Lessons with possible duplicates ({len(rep['with_similar'])})"]
    for e in rep["with_similar"]:
        out.append(f"- {e['path']}: " + ", ".join(e["similar"]))
    out += ["", "## Lessons per author"]
    out += [f"  {n:>4}  {a}" for a, n in rep["by_author"].items()]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Tidy: remove local copies once the reviewed version is on <remote>/<base>
# --------------------------------------------------------------------------- #

def tidy(cfg: Config, dry_run: bool = False) -> list[str]:
    """Delete untracked lesson files whose path now exists on <remote>/<base> (their PR was
    merged), so `git pull --ff-only` can bring in the reviewed version."""
    settings = pr_settings(cfg)
    if not cfg.lessons_dir.exists():
        return []
    git = _Git(cfg.lessons_dir)
    top = Path(git("rev-parse", "--show-toplevel"))
    git = _Git(top)
    remote, base = settings["remote"], settings["base"]
    git("fetch", "--no-tags", remote, f"+refs/heads/{base}:refs/remotes/{remote}/{base}")
    lessons_rel = cfg.lessons_dir.resolve().relative_to(top.resolve()).as_posix()
    untracked = git("ls-files", "--others", "--exclude-standard", "--", lessons_rel).splitlines()
    on_base = set(git("ls-tree", "-r", "--name-only", f"refs/remotes/{remote}/{base}", "--", lessons_rel).splitlines())
    removed = []
    for rel in untracked:
        if rel in on_base:
            removed.append(rel)
            if not dry_run:
                (top / rel).unlink()
    return removed


# --------------------------------------------------------------------------- #
# Plugin hooks
# --------------------------------------------------------------------------- #

def lint(cfg: Config) -> list[str]:
    problems = []
    for f in iter_lessons(cfg):
        meta, _ = read_lesson(f)
        rel = repo_rel(cfg, f)
        pr = meta.get("pr")
        if pr is not None and not PR_URL.match(str(pr)):
            problems.append(f"{rel}: 'pr' must be a pull request URL like https://github.com/owner/repo/pull/12, got {pr!r}")
        sim = meta.get("similar")
        if sim is None:
            continue
        if not isinstance(sim, list):
            problems.append(f"{rel}: 'similar' must be a list of paths")
            continue
        # A `similar` path may legitimately disappear (a lesson folded into a card and deleted),
        # so only the type is checked here; dangling entries are harmless hints.
        for s in sim:
            if not isinstance(s, str):
                problems.append(f"{rel}: similar entry {s!r} must be a path string")
    try:
        pr_settings(cfg)
    except ValueError as e:
        problems.append(f"conf.d/lessons.yaml: {e}")
    return problems


def _searcher(cfg: Config):
    from ..search import Searcher
    from ..store import Store

    return Searcher(cfg, store=Store(cfg.index_path, readonly=True))


def _cmd(args, cfg: Config) -> int:
    if args.lessons_cmd == "similar":
        path = Path(args.path)
        if not path.is_file():
            path = cfg.root / args.path
        if not path.is_file():
            print(f"No such lesson: {args.path}")
            return 2
        sims = similar_for_file(cfg, _searcher(cfg), path)
        if args.json:
            print(json.dumps([s.to_dict() for s in sims], indent=2))
        elif not sims:
            print("No similar lessons or cards found.")
        else:
            for s in sims:
                print(f"{s.score:.2f}  {s.path}  {s.title}")
        return 0

    if args.lessons_cmd == "report":
        rep = build_report(cfg, stale_days=args.stale_days)
        print(json.dumps(rep, indent=2) if args.json else format_report(rep))
        return 0

    if args.lessons_cmd == "tidy":
        try:
            removed = tidy(cfg, dry_run=args.dry_run)
        except GitError as e:
            print(f"tidy failed: {e}")
            return 1
        for r in removed:
            print(("would remove " if args.dry_run else "removed ") + r)
        return 0

    if args.lessons_cmd == "pr":
        settings = pr_settings(cfg)
        if settings["mode"] == "none":
            print("lessons.pr.mode is 'none' in conf.d/lessons.yaml; set it to 'branch' or 'github' first.")
            return 2
        if not args.paths and not args.all_unreviewed:
            print("Give lesson PATHs or --all-unreviewed.")
            return 2
        state = load_state(cfg)
        if args.paths:
            todo = [Path(p) if Path(p).is_file() else cfg.root / p for p in args.paths]
        else:
            todo = []
            for f in iter_lessons(cfg):
                meta, _ = read_lesson(f)
                if meta.get("status") == "unreviewed" and not pr_for(cfg, f, meta, state):
                    todo.append(f)
        rc = 0
        for f in todo:
            if args.dry_run:
                print(f"would open a PR for {repo_rel(cfg, f)}")
                continue
            meta, _ = read_lesson(f)
            res = submit_lesson(cfg, f, author=meta.get("author") or default_author())
            print(f"{repo_rel(cfg, f)}: {res.summary() if res else 'mode none'}")
            rc = rc or (1 if res and res.error else 0)
        if not todo:
            print("Nothing to do: every unreviewed lesson already has a PR.")
        return rc
    return 2


def register_cli(sub) -> dict:
    p = sub.add_parser("lessons", help="lesson review workflow: similar, pr, report, tidy")
    ls = p.add_subparsers(dest="lessons_cmd", required=True)
    q = ls.add_parser("similar", help="existing lessons/cards that look like duplicates of a lesson")
    q.add_argument("path")
    q.add_argument("--json", action="store_true")
    q = ls.add_parser("pr", help="push unreviewed lessons for review (lessons.pr in conf.d/lessons.yaml)")
    q.add_argument("paths", nargs="*", help="lesson files (default: see --all-unreviewed)")
    q.add_argument("--all-unreviewed", action="store_true", help="every unreviewed lesson without a PR yet")
    q.add_argument("--dry-run", action="store_true")
    q = ls.add_parser("report", help="unreviewed lessons by age, verified ones to fold into cards, duplicates, authors")
    q.add_argument("--json", action="store_true")
    q.add_argument("--stale-days", type=int, default=STALE_DAYS)
    q = ls.add_parser("tidy", help="delete local lesson copies that are merged on <remote>/<base> (run before git pull)")
    q.add_argument("--dry-run", action="store_true")
    return {"lessons": _cmd}
