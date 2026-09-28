"""Dataclasses, loading and validation for `servers.yaml` (spec: docs/servers-spec.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SERVERS_FILE = "servers.yaml"
SCHEDULERS = ("slurm", "pbs", "pbspro", "torque", "sge", "htcondor", "local")
PBS_FAMILY = ("pbs", "pbspro", "torque")
ESS = ("orca", "gaussian", "qchem", "psi4", "molpro", "pyscf")
PARALLEL = ("mpi", "threads")
QUERY_KINDS = ("jobs", "job", "history", "partitions", "quota", "fairshare", "queue_access")
# kinds that may be overridden in servers.yaml `commands:` (queue_access runs several fixed commands)
COMMAND_KINDS = ("jobs", "job", "history", "partitions", "quota", "fairshare")

SERVER_KEYS = {
    "description", "scheduler", "host", "user", "ssh_alias", "modules_available", "arc",
    "partitions", "scratch", "storage", "software", "commands",
}
PARTITION_KEYS = {
    "max_walltime", "cores_per_node", "mem_per_node_gb", "gpus_per_node", "gpu_type",
    "max_nodes", "default", "notes", "access",
}
ACCESS_KEYS = {"users", "groups", "notes"}
SOFTWARE_KEYS = {"ess", "version", "executable", "env", "setup", "parallel", "partitions", "notes"}
STORAGE_KEYS = {"name", "path", "quota_gb", "backed_up", "quota_command", "notes"}
SCRATCH_KEYS = {"path", "node_local", "notes"}
ARC_KEYS = {"path", "max_simultaneous_jobs"}

_SECRET_KEY_RE = re.compile(r"pass(word|wd|phrase)?$|token|secret|api_?key|private_?key", re.I)
_SECRET_VALUE_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20,}")
_EXE_RE = re.compile(r"^/[A-Za-z0-9_./+@%,:=~-]+$")  # it is interpolated into submit scripts
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Unix user / group names (POSIX portable set; '$' allowed at the end for machine accounts)
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\$?$")


class ServersConfigError(ValueError):
    """servers.yaml is invalid; `.problems` lists every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("servers.yaml is invalid:\n  " + "\n  ".join(problems))


@dataclass
class Access:
    """Who may use a partition/queue: a user listed in `users` OR a member of any of `groups`."""

    users: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    notes: str | None = None

    def describe(self) -> str:
        bits = []
        if self.users:
            bits.append("users " + ", ".join(self.users))
        if self.groups:
            bits.append("groups " + ", ".join(self.groups))
        return " or ".join(bits) or "nobody"


@dataclass
class Partition:
    name: str
    max_walltime: str
    cores_per_node: int
    mem_per_node_gb: float
    gpus_per_node: int = 0
    gpu_type: str | None = None
    max_nodes: int = 1
    default: bool = False
    notes: str | None = None
    access: Access | None = None  # None = everyone in the group may use it

    @property
    def max_walltime_seconds(self) -> int:
        return parse_walltime(self.max_walltime)


@dataclass
class SoftwareInstall:
    key: str
    ess: str
    executable: str
    parallel: str
    version: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    partitions: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class Storage:
    name: str
    path: str
    quota_gb: float | None = None
    backed_up: bool | None = None
    quota_command: str | None = None
    notes: str | None = None


@dataclass
class Scratch:
    path: str | None = None
    node_local: bool = False
    notes: str | None = None


@dataclass
class Server:
    name: str
    scheduler: str
    host: str | None = None
    description: str | None = None
    user: str | None = None
    ssh_alias: str | None = None
    modules_available: bool = False
    arc: dict[str, Any] = field(default_factory=dict)
    partitions: dict[str, Partition] = field(default_factory=dict)
    scratch: Scratch = field(default_factory=Scratch)
    storage: list[Storage] = field(default_factory=list)
    software: dict[str, SoftwareInstall] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)

    @property
    def default_partition(self) -> Partition | None:
        for p in self.partitions.values():
            if p.default:
                return p
        return next(iter(self.partitions.values()), None)

    def partitions_for(self, software_key: str) -> list[Partition]:
        """Partitions a software install may run on (all of them if it lists none)."""
        sw = self.software.get(software_key)
        if sw and sw.partitions:
            return [self.partitions[p] for p in sw.partitions if p in self.partitions]
        return list(self.partitions.values())


