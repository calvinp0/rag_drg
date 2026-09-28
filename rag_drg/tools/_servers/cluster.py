"""Read-only live cluster queries (scheduler state, quotas), strictly allowlisted.

Commands run with the SSH identity of whoever runs the rag-drg process:
`ssh -o BatchMode=yes -o ConnectTimeout=10 <ssh_alias | user@host | host> -- <command>`,
or directly (no shell) when the cluster is this machine. Only the programs/sub-commands in
ALLOWED_PROGRAMS may run, every argument must be made of a small safe character set (so the
remote shell cannot be abused), and a job id must match JOB_ID_RE.
"""

from __future__ import annotations

import getpass
import re
import shlex
import socket
import subprocess
from typing import Callable

from .model import PBS_FAMILY, QUERY_KINDS, Server

TIMEOUT_S = 30
MAX_OUTPUT = 8000
JOB_ID_RE = re.compile(r"^\d+(\.\w+)?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_@%:=,./+\- ]*$")
_JOB_PLACEHOLDER = "{job_id}"

SLURM_DEFAULTS: dict[str, list[str]] = {
    "jobs": ['squeue -u $USER -o "%.18i %.12P %.30j %.8T %.10M %.10l %.6D %.4C %R"'],
    "job": ["scontrol show job {job_id}"],
    "history": ["sacct -u $USER -S now-7days -X "
                "--format=JobID,JobName%30,Partition,State,Elapsed,Timelimit,AllocCPUS,ReqMem,MaxRSS,ExitCode"],
    "partitions": ["sinfo -s", 'sinfo -o "%P %a %l %D %c %m %G"'],
    "fairshare": ["sshare -u $USER"],
}
PBS_DEFAULTS: dict[str, list[str]] = {
    "jobs": ["qstat -u $USER"],
    "job": ["qstat -f {job_id}"],
    "history": ["qstat -x -u $USER"],  # PBS Pro / OpenPBS only (Torque has no job history in qstat)
    "partitions": ["qstat -Q", "pbsnodes -aSj"],
}


def _check_scontrol(args: list[str]) -> str | None:
    return None if args[:1] == ["show"] else "only 'scontrol show ...' is allowed"


def _check_pbsnodes(args: list[str]) -> str | None:
    for a in args:
        if a.startswith("-") and not re.fullmatch(r"-[aSjlLvF]+", a):
            return f"pbsnodes option {a!r} is not allowed (read-only options: -a -S -j -l -L -v -F)"
    return None


def _check_lfs(args: list[str]) -> str | None:
    return None if args[:1] == ["quota"] else "only 'lfs quota ...' is allowed"


def _check_beegfs(args: list[str]) -> str | None:
    return None if args[:1] == ["--getquota"] else "only 'beegfs-ctl --getquota ...' is allowed"


# program -> extra argument check (None = any safe arguments)
ALLOWED_PROGRAMS: dict[str, Callable[[list[str]], str | None] | None] = {
    "squeue": None,
    "sacct": None,
    "sinfo": None,
    "sshare": None,
    "scontrol": _check_scontrol,
    "qstat": None,
    "pbsnodes": _check_pbsnodes,
    "quota": None,
    "df": None,
    "lfs": _check_lfs,
    "mmlsquota": None,
    "beegfs-ctl": _check_beegfs,
}


def command_problems(command: str, allow_job_id: bool = False) -> list[str]:
    """Why a configured command may not run ([] = allowed)."""
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return [f"cannot parse {command!r}: {e}"]
    if not tokens:
        return ["empty command"]
    prog = tokens[0]
    if prog not in ALLOWED_PROGRAMS:
        return [f"program {prog!r} is not in the read-only allowlist ({', '.join(sorted(ALLOWED_PROGRAMS))})"]
    problems = []
    for t in tokens:
        t2 = t.replace("$USER", "").replace(_JOB_PLACEHOLDER, "") if allow_job_id else t.replace("$USER", "")
        if not _TOKEN_RE.match(t2):
            problems.append(f"argument {t!r} contains characters that are not allowed "
                            "(only letters, digits, spaces and _@%:=,./+- ; $USER is substituted)")
    if not allow_job_id and _JOB_PLACEHOLDER in command:
        problems.append("{job_id} is only allowed in the 'job' command")
    check = ALLOWED_PROGRAMS[prog]
    if check and not problems:
        msg = check(tokens[1:])
        if msg:
            problems.append(msg)
    return problems


