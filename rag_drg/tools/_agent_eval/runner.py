"""Run tasks x conditions x repeats through an agent command, grade, and report.

Conditions:
    with     the agent gets rag-drg as an MCP server (bin/rag-drg serve)
    without  the agent gets no MCP servers
Both run in a fresh work directory in the system temp dir, outside the repository, so the repo's
.mcp.json and CLAUDE.md cannot leak rag-drg into the `without` condition. File tools are also
denied on the repository (and the config root), so the only way to rag-drg's knowledge is the MCP
server; the first real run showed a `without` agent reading knowledge/ straight from disk.
Answers of `without` runs that cite repository paths are counted as leaks in the report.
The finished work directory is copied to <rep>/work for inspection and re-grading.

Layout: <runs>/<run id>/<task>/<condition>/rep<k>/{work/, agent.out, agent.err, result.json}
"""

from __future__ import annotations

import json
import math
import os
import shutil
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .graders import grade
from .tasks import Suite, Task

CONDITIONS = ("with", "without")
REPO = Path(__file__).resolve().parents[3]

DEFAULT_AGENT = {
    # Claude Code headless. --bare / --setting-sources project keep your own hooks, plugins and
    # user settings out; --strict-mcp-config makes {mcp_config} the only MCP servers it sees.
    "command": ["claude", "-p", "{prompt}", "--output-format", "json", "--bare",
                "--setting-sources", "project", "--permission-mode", "acceptEdits",
                "--strict-mcp-config", "--mcp-config", "{mcp_config}",
                "--allowedTools", "{allowed_tools}", "--disallowedTools", "{denied_tools}"],
    "allowed_tools": {"with": "Read Write Edit Glob Grep mcp__rag-drg",
                      "without": "Read Write Edit Glob Grep"},
}


def mcp_config(condition: str, cfg_path: Path | None) -> dict:
    if condition == "without":
        return {"mcpServers": {}}
    server = {"command": str(REPO / "bin" / "rag-drg"), "args": ["serve"]}
    if cfg_path:
        server["env"] = {"RAG_DRG_CONFIG": str(cfg_path)}
    return {"mcpServers": {"rag-drg": server}}


FILE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
# repository paths a `without` answer should never know about
LEAK = re.compile(r"knowledge/|servers\.yaml|rag_drg\.yaml|sources_cache/|integrations/claude-code")


def denied_tools(hidden: list[Path]) -> str:
    """Claude Code permission rules that keep the file tools out of `hidden` ("//" = absolute path)."""
    return " ".join(f"{t}(/{Path(h).resolve()}/**)" for h in hidden for t in FILE_TOOLS)


def build_command(agent: dict, condition: str, prompt: str, mcp_path: Path, work: Path,
                  model: str | None, hidden: list[Path] | None = None) -> list[str]:
    tools = (agent.get("allowed_tools") or {}).get(condition, "")
    values = {"prompt": prompt, "mcp_config": str(mcp_path), "allowed_tools": tools,
              "denied_tools": denied_tools(hidden if hidden is not None else [REPO]),
              "workdir": str(work), "model": model or ""}
    cmd = []
    for part in agent["command"]:
        filled = part
        for k, v in values.items():
            filled = filled.replace("{" + k + "}", v)
        cmd.append(filled)
    if model and not any("{model}" in part for part in agent["command"]) and cmd and Path(cmd[0]).name == "claude":
        cmd += ["--model", model]
    return cmd


def parse_answer(stdout: str) -> tuple[str, dict]:
    """(final answer text, metadata). Claude Code --output-format json gives {result, ...};
    any other agent's stdout is taken as the answer."""
    s = stdout.strip()
    if s.startswith("{"):
        try:
            data = json.loads(s)
            meta = {k: data.get(k) for k in ("total_cost_usd", "num_turns", "duration_ms", "is_error", "subtype",
                                             "terminal_reason") if k in data}
            return str(data.get("result") or ""), meta
        except json.JSONDecodeError:
            pass
    return stdout, {}


