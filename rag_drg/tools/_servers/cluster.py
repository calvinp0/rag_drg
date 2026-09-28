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

from .model import COMMAND_KINDS, PBS_FAMILY, QUERY_KINDS, Server

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


def _check_id(args: list[str]) -> str | None:
    if not args or any(not re.fullmatch(r"-[unGgr]+", a) for a in args):
        return "only 'id -un' / 'id -Gn' style options are allowed (no user argument)"
    return None


def _check_sacctmgr(args: list[str]) -> str | None:
    if args[:1] not in (["show"], ["list"]) or any(a in ("-i", "--immediate") for a in args):
        return "only 'sacctmgr show|list ...' is allowed"
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
    "sacctmgr": _check_sacctmgr,
    "id": _check_id,
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


# queue_access: fixed command sets (not overridable in servers.yaml `commands:`)
QUEUE_ACCESS_COMMANDS: dict[str, list[str]] = {
    "slurm": ["id -un", "id -Gn", "scontrol show partition",
              "sacctmgr show assoc user=$USER format=Account,Partition -P -n"],
    "pbs": ["id -un", "id -Gn", "qstat -Qf"],
}


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
    if what == "queue_access":
        fam = "pbs" if server.scheduler in ("pbs", "pbspro") else server.scheduler
        cmds = list(QUEUE_ACCESS_COMMANDS.get(fam, []))
    return cmds


def commands_for(server: Server, what: str) -> list[str]:
    """Override from servers.yaml `commands:` if set, else the scheduler default(s)."""
    if what in server.commands and what in COMMAND_KINDS:
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


def run_command(server: Server, cmd: str, job_id: str | None = None, runner: Callable | None = None,
                allow_job_id: bool = False) -> tuple[int | None, str, str]:
    """Run one allowlisted command; (returncode or None if it could not run, stdout, stderr/reason)."""
    problems = command_problems(cmd, allow_job_id=allow_job_id)
    if problems:
        raise ValueError(f"refusing to run {cmd!r}: " + "; ".join(problems))
    runner = runner or subprocess.run  # looked up at call time so tests can patch subprocess.run
    argv = build_argv(server, cmd, job_id)
    try:
        res = runner(argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {TIMEOUT_S} s"
    except FileNotFoundError as e:
        return None, "", f"cannot run: {e}"
    return res.returncode, res.stdout or "", res.stderr or ""


def where_label(server: Server) -> str:
    return "local" if is_local(server) else "via ssh " + ssh_target(server)


def cluster_query(server: Server, what: str, job_id: str | None = None,
                  runner: Callable | None = None) -> str:
    """Run the allowlisted read-only command(s) for `what` and return their (truncated) output.

    what: jobs | job (needs job_id) | history | partitions | quota | fairshare | queue_access.
    queue_access returns a per-queue "may I use it" report (see live_access.py).
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
    if what == "queue_access":
        from .live_access import live_queue_access, format_live_report

        return _truncate(format_live_report(server, live_queue_access(server, runner=runner)))
    cmds = commands_for(server, what)
    if not cmds:
        raise ValueError(f"no '{what}' command known for {server.name} ({server.scheduler}); "
                         f"add one under servers.{server.name}.commands.{what} in servers.yaml")
    for cmd in cmds:  # validate everything before running anything
        problems = command_problems(cmd, allow_job_id=(what == "job"))
        if problems:
            raise ValueError(f"refusing to run {cmd!r}: " + "; ".join(problems))
    outputs: list[str] = []
    for cmd in cmds:
        shown = cmd.replace(_JOB_PLACEHOLDER, job_id or "")
        rc, out, err = run_command(server, cmd, job_id if what == "job" else None, runner=runner,
                                   allow_job_id=(what == "job"))
        if rc is None:
            outputs.append(f"$ {shown}\n[{err}]")
            continue
        text = out + (("\n[stderr]\n" + err) if err else "")
        outputs.append(f"$ {shown}\n{text.rstrip()}\n[exit {rc}]")
    header = f"{server.name} ({where_label(server)}): {what}"
    return _truncate(header + "\n\n" + "\n\n".join(outputs))


def cluster_commands_enabled(cfg) -> bool:
    section = (getattr(cfg, "extra", None) or {}).get("cluster_commands") or {}
    return bool(section.get("enabled", False)) if isinstance(section, dict) else False
