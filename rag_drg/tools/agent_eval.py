"""Agent-task evaluation: can an agent do the group's real tasks correctly, with and without rag-drg?

    rag-drg agent-eval validate                      check eval/tasks.yaml
    rag-drg agent-eval check-graders                 graders pass every reference solution and fail
                                                     every known-bad one (the eval of the eval)
    rag-drg agent-eval run [--repeats 3] [--conditions with without] [--split dev] [--ids ...]
                           [--model M] [--out eval/runs/<id>]
    rag-drg agent-eval regrade RUN_DIR               re-grade a finished run with the current checks
    rag-drg agent-eval report RUN_DIR [--json]       pass rates with 95% intervals, per task and split

Tasks, graders and the with/without design: docs/agent-eval.md. Runs cost agent time (and API
money): start with --ids one-task --repeats 1.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from ._agent_eval.runner import (CONDITIONS, RunSpec, copy_reference, format_report, grade_run, load_results,
                                 regrade, run, summarize)
from ._agent_eval.tasks import DEFAULT_TASKS, load_suite, validate  # noqa: F401 - re-exported for tests


def _suite(cfg, path: str | None):
    p = Path(path) if path else Path(cfg.root) / DEFAULT_TASKS
    return load_suite(p)


def register_cli(subparsers) -> dict:
    p = subparsers.add_parser("agent-eval", help="run the group's agent tasks with and without rag-drg; grade; report",
                              description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", help=f"task file (default: {DEFAULT_TASKS} next to rag_drg.yaml)")
    sub = p.add_subparsers(dest="agent_eval_cmd", required=True)
    sub.add_parser("validate", help="check the task file")
    sub.add_parser("check-graders", help="graders must pass the reference and fail every bad/ solution")
    r = sub.add_parser("run", help="run the agent on the tasks")
    r.add_argument("--repeats", type=int, help="runs per task and condition (default from the task file, 3)")
    r.add_argument("--conditions", nargs="+", choices=CONDITIONS, default=list(CONDITIONS))
    r.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    r.add_argument("--ids", nargs="+", help="only these task ids")
    r.add_argument("--tags", nargs="+", help="only tasks with any of these tags")
    r.add_argument("--agent", choices=["claude", "codex"],
                   help="agent preset (default: Claude Code, or the task file's `agent:`)")
    r.add_argument("--model", help="agent model (Claude Code --model, Codex --model)")
    r.add_argument("--timeout", type=int, help="seconds per agent run (default from the task file, 900)")
    r.add_argument("--out", help="run directory (default eval/runs/<timestamp>); an existing one is resumed")
    g = sub.add_parser("regrade", help="re-grade a finished run with the current checks")
    g.add_argument("run_dir")
    rep = sub.add_parser("report", help="summarise a run")
    rep.add_argument("run_dir")
    rep.add_argument("--json", action="store_true")
    return {"agent-eval": cmd_agent_eval}


def cmd_agent_eval(args, cfg) -> int:
    try:
        suite = _suite(cfg, args.tasks)
    except (OSError, ValueError) as e:
        print(f"task file: {e}", file=sys.stderr)
        return 2
    c = args.agent_eval_cmd
    if c == "validate":
        dev = sum(t.split == "dev" for t in suite.tasks)
        print(f"{suite.path}: ok, {len(suite.tasks)} tasks ({dev} dev, {len(suite.tasks) - dev} holdout)")
        missing = [t.id for t in suite.tasks if not (suite.task_dir / t.id / "reference").is_dir()]
        if missing:
            print("no reference solution (add eval/tasks/<id>/reference/): " + ", ".join(missing), file=sys.stderr)
            return 1
        return 0
    if c == "check-graders":
        return check_graders(suite, cfg)
    if c == "run":
        tasks = [t for t in suite.tasks
                 if (args.split == "all" or t.split == args.split)
                 and (not args.ids or t.id in args.ids)
                 and (not args.tags or set(t.tags) & set(args.tags))]
        if not tasks:
            print("no tasks selected", file=sys.stderr)
            return 2
        out = Path(args.out) if args.out else Path(cfg.root) / "eval" / "runs" / time.strftime("%Y%m%d-%H%M%S")
        spec = RunSpec(suite, tasks, args.conditions, args.repeats or int(suite.defaults["repeats"]), out, cfg=cfg,
                       cfg_path=Path(cfg.root) / "rag_drg.yaml", model=args.model,
                       timeout_s=args.timeout or int(suite.defaults["timeout_s"]), agent_name=args.agent)
        print(f"{len(tasks)} task(s) x {len(spec.conditions)} condition(s) x {spec.repeats} repeat(s) -> {out}",
              file=sys.stderr)
        run(spec, progress=lambda m: print(m, file=sys.stderr))
        print(format_report(summarize(load_results(out))))
        return 0
    if c == "regrade":
        n = regrade(Path(args.run_dir), suite, cfg)
        print(f"re-graded {n} run(s)", file=sys.stderr)
        print(format_report(summarize(load_results(Path(args.run_dir)))))
        return 0
    if c == "report":
        s = summarize(load_results(Path(args.run_dir)))
        print(json.dumps(s, indent=2) if args.json else format_report(s))
        return 0
    return 2


def check_graders(suite, cfg) -> int:
    """Every task's reference must pass; every bad/<variant> must fail at least one check."""
    bad_count = 0
    problems = []
    for t in suite.tasks:
        base = suite.task_dir / t.id
        ref = base / "reference"
        if not ref.is_dir():
            problems.append(f"{t.id}: no reference solution ({ref})")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for name, text in t.files.items():
                (work / name).write_text(text)
            answer = copy_reference(ref, work)
            res = grade_run(t, work, answer, cfg)
        if not res["passed"]:
            problems.append(f"{t.id}: reference FAILS: " + " | ".join(res["failed"]))
        for bad in sorted((base / "bad").glob("*")) if (base / "bad").is_dir() else []:
            bad_count += 1
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                for name, text in t.files.items():
                    (work / name).write_text(text)
                answer = copy_reference(bad, work)
                res = grade_run(t, work, answer, cfg)
            if res["passed"]:
                problems.append(f"{t.id}: bad solution {bad.name!r} PASSES every check (graders too weak)")
    for p in problems:
        print(p)
    print(f"{len(suite.tasks)} reference(s), {bad_count} bad solution(s): "
          + ("ok" if not problems else f"{len(problems)} problem(s)"), file=sys.stderr)
    return 1 if problems else 0
