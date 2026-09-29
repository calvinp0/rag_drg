"""eval/tasks.yaml: the agent tasks, the agent command, and defaults.

    agent:                        # how to run the agent (placeholders filled per run)
      command: [claude, -p, "{prompt}", --mcp-config, "{mcp_config}", ...]
      allowed_tools: {with: "...", without: "..."}   # -> {allowed_tools}
    defaults: {repeats: 3, timeout_s: 900}
    tasks:
      - id: orca6-dlpno-sp-zeus
        split: dev | holdout      # tune the system on dev only; holdout says whether it generalises
        tags: [orca, zeus]
        prompt: |
          ...
        files: {input.gjf: "..."}  # optional files placed in the work directory first
        checks: [...]              # see graders.py; the task passes only if every check passes
        why: one line: what failure this task guards against

Each task has eval/tasks/<id>/reference/: a correct solution (files + answer.txt) that must pass
every check, and optionally eval/tasks/<id>/bad/<variant>/: wrong solutions that must fail at
least one check. `rag-drg agent-eval check-graders` verifies both (the eval of the eval).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .graders import CHECK_TYPES

DEFAULT_TASKS = "eval/tasks.yaml"
SPLITS = ("dev", "holdout")
PLACEHOLDERS = {"prompt", "mcp_config", "allowed_tools", "denied_tools", "workdir", "model"}


@dataclass
class Task:
    id: str
    prompt: str
    checks: list[dict]
    split: str = "dev"
    tags: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    why: str = ""


@dataclass
class Suite:
    path: Path
    tasks: list[Task]
    agent: dict
    defaults: dict

    @property
    def task_dir(self) -> Path:
        return self.path.parent / "tasks"


def load_suite(path: str | Path) -> Suite:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    problems = validate(raw)
    if problems:
        raise ValueError("; ".join(problems))
    tasks = [Task(id=t["id"], prompt=t["prompt"], checks=t["checks"], split=t.get("split", "dev"),
                  tags=list(t.get("tags") or []), files=dict(t.get("files") or {}), why=t.get("why", ""))
             for t in raw["tasks"]]
    return Suite(path, tasks, raw.get("agent") or {}, {"repeats": 3, "timeout_s": 900, **(raw.get("defaults") or {})})


def validate(raw) -> list[str]:
    p: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        return ["top level must be a mapping with a `tasks:` list"]
    cmd = (raw.get("agent") or {}).get("command")
    if cmd is not None:
        if not (isinstance(cmd, list) and all(isinstance(x, str) for x in cmd)):
            p.append("agent.command must be a list of strings")
        else:
            for x in cmd:
                for ph in re.findall(r"\{(\w+)\}", x):
                    if ph not in PLACEHOLDERS:
                        p.append(f"agent.command: unknown placeholder {{{ph}}} (known: {', '.join(sorted(PLACEHOLDERS))})")
    seen = set()
    for i, t in enumerate(raw["tasks"]):
        w = f"tasks[{i}]"
        if not isinstance(t, dict):
            p.append(f"{w}: must be a mapping")
            continue
        tid = t.get("id")
        if not (isinstance(tid, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", tid)):
            p.append(f"{w}: id must be lower-case letters, digits, . _ -")
        elif tid in seen:
            p.append(f"{w}: duplicate id {tid}")
        seen.add(tid)
        if not isinstance(t.get("prompt"), str) or not t["prompt"].strip():
            p.append(f"{w} ({tid}): prompt is required")
        if t.get("split", "dev") not in SPLITS:
            p.append(f"{w} ({tid}): split must be one of {SPLITS}")
        for name in (t.get("files") or {}):
            if "/" in name or name.startswith("."):
                p.append(f"{w} ({tid}): files: {name!r} must be a plain file name")
        checks = t.get("checks")
        if not isinstance(checks, list) or not checks:
            p.append(f"{w} ({tid}): at least one check is required")
            continue
        for j, c in enumerate(checks):
            cw = f"{w} ({tid}).checks[{j}]"
            if not isinstance(c, dict) or c.get("type") not in CHECK_TYPES:
                p.append(f"{cw}: type must be one of {', '.join(CHECK_TYPES)}")
                continue
            needs = {"file_exists": ["path"], "regex": ["path", "pattern"], "not_regex": ["path", "pattern"],
                     "number": ["path", "pattern"], "answer_regex": ["pattern"], "answer_not_regex": ["pattern"],
                     "check_input": ["path"], "arc_check": ["path"]}[c["type"]]
            for k in needs:
                if not isinstance(c.get(k), str) or not c[k]:
                    p.append(f"{cw}: `{k}` is required")
            if "pattern" in c and isinstance(c["pattern"], str):
                try:
                    re.compile(c["pattern"])
                except re.error as e:
                    p.append(f"{cw}: bad regex: {e}")
            if isinstance(c.get("path"), str) and (".." in Path(c["path"]).parts or c["path"].startswith("/")):
                p.append(f"{cw}: path must stay inside the work directory")
    return p