@dataclass
class RunSpec:
    suite: Suite
    tasks: list[Task]
    conditions: list[str]
    repeats: int
    out: Path
    cfg: object = None
    cfg_path: Path | None = None
    model: str | None = None
    timeout_s: int = 900


def _auth_compatible(agent: dict) -> tuple[dict, str | None]:
    """Claude Code's --bare authenticates only with ANTHROPIC_API_KEY (it never reads the OAuth
    login of a Claude subscription). Without a key, drop --bare so the run can log in; user
    settings, hooks and MCP servers are still kept out by --setting-sources/--strict-mcp-config."""
    cmd = list(agent["command"])
    if "--bare" in cmd and not os.environ.get("ANTHROPIC_API_KEY"):
        cmd.remove("--bare")
        return {**agent, "command": cmd}, (
            "no ANTHROPIC_API_KEY, so running without --bare (OAuth login). Your ~/.claude/CLAUDE.md and "
            "skills are visible to both conditions alike; set ANTHROPIC_API_KEY for strict isolation.")
    return agent, None


def run(spec: RunSpec, progress=print) -> Path:
    agent = {**DEFAULT_AGENT, **(spec.suite.agent or {})}
    agent, isolation = _auth_compatible(agent)
    if isolation:
        progress(f"note: {isolation}")
    # the agent runs with cwd=<rep>/work, so every path handed to it (the MCP config) must be absolute
    spec.out = Path(spec.out).resolve()
    spec.out.mkdir(parents=True, exist_ok=True)
    (spec.out / "run.json").write_text(json.dumps({
        "tasks": [t.id for t in spec.tasks], "conditions": spec.conditions, "repeats": spec.repeats,
        "model": spec.model, "agent_command": agent["command"], "isolation_note": isolation,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))
    for cond in spec.conditions:
        mcp_path = spec.out / f"mcp-{cond}.json"
        mcp_path.write_text(json.dumps(mcp_config(cond, spec.cfg_path), indent=2))
    hidden = [REPO] + ([Path(spec.cfg_path).resolve().parent] if spec.cfg_path else [])
    hidden = list(dict.fromkeys(hidden))
    for task in spec.tasks:
        for cond in spec.conditions:
            for rep in range(1, spec.repeats + 1):
                d = spec.out / task.id / cond / f"rep{rep}"
                if (d / "result.json").exists() and not json.loads((d / "result.json").read_text()).get("agent_error"):
                    continue  # resume: keep graded runs, retry runs where the agent itself failed
                d.mkdir(parents=True, exist_ok=True)
                work = _scratch_dir(task.id)
                for name, text in task.files.items():
                    (work / name).write_text(text)
                cmd = build_command(agent, cond, task.prompt, spec.out / f"mcp-{cond}.json", work, spec.model,
                                    hidden)
                t0 = time.time()
                try:
                    p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=spec.timeout_s)
                    stdout, stderr, rc = p.stdout, p.stderr, p.returncode
                except subprocess.TimeoutExpired as e:
                    stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                    stderr, rc = f"timeout after {spec.timeout_s} s", -1
                except FileNotFoundError as e:
                    stdout, stderr, rc = "", f"cannot start the agent: {e}", -2
                (d / "agent.out").write_text(stdout)
                (d / "agent.err").write_text(stderr)
                answer, meta = parse_answer(stdout)
                (d / "answer.txt").write_text(answer)
                result = grade_run(task, work, answer, spec.cfg)
                leaks = sorted(set(LEAK.findall(answer))) if cond == "without" else []
                result.update(task=task.id, split=task.split, condition=cond, rep=rep, returncode=rc,
                              seconds=round(time.time() - t0, 1), agent=meta,
                              agent_error=agent_error(rc, meta, stderr, stdout), leaks=leaks)
                shutil.rmtree(d / "work", ignore_errors=True)
                shutil.copytree(work, d / "work")
                shutil.rmtree(work, ignore_errors=True)
                (d / "result.json").write_text(json.dumps(result, indent=2))
                if result["agent_error"]:
                    progress(f"{task.id:32s} {cond:8s} rep{rep}: AGENT ERROR  ({result['agent_error']})")
                else:
                    progress(f"{task.id:32s} {cond:8s} rep{rep}: {'PASS' if result['passed'] else 'fail'}"
                             + ("" if result["passed"] else f"  ({result['failed'][0]})"))
    return spec.out


