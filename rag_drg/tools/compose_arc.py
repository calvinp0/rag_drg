"""Compose an ARC run on a cluster where ARC itself runs as a batch job (docs/arc-run.md).

Given an ARC input.yml and a servers.yaml cluster with an `arc.runner` block, produce:

* `submit.sh` - the runner job (queue, optional node pin, conda env, `python ARC.py input.yml`),
* `arc_settings.py` - the cluster as ARC's `'local'` server + `global_ess_settings` (merge into
  ~/.arc/settings.py),
* `arc_submit.py` - `submit_scripts['local'][ess]` (merge into ~/.arc/submit.py),

and cross-check the input against what ARC will do on that cluster (arc/job/adapter.py):
ESS jobs go to the first queue of `servers['local']['queues']` (= servers.yaml `arc.ess_queues`;
the others only after a walltime kill, via ARC's queue troubleshooting), `job_memory` (GB,
default 14) is capped at 95% of the server's `memory` and requested as
ceil(job_memory * 1024 * 1.10) MiB (1.05 when capped), a job gets min(8, cpus) cores, and
`max_job_time` (hours, default 120; values <= 0 or > 9999 become 120) is the job's walltime.
Errors when the job fits no usable queue, warnings for the queues it does not fit.

    rag-drg arc compose input.yml --server zeus [--out-dir DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import yaml

from ._arc.check import load_arc_yaml
from ._servers.access import queue_access
from ._servers.arc_runner import render_arc_runner_script
from ._servers.model import Server, ServersConfigError, load_servers, servers_path
from ._servers.render import ARC_ESS, arc_ess_queues, arc_settings_parts
from ._servers.submit import SubmitError

# arc/settings/settings.py default_job_settings and arc/job/adapter.py constants
ARC_DEFAULT_JOB_MEMORY_GB = 14
ARC_DEFAULT_MAX_JOB_TIME_H = 120
ARC_DEFAULT_JOB_CPUS = 8
ARC_MAX_NODE_MEMORY_FRACTION = 0.95
ARC_MEMORY_OVERHEAD = 1.10
ARC_CAPPED_MEMORY_OVERHEAD = 1.05
# ESS names ARC accepts in ess_settings (supported_ess + the extra adapters in arc/common.py
# check_ess_settings)
ARC_KNOWN_ESS = {"cfour", "gaussian", "mockter", "molpro", "orca", "qchem", "terachem", "onedmin", "xtb",
                 "torchani", "openbabel", "ase", "pyscf", "gcn", "goflow", "heuristics", "autotst", "kinbot",
                 "rits", "xtb_gsm", "orca_neb"}
# ESS that ARC submits to the server's queue with submit_scripts[server][ess]
QUEUE_ESS = {"cfour", "gaussian", "molpro", "orca", "qchem", "terachem", "onedmin"}
# ESS run as ORCA jobs (orca_neb uses the ORCA install)
_ESS_INSTALL = {"orca_neb": "orca"}

OUT_FILES = ("submit.sh", "arc_settings.py", "arc_submit.py")


def _f(severity: str, code: str, message: str, fix: str | None = None) -> dict:
    d = {"severity": severity, "code": code, "message": message}
    if fix:
        d["fix"] = fix
    return d


def _num(v, default: float) -> float | None:
    if v is None:
        return float(default)
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def arc_job_request(server: Server, job_mem: float, queues: list) -> dict:
    """What ARC asks the scheduler for per ESS job (arc/job/adapter.py set_cpu_and_mem):
    cores = min(job_cpu_cores (8), servers['local']['cpus']); memory capped at 95% of servers['local']['memory'],
    requested as ceil(GB x 1024 x 1.10) MiB (x 1.05 when capped). `queues`: the ESS queues in
    servers['local']['queues'] order; cpus/memory come from the first (arc_local_entry)."""
    first = queues[0] if queues else None
    arc_mem = float(first.mem_per_node_gb) if first else None
    node_cpus = first.cores_per_node if first else None
    # servers.yaml arc.cpus / arc.memory_gb override the node size (what arc_local_entry writes)
    if server.arc.get("cpus") is not None:
        node_cpus = int(server.arc["cpus"])
    if server.arc.get("memory_gb") is not None:
        arc_mem = float(server.arc["memory_gb"])
    want = _num((server.arc.get("default_job_settings") or {}).get("job_cpu_cores"), ARC_DEFAULT_JOB_CPUS)
    cores = int(min(want, node_cpus)) if node_cpus else int(want)
    capped = arc_mem is not None and job_mem > ARC_MAX_NODE_MEMORY_FRACTION * arc_mem
    eff = ARC_MAX_NODE_MEMORY_FRACTION * arc_mem if capped else job_mem
    overhead = ARC_CAPPED_MEMORY_OVERHEAD if capped else ARC_MEMORY_OVERHEAD
    return {"cores": cores, "memory_gb": eff, "memory_mib": math.ceil(eff * 1024 * overhead),
            "overhead": overhead, "capped": capped, "arc_memory": arc_mem}


def queue_fit(server: Server, queues: list, job_mem: float, max_time_h: float, user=None, groups=None) -> list[dict]:
    """Per ESS queue: {queue, usable (True/False/None), fits, reasons} for ARC's request.

    A queue the identity may not use is `usable: False` and is not in ARC's settings, so the
    request (cpus/memory of ARC's default queue) is computed from the usable ones."""
    ident = user is not None or groups is not None
    usable = {p.name: (True if p.access is None else
                       queue_access(server, p, user, groups)["allowed"] if ident else None) for p in queues}
    req = arc_job_request(server, job_mem, [p for p in queues if usable[p.name] is not False])
    rows = []
    for p in queues:
        reasons = []
        if req["memory_mib"] > p.mem_per_node_gb * 1024:
            reasons.append(f"{req['memory_mib']} MiB > {p.mem_per_node_gb:g} GB per node")
        if max_time_h * 3600 > p.max_walltime_seconds:
            reasons.append(f"max_job_time {max_time_h:g} h > max walltime {p.max_walltime}")
        if req["cores"] > p.cores_per_node:
            reasons.append(f"{req['cores']} cores > {p.cores_per_node} per node")
        rows.append({"queue": p.name, "usable": usable[p.name], "fits": not reasons, "reasons": reasons})
    return rows


def _fit_findings(server: Server, queues: list, job_mem: float, max_time_h: float, user, groups) -> list[dict]:
    out: list[dict] = []
    rows = queue_fit(server, queues, job_mem, max_time_h, user, groups)
    req = arc_job_request(server, job_mem, [p for p, r in zip(queues, rows) if r["usable"] is not False])
    if req["capped"]:
        out.append(_f("warning", "arc-job-memory-capped",
                      f"job_memory {job_mem:g} GB > 95% of {req['arc_memory']:g} GB (a node of ARC's default queue, "
                      f"servers['local']['memory']); ARC silently caps every job at {req['memory_gb']:.1f} GB",
                      fix=f"set job_memory <= {math.floor(ARC_MAX_NODE_MEMORY_FRACTION * req['arc_memory'])}"))
    usable = [r for r in rows if r["usable"] is not False]
    denied = [r["queue"] for r in rows if r["usable"] is False]
    fits = [r for r in usable if r["fits"]]
    misfit = [r for r in usable if not r["fits"]]
    what = (f"an ESS job (job_memory {job_mem:g} GB -> {req['memory_mib']} MiB requested, "
            f"max_job_time {max_time_h:g} h, {req['cores']} cores)")
    if not fits:
        detail = "; ".join(f"{r['queue']}: {', '.join(r['reasons'])}" for r in misfit) or "no usable queue"
        out.append(_f("error", "arc-job-fits-no-queue",
                      f"{what} fits none of the ESS queues ARC may use" + (f" (no access: {', '.join(denied)})" if denied else "")
                      + f" - {detail}",
                      fix="lower job_memory / max_job_time, or add a bigger queue to arc.ess_queues"))
    elif misfit:
        first = usable[0]
        lead = (f"{first['queue']} is ARC's default queue (every job is submitted there first), and " if
                first in misfit else "")
        out.append(_f("warning", "arc-job-misfits-some-queues",
                      f"{what} does not fit {', '.join(r['queue'] for r in misfit)} ("
                      + "; ".join(f"{r['queue']}: {', '.join(r['reasons'])}" for r in misfit)
                      + f"). {lead}ARC's queue troubleshooting may move a job there after a walltime kill; "
                      f"fits: {', '.join(r['queue'] for r in fits)}"))
    unknown = [r["queue"] for r in rows if r["usable"] is None]
    if unknown:
        out.append(_f("info", "arc-queue-access-unknown",
                      f"restricted ESS queue(s) {', '.join(unknown)}: pass user/groups to check that you may use them"))
    if denied:
        out.append(_f("info", "arc-queue-access-denied",
                      f"left out of servers['local']['queues'] (no access): {', '.join(denied)}"))
    return out


def _ess_findings(server: Server, ess_settings, other_servers: dict[str, Server], runner_cores: int) -> list[dict]:
    out: list[dict] = []
    if ess_settings is None:
        out.append(_f("info", "arc-ess-settings-global",
                      "no ess_settings in the input: ARC uses global_ess_settings from ~/.arc/settings.py "
                      "(arc_settings.py routes every installed ESS to 'local')"))
        return out
    if not isinstance(ess_settings, dict):
        return [_f("error", "arc-ess-settings-type", "ess_settings must be a mapping of ESS -> server or list of servers")]
    installed = {sw.ess for sw in server.software.values()}
    for ess, targets in ess_settings.items():
        e = str(ess).lower()
        if e not in ARC_KNOWN_ESS:
            out.append(_f("error", "arc-ess-unknown", f"ess_settings: ARC does not know ESS {ess!r} "
                                                      f"(known: {', '.join(sorted(ARC_KNOWN_ESS))})"))
            continue
        tlist = [targets] if isinstance(targets, str) else targets
        if not isinstance(tlist, list) or not all(isinstance(t, str) for t in tlist):
            out.append(_f("error", "arc-ess-settings-type", f"ess_settings.{ess}: must be a server name or a list"))
            continue
        for t in tlist:
            if t == server.name:
                out.append(_f("error", "arc-ess-self-remote",
                              f"ess_settings.{ess}: {t!r} - ARC runs on {server.name}, where it is the 'local' server; "
                              "arc_settings.py has no remote entry for it",
                              fix=f"use 'local' for {ess}"))
            elif t == "local":
                need = _ESS_INSTALL.get(e, e)
                if e in QUEUE_ESS or e in _ESS_INSTALL:
                    if need not in installed:
                        out.append(_f("error", "arc-ess-not-installed",
                                      f"ess_settings.{ess}: 'local' is {server.name}, which has no {need} install in "
                                      f"servers.yaml (installed: {', '.join(sorted(installed)) or 'none'})",
                                      fix=f"route {ess} to a server that has it, or add the install to servers.yaml"))
                    elif need not in ARC_ESS:
                        out.append(_f("error", "arc-ess-no-template",
                                      f"ess_settings.{ess}: no submit_scripts['local'][{need!r}] is generated for it; "
                                      "write one in ~/.arc/submit.py"))
                else:
                    out.append(_f("info", "arc-ess-incore",
                                  f"ess_settings.{ess}: runs in-core inside the runner job ({runner_cores} core(s)); "
                                  "it must be installed in ARC's env, and the runner may need more cores/memory"))
            elif t in other_servers:
                out.append(_f("warning", "arc-ess-other-server",
                              f"ess_settings.{ess}: {t!r} is another cluster; ARC reaches it over SSH from the runner "
                              f"node, and arc_settings.py has no entry for it",
                              fix=f"add it with `rag-drg servers arc-settings {server.name} {t}`"))
            else:
                out.append(_f("error", "arc-ess-unknown-server",
                              f"ess_settings.{ess}: server {t!r} is neither 'local' nor in servers.yaml; ARC raises "
                              "SettingsError for servers missing from ~/.arc/settings.py"))
    return out


def _arc_input_findings(content: str, input_file: str, cfg) -> list[dict]:
    try:
        from .arc_input import check_arc_input  # written separately; optional
    except ImportError:
        return []
    try:
        found = check_arc_input(content, input_file, cfg)
    except Exception as e:  # noqa: BLE001 - the checker must not break composing
        return [_f("info", "arc-input-checker-failed", f"check_arc_input failed: {e}")]
    out = []
    for f in found or []:
        d = f.to_dict() if hasattr(f, "to_dict") else dict(vars(f))
        d.setdefault("source", "check_arc_input")
        out.append(d)
    return out


def compose_arc_run(input_content: str, server: Server | str, input_file: str = "input.yml", *, cfg=None,
                    servers: dict[str, Server] | None = None, user: str | None = None,
                    groups: list[str] | None = None, arc_path: str | None = None, conda_env: str | None = None,
                    conda_sh: str | None = None, use_local: bool | None = None, **overrides) -> dict:
    """Runner submit script, ARC settings snippets and cross-checks for one ARC run.

    Returns {"submit_sh", "arc_settings_py", "arc_submit_py", "findings": [{severity, code,
    message, fix?}], "notes": [...]}; submit_sh is None when the runner cannot be rendered.
    `server` is a Server or a servers.yaml name (then `servers` or `cfg` is needed).
    `overrides` replace arc.runner fields (queue, host, cores, mem_gb, walltime, ...).
    `arc_path`/`conda_env`/`conda_sh` are the per-user values; missing ones come from
    ~/.config/rag-drg/user.yaml and the environment unless `use_local` is False (default: not in
    RAG_DRG_SERVER_MODE), else the script resolves them at run time (docs/arc-run.md).
    """
    if servers is None:
        servers = load_servers(cfg) if cfg is not None else {}
    if isinstance(server, str):
        if server not in servers:
            return {"submit_sh": None, "arc_settings_py": None, "arc_submit_py": None, "notes": [],
                    "findings": [_f("error", "unknown-server",
                                    f"unknown server {server!r}; known: {', '.join(servers) or '(none)'}")]}
        server = servers[server]
    findings: list[dict] = []
    notes: list[str] = []

    try:
        data = load_arc_yaml(input_content)
    except yaml.YAMLError as e:
        data = None
        findings.append(_f("error", "arc-input-yaml", f"{input_file} is not valid YAML: {e}"))
    if data is not None and not isinstance(data, dict):
        findings.append(_f("error", "arc-input-yaml", f"{input_file} must be a YAML mapping"))
        data = None
    data = data or {}
    project = data.get("project")
    if not project and not any(f["code"] == "arc-input-yaml" for f in findings):
        findings.append(_f("error", "arc-input-project", "no `project:` - ARC.py stops with 'A project name must be provided!'"))

    # runner job
    job_name = f"ARC_{project}" if isinstance(project, str) and project else None
    submit_sh = None
    try:
        submit_sh, rnotes = render_arc_runner_script(server, input_file, job_name, user=user, groups=groups,
                                                     arc_path=arc_path, conda_env=conda_env, conda_sh=conda_sh,
                                                     use_local=use_local, **overrides)
        notes += rnotes
    except SubmitError as e:
        for p in e.problems:
            findings.append(_f(p["severity"], "arc-runner", p["message"]))
    runner_cores = int(overrides.get("cores") or (server.arc_runner.cores if server.arc_runner else 1))

    # ARC settings
    arc_settings_py = arc_submit_py = None
    try:
        arc_settings_py, arc_submit_py = arc_settings_parts({server.name: server}, [server.name], local=server.name,
                                                            user=user, groups=groups)
    except (KeyError, ValueError) as e:
        findings.append(_f("error", "arc-settings", str(e.args[0] if e.args else e)))

    # what ARC will do with the input on this cluster
    queues, _excluded = arc_ess_queues(server)
    usable_q = arc_ess_queues(server, user, groups)[0]  # = servers['local']['queues'] in arc_settings.py
    if usable_q:
        notes.append("ARC submits every ESS job to " + usable_q[0].name
                     + (f"; after a walltime kill its queue troubleshooting may move it to "
                        f"{', '.join(p.name for p in usable_q[1:])}" if len(usable_q) > 1 else ""))
    if queues:
        default_mem = _num((server.arc.get("default_job_settings") or {}).get("job_total_memory_gb"),
                           ARC_DEFAULT_JOB_MEMORY_GB)
        mem = _num(data.get("job_memory"), default_mem)
        if mem is None or mem <= 0:
            findings.append(_f("error", "arc-job-memory", f"job_memory {data.get('job_memory')!r} must be a positive number (GB)"))
        t = _num(data.get("max_job_time"), ARC_DEFAULT_MAX_JOB_TIME_H)
        if t is None:
            findings.append(_f("error", "arc-max-job-time", f"max_job_time {data.get('max_job_time')!r} must be hours"))
        elif t <= 0 or t > 9999:
            findings.append(_f("warning", "arc-max-job-time",
                               f"max_job_time {t:g} is <= 0 or > 9999; ARC replaces it with {ARC_DEFAULT_MAX_JOB_TIME_H} h"))
            t = ARC_DEFAULT_MAX_JOB_TIME_H
        if mem is not None and mem > 0 and t is not None:
            findings += _fit_findings(server, queues, mem, t, user, groups)
            req = arc_job_request(server, mem, usable_q)
            notes.append(f"ARC gives each ESS job {req['cores']} cores (min(job_cpu_cores, servers['local']['cpus'])) and "
                         f"requests {req['memory_mib']} MiB")
    else:
        findings.append(_f("error", "arc-no-queues", f"{server.name} has no queue ARC could submit ESS jobs to"))
    findings += _ess_findings(server, data.get("ess_settings"), {n: s for n, s in servers.items() if n != server.name},
                              runner_cores)
    if data.get("project_directory"):
        notes.append(f"project_directory {data['project_directory']!r} is used instead of the input's directory; "
                     "it must be a path on the cluster")
    findings += _arc_input_findings(input_content, input_file, cfg)

    notes += [
        "arc_settings.py: merge `servers['local']` and `global_ess_settings` into ~/.arc/settings.py on the cluster "
        "(a top-level name there replaces ARC's default)",
        "arc_submit.py: merge `submit_scripts['local']` into ~/.arc/submit.py on the cluster",
        "ARC submits the ESS jobs from the runner node with qsub/sbatch: the cluster must accept submissions from "
        "compute nodes (ARC stops with 'PBS job submission attempted from a compute node' otherwise)",
    ]
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.get("severity"), 3))
    return {"submit_sh": submit_sh, "arc_settings_py": arc_settings_py, "arc_submit_py": arc_submit_py,
            "findings": findings, "notes": notes}


