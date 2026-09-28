"""The batch job that runs ARC itself on a cluster (servers.yaml `arc.runner`).

The group runs ARC *on* the cluster: a job in the runner queue (pinned to the node ARC submits
from) activates ARC's conda env and runs `python $ARC_PATH/ARC.py <input.yml>`; ARC then submits
its ESS jobs from that node with the local qsub/sbatch. ARC.py takes the input file as its single
positional argument (`parse_command_line_arguments`: `file`, plus `-d/--debug`, `-q/--quiet`) and
uses the input file's directory as the project directory unless the input sets
`project_directory`, so the script runs from the submit directory.

servers.yaml holds only the group-level facts (queue, node, default cores/memory/walltime). Each
person's ARC clone and conda install are per user and resolve, per value, in this order:

1. explicit arguments (`rag-drg arc compose --arc-path/--conda-env/--conda-sh`, MCP arguments);
2. the user config file `$XDG_CONFIG_HOME/rag-drg/user.yaml` (default `~/.config/rag-drg/user.yaml`),
   keys `arc_path`, `conda_env`, `conda_sh`;
3. environment variables: `ARC_PATH` (then `arc_path`) for the clone - ARC's docs name no variable
   for it; ARC's own code calls the clone `ARC_PATH` (arc/common.py) - and `CONDA_EXE` (then
   `CONDA_PREFIX`) for conda.sh;
4. defaults: env `arc_env`; conda.sh found by the job itself (`conda info --base`, `$CONDA_EXE`,
   the usual ~/miniforge3-style installs); an unknown clone becomes `${ARC_PATH:?...}` so the job
   fails at once with a message instead of running the wrong ARC.

Steps 2 and 3 read this machine, so they are skipped when `RAG_DRG_SERVER_MODE` is set (the
shared server knows nobody's paths; the client passes them).
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from .access import local_identity_allowed, queue_access
from .model import (
    RUNNER_KEYS,
    ArcRunner,
    Server,
    _CONDA_ENV_RE,
    _safe_abs_path,
    _validate_runner,
    format_walltime,
    parse_walltime,
)
from .submit import SubmitError, _job_name, _job_vars, _mem_str, _p

_ARC_INPUT_RE = re.compile(r"^/?[A-Za-z0-9_][A-Za-z0-9_./+-]*$")
DEFAULT_RUNNER_JOB_NAME = "ARC"
DEFAULT_CONDA_ENV = "arc_env"
USER_KEYS = ("arc_path", "conda_env", "conda_sh")
ARC_PATH_ENV_VARS = ("ARC_PATH", "arc_path")
# conda installs the job looks for when neither conda.sh nor `conda` is known
_CONDA_GUESSES = ("$HOME/miniforge3", "$HOME/mambaforge", "$HOME/miniconda3", "$HOME/anaconda3")


def user_config_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "rag-drg" / "user.yaml"


@dataclass
class ArcUserEnv:
    """Per-user ARC paths; None = not known here (the script then resolves it at run time)."""

    arc_path: str | None = None
    conda_env: str = DEFAULT_CONDA_ENV
    conda_sh: str | None = None
    sources: dict[str, str] = field(default_factory=dict)  # key -> where the value came from
    notes: list[str] = field(default_factory=list)


def _valid(key: str, v) -> bool:
    if key == "conda_env":
        return isinstance(v, str) and bool(_CONDA_ENV_RE.match(v) or _safe_abs_path(v))
    return _safe_abs_path(v)


def _conda_sh_from_env(env) -> str | None:
    exe = env.get("CONDA_EXE")
    if exe:  # <base>/bin/conda (or <base>/condabin/conda)
        return str(PurePosixPath(exe).parent.parent / "etc" / "profile.d" / "conda.sh")
    prefix = env.get("CONDA_PREFIX")
    if prefix:  # <base> or <base>/envs/<name>
        return str(PurePosixPath(prefix.split("/envs/")[0]) / "etc" / "profile.d" / "conda.sh")
    return None


def resolve_arc_user_env(arc_path: str | None = None, conda_env: str | None = None, conda_sh: str | None = None,
                         *, use_local: bool | None = None, environ=None,
                         config_path: str | Path | None = None) -> ArcUserEnv:
    """Per-user ARC clone / conda env / conda.sh (see the module docstring for the order).

    `use_local=None` reads the user config file and environment unless RAG_DRG_SERVER_MODE is set.
    Raises SubmitError for an invalid explicit or config-file value; invalid environment values
    are skipped with a note.
    """
    if use_local is None:
        use_local = local_identity_allowed()
    env = os.environ if environ is None else environ
    explicit = {"arc_path": arc_path, "conda_env": conda_env, "conda_sh": conda_sh}
    out = ArcUserEnv()
    problems: list[dict] = []
    values: dict[str, str | None] = {}
    for k, v in explicit.items():
        if v is not None and v != "":
            if not _valid(k, v):
                problems.append(_p("error", f"{k} {v!r}: must be an absolute path without whitespace or shell "
                                            "metacharacters" + (" (or a conda env name)" if k == "conda_env" else "")))
            values[k], out.sources[k] = v, "given"
    if use_local:
        cfg_file = Path(config_path) if config_path else user_config_path(env)
        conf: dict = {}
        if cfg_file.is_file():
            try:
                conf = yaml.safe_load(cfg_file.read_text()) or {}
            except (OSError, yaml.YAMLError) as e:
                problems.append(_p("error", f"{cfg_file}: cannot read ({e})"))
            if not isinstance(conf, dict):
                problems.append(_p("error", f"{cfg_file}: must be a mapping with {', '.join(USER_KEYS)}"))
                conf = {}
        for k in USER_KEYS:
            if k in values or conf.get(k) in (None, ""):
                continue
            if not _valid(k, conf[k]):
                problems.append(_p("error", f"{cfg_file}: {k} {conf[k]!r} must be an absolute path without "
                                            "whitespace or shell metacharacters"))
                continue
            values[k], out.sources[k] = str(conf[k]), str(cfg_file)
        if "arc_path" not in values:
            for var in ARC_PATH_ENV_VARS:
                v = env.get(var)
                if v:
                    if _valid("arc_path", v.rstrip("/") or v):
                        values["arc_path"], out.sources["arc_path"] = v.rstrip("/"), f"${var}"
                        break
                    out.notes.append(f"ignored ${var}={v!r} (not a plain absolute path)")
        if "conda_sh" not in values:
            v = _conda_sh_from_env(env)
            if v and _valid("conda_sh", v):
                values["conda_sh"] = v
                out.sources["conda_sh"] = "$CONDA_EXE" if env.get("CONDA_EXE") else "$CONDA_PREFIX"
    if problems:
        raise SubmitError(problems)
    out.arc_path = values.get("arc_path")
    out.conda_sh = values.get("conda_sh")
    if values.get("conda_env"):
        out.conda_env = values["conda_env"]
    else:
        out.sources["conda_env"] = "default"
    return out


def _raw_partitions(server: Server) -> dict:
    return {p.name: {"max_walltime": p.max_walltime, "cores_per_node": p.cores_per_node,
                     "mem_per_node_gb": p.mem_per_node_gb} for p in server.partitions.values()}


def effective_runner(server: Server, **overrides) -> ArcRunner:
    """server.arc_runner with `overrides` applied (validated like servers.yaml). Raises SubmitError."""
    unknown = sorted(set(overrides) - RUNNER_KEYS)
    if unknown:
        raise SubmitError([_p("error", f"unknown runner override(s) {unknown}; allowed: {', '.join(sorted(RUNNER_KEYS))}")])
    overrides = {k: v for k, v in overrides.items() if v is not None}
    base = server.arc_runner
    if base is None:
        raw = dict(overrides)
    else:
        raw = {k: v for k, v in dataclasses.asdict(base).items() if v not in (None, [])}
        raw.update(overrides)
    problems: list[str] = []
    if base is None and not overrides:
        problems.append(f"{server.name} has no `arc.runner` block in servers.yaml (see docs/arc-run.md)")
    else:
        _validate_runner(raw, server.scheduler, _raw_partitions(server), f"{server.name}.arc.runner", problems)
    if problems:
        raise SubmitError([_p("error", m) for m in problems])
    return ArcRunner(
        queue=str(raw["queue"]), host=raw.get("host"), host_cores=raw.get("host_cores"),
        host_mem_gb=raw.get("host_mem_gb"), cores=int(raw.get("cores", 1)),
        mem_gb=float(raw.get("mem_gb", 8)),
        walltime=None if raw.get("walltime") is None else str(raw["walltime"]),
        extra_setup=list(raw.get("extra_setup") or []),
        notes=raw.get("notes"),
    )


def _runner_header(scheduler: str, name: str, r: ArcRunner, wall: str) -> list[str]:
    if scheduler == "slurm":
        lines = [f"#SBATCH --job-name={name}", f"#SBATCH --partition={r.queue}", "#SBATCH --nodes=1",
                 "#SBATCH --ntasks=1", f"#SBATCH --cpus-per-task={r.cores}",
                 f"#SBATCH --mem={_mem_str(r.mem_gb, 'G', 'M')}", f"#SBATCH --time={wall}"]
        if r.host:
            lines.append(f"#SBATCH --nodelist={r.host}")
        return lines + ["#SBATCH --output=%x-%j.out", "#SBATCH --error=%x-%j.err"]
    if scheduler in ("pbs", "pbspro"):
        # PBS Pro / OpenPBS: `host` is a built-in host-level resource inside a select chunk
        sel = f"select=1:ncpus={r.cores}:mem={_mem_str(r.mem_gb, 'gb', 'mb')}" + (f":host={r.host}" if r.host else "")
        lines = [f"#PBS -N {name}", f"#PBS -q {r.queue}", f"#PBS -l {sel}", f"#PBS -l walltime={wall}"]
    else:  # torque: a node name in place of the node count pins the job
        lines = [f"#PBS -N {name}", f"#PBS -q {r.queue}", f"#PBS -l nodes={r.host or 1}:ppn={r.cores}",
                 f"#PBS -l mem={_mem_str(r.mem_gb, 'gb', 'mb')}", f"#PBS -l walltime={wall}"]
    return lines + [f"#PBS -o {name}.out", f"#PBS -e {name}.err"]


def _env_lines(u: ArcUserEnv, submit: str) -> list[str]:
    """ARC_PATH + conda activation. No `#PBS -V`: the job must not depend on the shell it was
    submitted from (an activated env, a different PATH), so everything is resolved here."""
    lines = ["# --- ARC's Python environment (per user; see docs/arc-run.md) ---"]
    if u.arc_path:
        lines.append(f"ARC_PATH={shlex.quote(u.arc_path)}")
    else:
        lines.append(f'ARC_PATH="${{ARC_PATH:?set ARC_PATH to your ARC clone: export it in ~/.bashrc, '
                     f'or {submit} -v ARC_PATH ..., or rerun rag-drg arc compose --arc-path DIR}}"')
    if u.conda_sh:
        lines.append(f"CONDA_SH={shlex.quote(u.conda_sh)}")
    else:
        guesses = " ".join(f'"{g}"' for g in _CONDA_GUESSES)
        lines += ['CONDA_SH=""',
                  "if command -v conda >/dev/null 2>&1; then",
                  '    CONDA_SH="$(conda info --base)/etc/profile.d/conda.sh"',
                  'elif [ -n "${CONDA_EXE:-}" ]; then',
                  '    CONDA_SH="$(dirname "$(dirname "$CONDA_EXE")")/etc/profile.d/conda.sh"',
                  "else",
                  f"    for base in {guesses}; do",
                  '        if [ -f "$base/etc/profile.d/conda.sh" ]; then CONDA_SH="$base/etc/profile.d/conda.sh"; break; fi',
                  "    done",
                  "fi"]
    lines += ['if [ ! -f "$CONDA_SH" ]; then',
              '    echo "conda.sh not found (${CONDA_SH:-no conda on PATH}); rerun rag-drg arc compose --conda-sh PATH" >&2',
              "    exit 1",
              "fi",
              'source "$CONDA_SH"',
              f"conda activate {shlex.quote(u.conda_env)} || {{ echo {shlex.quote('conda activate ' + u.conda_env + ' failed')} >&2; exit 1; }}"]
    return lines


def render_arc_runner_script(server: Server, input_file: str = "input.yml", job_name: str | None = None, *,
                             user: str | None = None, groups: list[str] | None = None,
                             arc_path: str | None = None, conda_env: str | None = None,
                             conda_sh: str | None = None, use_local: bool | None = None,
                             user_env: ArcUserEnv | None = None, **overrides) -> tuple[str, list[str]]:
    """A batch script that runs ARC.py on `server` (its servers.yaml `arc.runner` block), plus notes.

    `overrides` replace the group-level runner fields (queue, host, cores, mem_gb, walltime,
    extra_setup). `arc_path`/`conda_env`/`conda_sh` are the per-user values (resolved with
    resolve_arc_user_env; `use_local` controls reading the user config file and environment).
    `user`/`groups` are checked against the queue's `access:` rule. Raises SubmitError (with
    `.problems`) when the runner is missing/invalid or violates a limit.
    """
    r = effective_runner(server, **overrides)
    u = user_env or resolve_arc_user_env(arc_path, conda_env, conda_sh, use_local=use_local)
    if not input_file or not _ARC_INPUT_RE.match(input_file) or ".." in PurePosixPath(input_file).parts:
        raise SubmitError([_p("error", f"input_file {input_file!r} must be a path of letters, digits, _ . / + -")])
    part = server.partitions[r.queue]
    wall_s = parse_walltime(r.walltime) if r.walltime else part.max_walltime_seconds
    wall = format_walltime(wall_s)
    # cores/memory/walltime were checked against the (pinned) node and the queue in effective_runner;
    # here only who may use the queue
    problems = []
    if part.access is not None:
        acc = queue_access(server, part, user, groups)
        if acc["allowed"] is False:
            raise SubmitError([_p("error", acc["reason"])])
        if acc["allowed"] is None:
            problems.append(_p("info", acc["reason"]))
    name = _job_name(job_name or DEFAULT_RUNNER_JOB_NAME, server.scheduler)
    workdir, jobid = _job_vars(server.scheduler)
    submit = "sbatch" if server.scheduler == "slurm" else "qsub"
    where = f"{server.name}:{r.queue}" + (f" (node {r.host})" if r.host else "")
    lines = ["#!/bin/bash",
             f"# ARC runner on {where}; generated by `rag-drg arc compose` from servers.yaml.",
             "# ARC runs here and submits its ESS jobs itself (ARC's 'local' server).",
             *_runner_header(server.scheduler, name, r, wall),
             "",
             f"WORKDIR={workdir}",
             f"JOBID={jobid}",
             'cd "$WORKDIR" || exit 1',
             "",
             *_env_lines(u, submit)]
    lines += list(r.extra_setup)
    lines += [f"export OMP_NUM_THREADS={r.cores}   # in-core work (conformers, xTB, ...) uses the runner's cores",
              'ARC_PY="$ARC_PATH/ARC.py"',
              'if [ ! -f "$ARC_PY" ]; then echo "no ARC.py in $ARC_PATH" >&2; exit 1; fi',
              f"ARC_INPUT={shlex.quote(input_file)}",
              "",
              'echo "ARC runner $JOBID on $(hostname) started $(date): $ARC_PY $ARC_INPUT"',
              "",
              "# on exit, also after a walltime kill or qdel/scancel (SIGTERM): stop ARC and say how to resume.",
              "# ESS jobs ARC already submitted keep running; a restart picks them up from restart.yml.",
              "cleanup() {",
              "    rc=$?",
              '    if [ -n "${ARC_PID:-}" ]; then kill -TERM "$ARC_PID" 2>/dev/null; fi',
              '    echo "ARC runner $JOBID ended $(date) with status $rc"',
              '    [ "$rc" -eq 0 ] || echo "resume: point ARC_INPUT at <project directory>/restart.yml and resubmit"',
              "}",
              "trap cleanup EXIT",
              "trap 'exit 143' TERM INT",
              "",
              'python "$ARC_PY" "$ARC_INPUT" &',
              "ARC_PID=$!",
              'wait "$ARC_PID"',
              "rc=$?",
              "ARC_PID=",
              "exit $rc"]
    script = "\n".join(lines).rstrip() + "\n"
    notes = [f"runner: {where}, {r.cores} core(s), {r.mem_gb:g} GB, walltime {wall}"
             + ("" if r.walltime else " (the queue's maximum)"),
             f"submit from the directory that holds {input_file}: {submit} submit.sh "
             "(ARC's project directory defaults to the input file's directory)"]
    src = u.sources
    notes.append("ARC clone: " + (f"{u.arc_path} (from {src.get('arc_path')})" if u.arc_path else
                                  "unknown here - the job uses $ARC_PATH from its environment and stops if unset; "
                                  "pass arc_path / --arc-path"))
    notes.append(f"conda env: {u.conda_env} (from {src.get('conda_env', 'default')}); conda.sh: "
                 + (f"{u.conda_sh} (from {src.get('conda_sh')})" if u.conda_sh else
                    "found by the job (conda on PATH, $CONDA_EXE, ~/miniforge3 ...); pass conda_sh / --conda-sh to fix it"))
    notes += u.notes
    for p in problems:
        notes.append(f"{p['severity']}: {p['message']}")
    return script, notes