def _scratch_dir(task_id: str) -> Path:
    """A fresh work directory outside the repository (never under it, even if TMPDIR is)."""
    for base in (None, "/tmp"):
        work = Path(tempfile.mkdtemp(prefix=f"rag-drg-agent-eval-{task_id}-", dir=base)).resolve()
        if not work.is_relative_to(REPO):
            return work
        shutil.rmtree(work, ignore_errors=True)
    raise RuntimeError(f"no temp directory outside {REPO}; set TMPDIR")


def agent_error(rc: int, meta: dict, stderr: str, stdout: str = "") -> str | None:
    """Why the agent itself failed to run (not a task failure): these runs are left out of the pass
    rates, because an outage, a missing login or a bad agent config says nothing about rag-drg."""
    if rc in (-1, -2):
        return stderr.strip()[:200] or "agent did not run"
    if rc != 0 and not stdout.strip():
        # exited with an error and produced nothing, e.g. "Invalid MCP configuration"
        return f"agent exited {rc} without output: {stderr.strip()[:200]}".rstrip(": ")
    if meta.get("terminal_reason") in ("api_error",) or meta.get("subtype") in ("error_during_execution",):
        return f"agent error: {meta.get('terminal_reason') or meta.get('subtype')}"
    if meta.get("is_error") and not meta.get("num_turns"):
        return "agent error before any turn"
    return None


def grade_run(task: Task, work: Path, answer: str, cfg) -> dict:
    checks = grade(task.checks, work, answer, cfg)
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    return {"passed": not failed, "checks": [c.to_dict() for c in checks], "failed": failed}


def regrade(run_dir: Path, suite: Suite, cfg) -> int:
    """Re-grade a finished run with the current checks (graders change; runs are expensive)."""
    by_id = {t.id: t for t in suite.tasks}
    n = 0
    for res_path in sorted(run_dir.glob("*/*/rep*/result.json")):
        old = json.loads(res_path.read_text())
        task = by_id.get(old.get("task"))
        if task is None:
            continue
        d = res_path.parent
        answer = (d / "answer.txt").read_text() if (d / "answer.txt").exists() else ""
        old.update(grade_run(task, d / "work", answer, cfg))
        old["leaks"] = sorted(set(LEAK.findall(answer))) if old.get("condition") == "without" else []
        res_path.write_text(json.dumps(old, indent=2))
        n += 1
    return n


# ------------------------------------------------------------------ report


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n (sensible for small n and 0 or n)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_results(run_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(run_dir.glob("*/*/rep*/result.json"))]