def format_result(res: dict) -> str:
    lines = []
    for f in res["findings"]:
        lines.append(f"{f['severity']}: [{f.get('code', '')}] {f['message']}" + (f" (fix: {f['fix']})" if f.get("fix") else ""))
    lines += [f"note: {n}" for n in res["notes"]]
    return "\n".join(lines) or "ok"


# ----------------------------------------------------------------- CLI

def _add_compose(sub) -> None:
    q = sub.add_parser("compose", help="runner submit.sh + ARC settings for an ARC run on a cluster, with checks")
    q.add_argument("input", help="ARC input.yml")
    q.add_argument("--server", required=True, help="servers.yaml cluster ARC runs on (needs arc.runner)")
    q.add_argument("--servers-file", help="servers file instead of <repo>/servers.yaml")
    q.add_argument("--out-dir", help="where to write submit.sh, arc_settings.py, arc_submit.py (default: the input's directory)")
    q.add_argument("--json", action="store_true", help="print the whole result as JSON (nothing is written)")
    q.add_argument("--force", action="store_true", help="overwrite existing files; write even when there are errors")
    q.add_argument("--queue")
    q.add_argument("--host", help="pin the runner to this node")
    q.add_argument("--cores", type=int)
    q.add_argument("--mem", type=float, dest="mem_gb")
    q.add_argument("--time", dest="walltime")
    q.add_argument("--arc-path", help="your ARC clone (default: ~/.config/rag-drg/user.yaml, then $ARC_PATH)")
    q.add_argument("--conda-env", help="conda env with ARC's dependencies (default: user.yaml, then arc_env)")
    q.add_argument("--conda-sh", help="your <conda base>/etc/profile.d/conda.sh (default: user.yaml, then $CONDA_EXE)")
    q.add_argument("--user", help="Unix user on the cluster (restricted queues)")
    q.add_argument("--groups", help="comma-separated Unix groups on the cluster")


