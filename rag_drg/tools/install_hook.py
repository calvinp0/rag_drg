"""``rag-drg install-hook``: add the input-check hook to Claude Code's settings.json.

Writing the hook by hand means putting a shell command into JSON, and small slips break it
silently (Claude Code only reports a non-blocking error), e.g. a leading ``/`` in front of an
environment assignment: ``/RAG_DRG_BIN=... rag-drg`` makes the shell look for a program named
``/RAG_DRG_BIN=...``. This command builds the line itself, runs it once before saving, and
replaces any earlier rag-drg hook (broken or not), leaving every other hook alone.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HOOK_ARGS = "check-input --hook"
MATCHER = "Write|Edit|MultiEdit"


def launcher() -> Path | None:
    """This checkout's bin/rag-drg (finds the venv/conda install itself), if there is one."""
    p = Path(__file__).resolve().parents[2] / "bin" / "rag-drg"
    return p if p.is_file() and os.access(p, os.X_OK) else None


def hook_command(env_bin: str | None = None) -> str:
    """The shell line for the hook: the absolute path of bin/rag-drg (or of the installed
    rag-drg), optionally preceded by a correctly written RAG_DRG_BIN=... assignment."""
    exe = launcher()
    if exe is None:
        found = shutil.which("rag-drg")
        if not found:
            raise SystemExit("install-hook: can't find bin/rag-drg or rag-drg on PATH")
        exe = Path(found).resolve()
    cmd = f"{shlex.quote(str(exe))} {HOOK_ARGS}"
    if env_bin and launcher() is not None:
        cmd = f"RAG_DRG_BIN={shlex.quote(env_bin)} {cmd}"
    return cmd


def check_command(cmd: str) -> str | None:
    """Run the hook line once as Claude Code would (sh -c, hook JSON on stdin). None if it works,
    else what went wrong."""
    try:
        r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "timed out after 120 s"
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip() or f"exit status {r.returncode}"
    return None


def _is_ours(hook: dict) -> bool:
    return HOOK_ARGS in str(hook.get("command", ""))


def merge(settings: dict, cmd: str) -> tuple[dict, int]:
    """Put our hook into `settings`, removing earlier copies. Returns (settings, n_replaced)."""
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault("PostToolUse", [])
    kept, replaced = [], 0
    for entry in entries:
        inner = entry.get("hooks", []) if isinstance(entry, dict) else []
        ours = [h for h in inner if isinstance(h, dict) and _is_ours(h)]
        if not ours:
            kept.append(entry)
            continue
        replaced += len(ours)
        rest = [h for h in inner if not (isinstance(h, dict) and _is_ours(h))]
        if rest:
            kept.append({**entry, "hooks": rest})
    kept.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": cmd}]})
    hooks["PostToolUse"] = kept
    return settings, replaced


def install(settings_path: Path, cmd: str, verify: bool = True, dry_run: bool = False,
            out=print) -> int:
    if verify:
        problem = check_command(cmd)
        if problem:
            out(f"install-hook: the hook command fails, nothing written:\n  {cmd}\n  {problem}")
            return 1
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError as e:
            out(f"install-hook: {settings_path} is not valid JSON ({e}); fix it first, nothing written")
            return 1
    else:
        settings = {}
    settings, replaced = merge(settings, cmd)
    text = json.dumps(settings, indent=2) + "\n"
    if dry_run:
        out(text)
        return 0
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_name(settings_path.name + ".bak"))
    settings_path.write_text(text)
    what = f"replaced {replaced} earlier rag-drg hook(s)" if replaced else "added"
    out(f"install-hook: {what} in {settings_path}\n  command: {cmd}")
    return 0


# ------------------------------------------------------------------ plugin hooks


def register_cli(subparsers):
    p = subparsers.add_parser(
        "install-hook",
        help="add the Claude Code input-check hook to settings.json (absolute path, tested first)")
    p.add_argument("--settings", help="settings file (default: ~/.claude/settings.json; "
                                      "--project: .claude/settings.json here)")
    p.add_argument("--project", action="store_true", help="install into ./.claude/settings.json")
    p.add_argument("--rag-drg-bin", help="also set RAG_DRG_BIN (only if bin/rag-drg can't find "
                                         "your install by itself)")
    p.add_argument("--dry-run", action="store_true", help="print the resulting settings, write nothing")
    p.add_argument("--no-verify", action="store_true", help="don't run the command once before saving")
    return {"install-hook": _cli}


def _cli(args, cfg) -> int:
    if args.settings:
        path = Path(args.settings).expanduser()
    elif args.project:
        path = Path.cwd() / ".claude" / "settings.json"
    else:
        path = Path.home() / ".claude" / "settings.json"
    cmd = hook_command(args.rag_drg_bin)
    return install(path, cmd, verify=not args.no_verify, dry_run=args.dry_run,
                   out=lambda m: print(m, file=sys.stderr if not args.dry_run else sys.stdout))