# ----------------------------------------------------------------- walltime helpers

MAX_PLAUSIBLE_WALLTIME_H = 10000  # a numeric max_walltime above this is surely a YAML mis-parse
_WALL_RE = re.compile(r"^(?:(\d+)-)?(\d+):(\d{1,2})(?::(\d{1,2}))?$")


def parse_walltime(value: Any) -> int:
    """'72:00:00', '3-00:00:00', '1:30' (H:MM), or a number of hours -> seconds."""
    if isinstance(value, bool):
        raise ValueError(f"invalid walltime {value!r}")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError(f"invalid walltime {value!r}")
        return int(round(float(value) * 3600))
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_walltime(float(s))
    m = _WALL_RE.match(s)
    if not m:
        raise ValueError(f"invalid walltime {value!r} (use HH:MM:SS or D-HH:MM:SS)")
    days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"invalid walltime {value!r}")
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if total <= 0:
        raise ValueError(f"invalid walltime {value!r}")
    return total


def format_walltime(seconds: int) -> str:
    """Seconds -> 'HHH:MM:SS' (hours may exceed 24; accepted by Slurm, PBS and ARC)."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ----------------------------------------------------------------- validation

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scan_secrets(obj: Any, where: str, problems: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and v not in (None, "", False):
                problems.append(f"{where}.{k}: looks like a secret; never put passwords/tokens/keys in servers.yaml")
            _scan_secrets(v, f"{where}.{k}", problems)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_secrets(v, f"{where}[{i}]", problems)
    elif isinstance(obj, str) and _SECRET_VALUE_RE.search(obj):
        problems.append(f"{where}: contains what looks like a private key or token")


def _unknown(d: dict, allowed: set[str], where: str, problems: list[str]) -> None:
    for k in d:
        if k not in allowed:
            problems.append(f"{where}: unknown key {k!r} (allowed: {', '.join(sorted(allowed))})")


def _validate_access(acc: Any, where: str, problems: list[str]) -> None:
    if not isinstance(acc, dict):
        problems.append(f"{where}: must be a mapping with 'users' and/or 'groups' lists (omit it for no restriction)")
        return
    _unknown(acc, ACCESS_KEYS, where, problems)
    n = 0
    for key in ("users", "groups"):
        v = acc.get(key)
        if v is None:
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append(f"{where}.{key}: must be a list of Unix {key[:-1]} names")
            continue
        for x in v:
            if not ACCOUNT_NAME_RE.match(x):
                problems.append(f"{where}.{key}: {x!r} is not a valid Unix {key[:-1]} name "
                                "(letters, digits, _ . -)")
        n += len(v)
    if n == 0:
        problems.append(f"{where}: lists no users or groups; remove 'access' if everyone may use it")
    if acc.get("notes") is not None and not isinstance(acc["notes"], str):
        problems.append(f"{where}.notes: must be a string")


def validate_data(raw: Any, label: str = SERVERS_FILE) -> list[str]:
    """Every problem in a parsed servers.yaml, as '<label>: <where>: <message>' strings."""
    from .cluster import command_problems  # local import: cluster imports model

    problems: list[str] = []
    if raw is None:
        return [f"{label}: empty file"]
    if not isinstance(raw, dict) or not isinstance(raw.get("servers"), dict):
        return [f"{label}: top level must be a mapping with a 'servers:' mapping"]
    _scan_secrets(raw, "servers", problems)
    extra_top = set(raw) - {"servers"}
    if extra_top:
        problems.append(f"unknown top-level key(s) {sorted(extra_top)}; only 'servers' is allowed")
    for name, s in raw["servers"].items():
        w = f"servers.{name}"
        if not isinstance(name, str) or not _NAME_RE.match(name):
            problems.append(f"{w}: server name must be a short id (letters, digits, _ . -)")
        if not isinstance(s, dict):
            problems.append(f"{w}: must be a mapping")
            continue
        _unknown(s, SERVER_KEYS, w, problems)
        sched = s.get("scheduler")
        if sched not in SCHEDULERS:
            problems.append(f"{w}.scheduler: {sched!r} not in {list(SCHEDULERS)}")
        if sched != "local" and not s.get("host") and not s.get("ssh_alias"):
            problems.append(f"{w}: 'host' (login node) is required")
        for key in ("host", "user", "ssh_alias", "description"):
            if s.get(key) is not None and not isinstance(s.get(key), str):
                problems.append(f"{w}.{key}: must be a string")
        for key in ("host", "user", "ssh_alias"):
            v = s.get(key)
            if isinstance(v, str) and not re.fullmatch(r"[A-Za-z0-9_.@-]+", v):
                problems.append(f"{w}.{key}: {v!r} has characters that are not allowed")

        parts = s.get("partitions")
        if not isinstance(parts, dict) or not parts:
            problems.append(f"{w}.partitions: at least one partition/queue is required")
            parts = {}
        n_default = 0
        for pname, p in parts.items():
            pw = f"{w}.partitions.{pname}"
            if not isinstance(p, dict):
                problems.append(f"{pw}: must be a mapping")
                continue
            _unknown(p, PARTITION_KEYS, pw, problems)
            if "max_walltime" not in p:
                problems.append(f"{pw}: 'max_walltime' is required")
            else:
                mw = p["max_walltime"]
                try:
                    secs = parse_walltime(mw)
                except ValueError as e:
                    problems.append(f"{pw}.max_walltime: {e}")
                else:
                    if _is_num(mw) and secs > MAX_PLAUSIBLE_WALLTIME_H * 3600:
                        problems.append(
                            f"{pw}.max_walltime: {mw!r} would mean {mw} hours; a bare number is read as "
                            "hours (an unquoted 72:00:00 may have been parsed by YAML as the base-60 "
                            'number 259200). Quote it: max_walltime: "72:00:00"')
            if not (_is_int(p.get("cores_per_node")) and p["cores_per_node"] > 0):
                problems.append(f"{pw}: 'cores_per_node' must be a positive integer")
            if not (_is_num(p.get("mem_per_node_gb")) and p["mem_per_node_gb"] > 0):
                problems.append(f"{pw}: 'mem_per_node_gb' must be a positive number")
            if p.get("gpus_per_node") is not None and not (_is_int(p["gpus_per_node"]) and p["gpus_per_node"] >= 0):
                problems.append(f"{pw}: 'gpus_per_node' must be an integer >= 0")
            if p.get("max_nodes") is not None and not (_is_int(p["max_nodes"]) and p["max_nodes"] > 0):
                problems.append(f"{pw}: 'max_nodes' must be a positive integer")
            if p.get("default") is True:
                n_default += 1
            if "access" in p:
                _validate_access(p["access"], f"{pw}.access", problems)
        if n_default > 1:
            problems.append(f"{w}.partitions: at most one partition may have 'default: true' ({n_default} do)")

        scratch = s.get("scratch")
        if scratch is not None:
            if not isinstance(scratch, dict):
                problems.append(f"{w}.scratch: must be a mapping with 'path' and 'node_local'")
            else:
                _unknown(scratch, SCRATCH_KEYS, f"{w}.scratch", problems)
                sp = scratch.get("path")
                if sp is not None and not (isinstance(sp, str) and (sp.startswith("/") or sp.startswith("$"))):
                    problems.append(f"{w}.scratch.path: must be an absolute path (may start with $TMPDIR)")

        storage = s.get("storage") or []
        if not isinstance(storage, list):
            problems.append(f"{w}.storage: must be a list")
            storage = []
        for i, st in enumerate(storage):
            sw_ = f"{w}.storage[{i}]"
            if not isinstance(st, dict):
                problems.append(f"{sw_}: must be a mapping")
                continue
            _unknown(st, STORAGE_KEYS, sw_, problems)
            if not st.get("name") or not st.get("path"):
                problems.append(f"{sw_}: 'name' and 'path' are required")
            if st.get("quota_command"):
                for msg in command_problems(str(st["quota_command"])):
                    problems.append(f"{sw_}.quota_command: {msg}")

        arc = s.get("arc")
        if arc is not None:
            if not isinstance(arc, dict):
                problems.append(f"{w}.arc: must be a mapping")
            else:
                _unknown(arc, ARC_KEYS, f"{w}.arc", problems)
                if arc.get("max_simultaneous_jobs") is not None and not _is_int(arc["max_simultaneous_jobs"]):
                    problems.append(f"{w}.arc.max_simultaneous_jobs: must be an integer")

        cmds = s.get("commands") or {}
        if not isinstance(cmds, dict):
            problems.append(f"{w}.commands: must be a mapping")
            cmds = {}
        for k, v in cmds.items():
            if k not in COMMAND_KINDS:
                problems.append(f"{w}.commands.{k}: unknown query (allowed: {', '.join(COMMAND_KINDS)})")
                continue
            for msg in command_problems(str(v), allow_job_id=(k == "job")):
                problems.append(f"{w}.commands.{k}: {msg}")

        software = s.get("software") or {}
        if not isinstance(software, dict):
            problems.append(f"{w}.software: must be a mapping")
            software = {}
        for key, sw in software.items():
            kw = f"{w}.software.{key}"
            if not isinstance(sw, dict):
                problems.append(f"{kw}: must be a mapping")
                continue
            _unknown(sw, SOFTWARE_KEYS, kw, problems)
            ess = sw.get("ess")
            if ess not in ESS:
                problems.append(f"{kw}.ess: {ess!r} not in {list(ESS)}")
            elif not (key == ess or str(key).startswith(ess + "-")):
                problems.append(f"{kw}: key must be '<ess>' or '<ess>-<version>' (e.g. {ess}-{sw.get('version', 'X')})")
            exe = sw.get("executable")
            if not (isinstance(exe, str) and exe.startswith("/")):
                problems.append(f"{kw}.executable: must be an absolute path, got {exe!r}")
            elif not _EXE_RE.match(exe):
                problems.append(f"{kw}.executable: {exe!r} contains whitespace or shell metacharacters "
                                "(allowed: letters, digits, / _ . + - @ % , : = ~)")
            if sw.get("parallel") not in PARALLEL:
                problems.append(f"{kw}.parallel: {sw.get('parallel')!r} not in {list(PARALLEL)}")
            env = sw.get("env") or {}
            if not isinstance(env, dict) or not all(isinstance(v, (str, int, float)) for v in env.values()):
                problems.append(f"{kw}.env: must be a mapping of NAME: value")
            elif not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) for k in env):
                problems.append(f"{kw}.env: variable names must be valid shell identifiers")
            setup = sw.get("setup") or []
            if not isinstance(setup, list) or not all(isinstance(x, str) for x in setup):
                problems.append(f"{kw}.setup: must be a list of shell lines")
            sp = sw.get("partitions") or []
            if not isinstance(sp, list):
                problems.append(f"{kw}.partitions: must be a list")
                sp = []
            for pn in sp:
                if pn not in parts:
                    problems.append(f"{kw}.partitions: unknown partition {pn!r}")
    return [f"{label}: {p}" for p in problems]


# ----------------------------------------------------------------- parsing

def _parse_access(acc: Any) -> Access | None:
    if not isinstance(acc, dict):
        return None
    return Access(users=[str(x) for x in acc.get("users") or []],
                  groups=[str(x) for x in acc.get("groups") or []],
                  notes=acc.get("notes"))


def _parse_server(name: str, s: dict) -> Server:
    partitions = {
        str(pn): Partition(
            name=str(pn),
            max_walltime=str(p["max_walltime"]),
            cores_per_node=int(p["cores_per_node"]),
            mem_per_node_gb=float(p["mem_per_node_gb"]),
            gpus_per_node=int(p.get("gpus_per_node") or 0),
            gpu_type=p.get("gpu_type"),
            max_nodes=int(p.get("max_nodes") or 1),
            default=bool(p.get("default", False)),
            notes=p.get("notes"),
            access=_parse_access(p.get("access")),
        )
        for pn, p in (s.get("partitions") or {}).items()
    }
    software = {
        str(k): SoftwareInstall(
            key=str(k),
            ess=sw["ess"],
            executable=sw["executable"],
            parallel=sw["parallel"],
            version=None if sw.get("version") is None else str(sw["version"]),
            env={str(ek): str(ev) for ek, ev in (sw.get("env") or {}).items()},
            setup=list(sw.get("setup") or []),
            partitions=[str(x) for x in (sw.get("partitions") or [])],
            notes=sw.get("notes"),
        )
        for k, sw in (s.get("software") or {}).items()
    }
    storage = [
        Storage(
            name=str(st["name"]), path=str(st["path"]), quota_gb=st.get("quota_gb"),
            backed_up=st.get("backed_up"), quota_command=st.get("quota_command"), notes=st.get("notes"),
        )
        for st in (s.get("storage") or [])
    ]
    sc = s.get("scratch") or {}
    return Server(
        name=name,
        scheduler=s["scheduler"],
        host=s.get("host"),
        description=s.get("description"),
        user=s.get("user"),
        ssh_alias=s.get("ssh_alias"),
        modules_available=bool(s.get("modules_available", False)),
        arc=dict(s.get("arc") or {}),
        partitions=partitions,
        scratch=Scratch(path=sc.get("path"), node_local=bool(sc.get("node_local", False)), notes=sc.get("notes")),
        storage=storage,
        software=software,
        commands={str(k): str(v) for k, v in (s.get("commands") or {}).items()},
    )


def servers_path(cfg) -> Path:
    return Path(cfg.root) / SERVERS_FILE


class _ServersLoader(yaml.SafeLoader):
    """SafeLoader without YAML 1.1 base-60 numbers.

    PyYAML reads an unquoted `max_walltime: 72:00:00` as the sexagesimal int 259200 (and `1:30`
    as 90), which would then be taken as a number of hours. With this loader such values stay
    strings, so `72:00:00` means 72 hours whether or not it is quoted.
    """


_ServersLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers
         if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ServersLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"""^(?:[-+]?0b[0-1_]+
                    |[-+]?0[0-7_]+
                    |[-+]?(?:0|[1-9][0-9_]*)
                    |[-+]?0x[0-9a-fA-F_]+)$""", re.X),
    list("-+0123456789"))
_ServersLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
                    |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
                    |[-+]?\.(?:inf|Inf|INF)
                    |\.(?:nan|NaN|NAN))$""", re.X),
    list("-+0123456789."))


def load_yaml_text(text: str) -> Any:
    """Parse servers.yaml text (no base-60 numbers: `72:00:00` stays a string)."""
    return yaml.load(text, Loader=_ServersLoader)  # noqa: S506 - SafeLoader subclass


def read_raw(path: Path) -> Any:
    return load_yaml_text(Path(path).read_text())


def check_file(path: str | Path) -> list[str]:
    """Validation problems of one servers file ([] = valid). Used by lint and tests."""
    path = Path(path)
    try:
        raw = read_raw(path)
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML error: {e}"]
    return validate_data(raw, label=path.name)


def load_servers_file(path: str | Path) -> dict[str, Server]:
    path = Path(path)
    if not path.is_file():
        return {}
    problems = check_file(path)
    if problems:
        raise ServersConfigError(problems)
    raw = read_raw(path)
    return {str(n): _parse_server(str(n), s) for n, s in raw["servers"].items()}


def load_servers(cfg, path: str | Path | None = None) -> dict[str, Server]:
    """`servers.yaml` at the repo root -> {name: Server}; {} if the file does not exist.

    Raises ServersConfigError (with `.problems`) if the file is invalid.
    """
    return load_servers_file(Path(path) if path else servers_path(cfg))