def register_cli(subparsers):
    from ._arc.cli import add_arc_command, arc_group, dispatch

    _add_compose(arc_group(subparsers))
    add_arc_command("compose", _cli)
    return {"arc": dispatch}


def _within(path: Path, other: Path) -> bool:
    try:
        path.resolve().relative_to(other.resolve())
        return True
    except ValueError:
        return False


def _cli(args: argparse.Namespace, cfg) -> int:
    if getattr(args, "arc_cmd", None) != "compose":
        return 2
    inp = Path(args.input)
    if not inp.is_file():
        print(f"{inp}: not found", file=sys.stderr)
        return 2
    try:
        servers = load_servers(cfg, Path(args.servers_file) if args.servers_file else servers_path(cfg))
    except ServersConfigError as e:
        print(e, file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir) if args.out_dir else inp.parent
    if _within(out_dir, Path.home() / ".arc"):
        print("refusing to write into ~/.arc; merge arc_settings.py / arc_submit.py there by hand", file=sys.stderr)
        return 2
    # the input as the runner sees it from the submit directory (= out_dir)
    copy_note = None
    try:
        rel = inp.resolve().relative_to(out_dir.resolve()).as_posix()
    except ValueError:  # the runner reads the input from the submit directory (= ARC's project directory)
        rel = inp.name
        copy_note = f"copy {inp} to {out_dir / inp.name} (submit.sh runs ARC on {inp.name} in its own directory)"
    groups = [g for g in args.groups.split(",") if g] if args.groups else None
    overrides = {k: getattr(args, k) for k in ("queue", "host", "cores", "mem_gb", "walltime")
                 if getattr(args, k) is not None}
    res = compose_arc_run(inp.read_text(), args.server, rel, cfg=cfg, servers=servers,
                          user=args.user, groups=groups, arc_path=args.arc_path, conda_env=args.conda_env,
                          conda_sh=args.conda_sh, **overrides)
    errors = any(f["severity"] == "error" for f in res["findings"])
    if args.json:
        print(json.dumps(res, indent=1))
        return 1 if errors else 0
    print(format_result(res), file=sys.stderr)
    if errors and not args.force:
        print("not writing files because of the errors above (--force writes anyway)", file=sys.stderr)
        return 1
    contents = dict(zip(OUT_FILES, (res["submit_sh"], res["arc_settings_py"], res["arc_submit_py"])))
    targets = {name: out_dir / name for name, text in contents.items() if text is not None}
    clash = [str(p) for p in targets.values() if p.exists()]
    if clash and not args.force:
        print(f"not overwriting {', '.join(clash)} (use --force)", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, path in targets.items():
        path.write_text(contents[name])
        print(f"wrote {path}")
    if copy_note:
        print(f"note: {copy_note}")
    submit = "sbatch" if getattr(servers.get(args.server), "scheduler", "") == "slurm" else "qsub"
    print(f"next: merge {out_dir / 'arc_settings.py'} into ~/.arc/settings.py and {out_dir / 'arc_submit.py'} into "
          f"~/.arc/submit.py on {args.server} (once), then `cd {out_dir} && {submit} submit.sh`")
    return 1 if errors else 0


# ----------------------------------------------------------------- MCP

def register_mcp(mcp, ctx) -> None:
    cfg = ctx.cfg

    @mcp.tool(name="compose_arc_run")
    def compose_arc_run_tool(input_content: str, server: str, input_file: str = "input.yml",
                             arc_path: str | None = None, conda_env: str | None = None,
                             conda_sh: str | None = None, user: str | None = None,
                             groups: list[str] | None = None) -> str:
        """Prepare an ARC run where ARC itself runs as a batch job on a cluster (servers.yaml
        `arc.runner`, e.g. zeus). Returns JSON {submit_sh, arc_settings_py, arc_submit_py, findings,
        notes}: the runner submit script (queue, node pin, conda env, python ARC.py input.yml), the
        cluster as ARC's 'local' server for ~/.arc/settings.py, submit_scripts['local'] for
        ~/.arc/submit.py, and checks of input.yml (job_memory / max_job_time vs the ESS queues,
        ess_settings vs the installed ESS). Fix every error before submitting.

        The ARC clone and conda install are per user and a shared server does not know them:
        pass arc_path / conda_env / conda_sh (e.g. from the user's ~/.config/rag-drg/user.yaml or
        `echo $ARC_PATH; conda info --base` on the cluster). Without them the script reads
        $ARC_PATH and finds conda at run time, and stops with a message if they are missing.

        Args:
            input_content: The full text of ARC's input.yml.
            server: Cluster name from list_servers() that ARC runs on.
            input_file: The input's path relative to the submit directory (default input.yml).
            arc_path: Absolute path of the user's ARC clone on the cluster (holds ARC.py). Optional.
            conda_env: Conda env with ARC's dependencies (default arc_env).
            conda_sh: Absolute path of <conda base>/etc/profile.d/conda.sh on the cluster. Optional.
            user: Your Unix user on the cluster (restricted queues). Optional.
            groups: Your Unix groups on the cluster. Optional.
        """
        try:
            servers = load_servers(cfg)
        except ServersConfigError as e:
            return str(e)
        res = compose_arc_run(input_content, server, input_file, cfg=cfg, servers=servers, user=user, groups=groups,
                              arc_path=arc_path, conda_env=conda_env, conda_sh=conda_sh)
        ctx.emit({"tool": "compose_arc_run", "args": {"server": server, "input_file": input_file},
                  "n_results": len(res["findings"]),
                  "results": [{"code": f.get("code"), "severity": f.get("severity")} for f in res["findings"]]})
        return json.dumps(res, indent=1)