def default_commands(server: Server, what: str) -> list[str]:
    if server.scheduler == "slurm":
        cmds = list(SLURM_DEFAULTS.get(what, []))
    elif server.scheduler in PBS_FAMILY:
        cmds = list(PBS_DEFAULTS.get(what, []))
        if what == "history" and server.scheduler == "torque":
            cmds = []
    else:
        cmds = []
    if what == "quota":
        cmds = [st.quota_command for st in server.storage if st.quota_command]
    return cmds


def commands_for(server: Server, what: str) -> list[str]:
    """Override from servers.yaml `commands:` if set, else the scheduler default(s)."""
    if what in server.commands:
        return [server.commands[what]]
    return default_commands(server, what)


def is_local(server: Server) -> bool:
    if server.scheduler == "local":
        return True
    if server.ssh_alias:
        return False
    host = (server.host or "").lower()
    names = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    return host in names


def ssh_target(server: Server) -> str:
    if server.ssh_alias:
        return server.ssh_alias
    return f"{server.user}@{server.host}" if server.user else str(server.host)


def _remote_quote(token: str) -> str:
    """shlex.quote, but leave $USER to be expanded by the remote shell."""
    parts = token.split("$USER")
    return '"$USER"'.join(shlex.quote(p) if p else "" for p in parts)


def build_argv(server: Server, command: str, job_id: str | None = None) -> list[str]:
    tokens = shlex.split(command)
    if job_id is not None:
        tokens = [t.replace(_JOB_PLACEHOLDER, job_id) for t in tokens]
    if is_local(server):
        user = getpass.getuser()
        return [t.replace("$USER", user) for t in tokens]
    remote = " ".join(_remote_quote(t) for t in tokens)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target(server), "--", remote]


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def cluster_query(server: Server, what: str, job_id: str | None = None,
                  runner: Callable | None = None) -> str:
    """Run the allowlisted read-only command(s) for `what` and return their (truncated) output.

    what: jobs | job (needs job_id) | history | partitions | quota | fairshare.
    Raises ValueError for anything not allowed.
    """
    if what not in QUERY_KINDS:
        raise ValueError(f"'what' must be one of {', '.join(QUERY_KINDS)}")
    if what == "job":
        if job_id is None or not JOB_ID_RE.match(str(job_id)):
            raise ValueError("job_id must look like 12345 or 12345.server (digits, optional .suffix)")
        job_id = str(job_id)
    elif job_id is not None:
        raise ValueError("job_id is only used with what='job'")
    cmds = commands_for(server, what)
    if not cmds:
        raise ValueError(f"no '{what}' command known for {server.name} ({server.scheduler}); "
                         f"add one under servers.{server.name}.commands.{what} in servers.yaml")
    runner = runner or subprocess.run  # looked up at call time so tests can patch subprocess.run
    outputs: list[str] = []
    for cmd in cmds:
        problems = command_problems(cmd, allow_job_id=(what == "job"))
        if problems:
            raise ValueError(f"refusing to run {cmd!r}: " + "; ".join(problems))
        argv = build_argv(server, cmd, job_id if what == "job" else None)
        shown = cmd.replace(_JOB_PLACEHOLDER, job_id or "")
        try:
            res = runner(argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            outputs.append(f"$ {shown}\n[timed out after {TIMEOUT_S} s]")
            continue
        except FileNotFoundError as e:
            outputs.append(f"$ {shown}\n[cannot run: {e}]")
            continue
        text = (res.stdout or "") + (("\n[stderr]\n" + res.stderr) if res.stderr else "")
        outputs.append(f"$ {shown}\n{text.rstrip()}\n[exit {res.returncode}]")
    header = f"{server.name} ({'local' if is_local(server) else 'via ssh ' + ssh_target(server)}): {what}"
    return _truncate(header + "\n\n" + "\n\n".join(outputs))


def cluster_commands_enabled(cfg) -> bool:
    section = (getattr(cfg, "extra", None) or {}).get("cluster_commands") or {}
    return bool(section.get("enabled", False)) if isinstance(section, dict) else False