def summarize(all_results: list[dict]) -> dict:
    errors = [r for r in all_results if r.get("agent_error")]
    results = [r for r in all_results if not r.get("agent_error")]
    tasks = sorted({r["task"] for r in results})
    conds = [c for c in CONDITIONS if any(r["condition"] == c for r in results)]
    per_task = {}
    for t in tasks:
        row = {"split": next(r["split"] for r in results if r["task"] == t)}
        for c in conds:
            rs = [r for r in results if r["task"] == t and r["condition"] == c]
            k = sum(r["passed"] for r in rs)
            row[c] = {"passed": k, "n": len(rs), "all": bool(rs) and k == len(rs)}
        per_task[t] = row
    overall = {}
    for split in ("all", "dev", "holdout"):
        for c in conds:
            rs = [r for r in results if r["condition"] == c and (split == "all" or r["split"] == split)]
            if not rs:
                continue
            k, n = sum(r["passed"] for r in rs), len(rs)
            lo, hi = wilson(k, n)
            tasks_c = {r["task"] for r in rs}
            all_pass = sum(1 for t in tasks_c if per_task[t][c]["all"])
            overall.setdefault(split, {})[c] = {"passed": k, "n": n, "rate": k / n, "ci95": [lo, hi],
                                                "tasks_all_reps": all_pass, "tasks": len(tasks_c)}
    fails: dict[str, int] = {}
    for r in results:
        for f in r.get("failed", []):
            key = f"{r['condition']} | {r['task']} | {f.split(': ', 1)[0]}"
            fails[key] = fails.get(key, 0) + 1
    cost = {c: sum((r.get("agent") or {}).get("total_cost_usd") or 0 for r in results if r["condition"] == c)
            for c in conds}
    # a `without` run that passes while citing repo paths probably read them; one that only asks
    # the user for e.g. servers.yaml and fails is not a leak
    leaks = [{"task": r["task"], "rep": r["rep"], "paths": r["leaks"]} for r in results
             if r.get("leaks") and r.get("passed")]
    return {"tasks": per_task, "overall": overall, "top_failures": sorted(fails.items(), key=lambda x: -x[1])[:15],
            "conditions": conds, "cost_usd": cost, "leaks": leaks,
            "agent_errors": [{"task": r["task"], "condition": r["condition"], "rep": r["rep"], "error": r["agent_error"]}
                             for r in errors]}


def format_report(s: dict) -> str:
    conds = s["conditions"]
    lines = ["# Agent-task eval", ""]
    if s.get("agent_errors"):
        lines += [f"**{len(s['agent_errors'])} run(s) where the agent itself failed** (login, network, timeout) are "
                  "left out of the rates below; fix the cause and run again with `--out <this run>`: graded runs are "
                  "kept and these are retried. First: " + s["agent_errors"][0]["error"], ""]
    if s.get("leaks"):
        first = s["leaks"][0]
        lines += [f"**Warning: {len(s['leaks'])} passing `without` answer(s) cite repository paths** (first: "
                  f"{first['task']} rep{first['rep']}: {', '.join(first['paths'])}). That agent saw rag-drg's "
                  "knowledge some other way, so the comparison is not clean; check those runs.", ""]
    if not s["overall"]:
        return "\n".join(lines + ["No graded runs yet.", ""])
    lines += ["| Split | " + " | ".join(f"{c}: pass rate (95% CI) | {c}: tasks passing every rep" for c in conds) + " |",
              "|---|" + "---|---|" * len(conds)]
    for split, row in s["overall"].items():
        cells = []
        for c in conds:
            o = row.get(c)
            cells += ["-", "-"] if o is None else [
                f"{o['passed']}/{o['n']} = {o['rate']:.0%} ({o['ci95'][0]:.0%}-{o['ci95'][1]:.0%})",
                f"{o['tasks_all_reps']}/{o['tasks']}"]
        lines.append(f"| {split} | " + " | ".join(cells) + " |")
    lines += ["", "| Task | Split | " + " | ".join(conds) + " |", "|---|---|" + "---|" * len(conds)]
    for t, row in s["tasks"].items():
        cells = [f"{row[c]['passed']}/{row[c]['n']}" if c in row else "-" for c in conds]
        lines.append(f"| {t} | {row['split']} | " + " | ".join(cells) + " |")
    if s["top_failures"]:
        lines += ["", "Most frequent failed checks (read these runs first):", ""]
        lines += [f"- {n}x {k}" for k, n in s["top_failures"]]
    if any(s["cost_usd"].values()):
        lines += ["", "Cost (USD): " + ", ".join(f"{c} {v:.2f}" for c, v in s["cost_usd"].items())]
    lines += ["", "Small samples: a 95% interval that overlaps between conditions is not a difference. "
              "Add tasks or repeats before drawing conclusions; judge the system on `holdout`."]
    return "\n".join(lines) + "\n"


def copy_reference(src: Path, work: Path) -> str:
    """Copy a reference/bad solution into `work`; returns its answer.txt text."""
    answer = ""
    for f in src.iterdir():
        if f.name == "answer.txt":
            answer = f.read_text()
        elif f.is_file():
            shutil.copy(f, work / f.name)
    return answer
