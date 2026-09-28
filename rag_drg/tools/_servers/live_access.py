"""Live queue-access report (`cluster_query(server, "queue_access")`) and the PBS discovery draft.

PBS (Pro/OpenPBS): `id -un`, `id -Gn` and `qstat -Qf` are run as the user; each queue's
`acl_user_enable/acl_users`, `acl_group_enable/acl_groups`, `enabled`, `started` and
`resources_max.*` / `max_run` / `max_user_run` decide "usable" and give the limits. Every
enabled ACL must admit the user (PBS checks each one), and PBS compares `acl_groups` with the
job's group, so membership of a secondary group may need `#PBS -W group_list=<group>`.

Slurm: `id -Gn`, `scontrol show partition` (State, AllowGroups, AllowAccounts, DenyAccounts,
MaxTime, ...) and `sacctmgr show assoc user=$USER` (the user's accounts).

`discover_pbs()` turns saved `qstat -Qf` (and optionally `pbsnodes -a` / `pbsnodes -aSj`) output
into a DRAFT `partitions:` block for servers.yaml.
"""

from __future__ import annotations

import math
import re
from typing import Callable

from .model import ACCOUNT_NAME_RE, PBS_FAMILY, Server, format_walltime

# ----------------------------------------------------------------- generic helpers

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(b|w)?\s*$", re.I)
_UNIT_POW = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def pbs_size_gb(value: str | None) -> float | None:
    """PBS size ('256gb', '263842692kb', '4tb', '1048576' bytes) -> GiB; None if unparsable."""
    if not value:
        return None
    m = _SIZE_RE.match(str(value))
    if not m:
        return None
    num, unit, word = float(m.group(1)), m.group(2).lower(), (m.group(3) or "b").lower()
    nbytes = num * 1024 ** _UNIT_POW[unit] * (8 if word == "w" else 1)
    return nbytes / 1024 ** 3


def pbs_walltime_s(value: str | None) -> int | None:
    """PBS duration ('72:00:00', '1:30:00', '259200' seconds) -> seconds."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return int(v)
    parts = v.split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 4:
        return None
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.insert(0, 0)
    if len(nums) == 4:
        d, h, m, s = nums
        return ((d * 24 + h) * 60 + m) * 60 + s
    h, m, s = nums
    return (h * 60 + m) * 60 + s


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("true", "t", "1", "yes", "y")


# ----------------------------------------------------------------- PBS parsing

def parse_qstat_qf(text: str) -> dict[str, dict[str, str]]:
    """`qstat -Qf` output -> {queue: {attribute: value}}.

    Blocks start with `Queue: <name>`; attributes are `    key = value`; a value that PBS wraps
    continues on lines that start with a tab (joined without a separator, as PBS splits anywhere).
    """
    queues: dict[str, dict[str, str]] = {}
    cur: dict[str, str] | None = None
    last: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        m = re.match(r"^Queue:\s*(\S+)\s*$", line)
        if m:
            cur = queues.setdefault(m.group(1), {})
            last = None
            continue
        if cur is None or not line.strip():
            continue
        if line.startswith("\t") and last is not None:
            cur[last] += line.strip()
            continue
        m = re.match(r"^\s+([A-Za-z0-9_.]+)\s*=\s*(.*)$", line)
        if m:
            last = m.group(1)
            cur[last] = m.group(2).strip()
        elif last is not None and line.startswith((" ", "\t")):
            cur[last] += line.strip()
    return queues


def _acl_list(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _acl_admits(entries: list[str], names: set[str]) -> bool | None:
    """PBS ACL: entries are name[@host], optionally prefixed '+' (allow) or '-' (deny); first match wins."""
    for e in entries:
        sign = "-" if e.startswith("-") else "+"
        name = e.lstrip("+-").split("@", 1)[0]
        if name == "*" or name in names:
            return sign == "+"
    return False


def pbs_queue_report(name: str, attrs: dict[str, str], user: str | None, groups: list[str] | None) -> dict:
    reasons: list[str] = []
    usable: bool | None = True
    qtype = attrs.get("queue_type", "")
    if not _truthy(attrs.get("enabled", "True")):
        usable = False
        reasons.append("queue is disabled (enabled = False)")
    if not _truthy(attrs.get("started", "True")):
        usable = False
        reasons.append("queue is stopped (started = False)")
    if _truthy(attrs.get("acl_user_enable")):
        users = _acl_list(attrs.get("acl_users"))
        if user is None:
            usable = None if usable is not False else False
            reasons.append(f"user ACL ({', '.join(users) or 'empty'}) but your user name is unknown")
        elif not _acl_admits(users, {user}):
            usable = False
            reasons.append(f"user {user} is not in acl_users ({', '.join(users) or 'empty'})")
        else:
            reasons.append(f"user {user} is in acl_users")
    if _truthy(attrs.get("acl_group_enable")):
        acl_groups = _acl_list(attrs.get("acl_groups"))
        if groups is None:
            usable = None if usable is not False else False
            reasons.append(f"group ACL ({', '.join(acl_groups) or 'empty'}) but your groups are unknown")
        else:
            # PBS checks the ACL against the JOB's group (one group), not the set of all of the
            # user's groups: the queue is usable if ANY single group of the user is admitted
            # (a `-badgrp` entry does not stop a job submitted with group_list=danagrp).
            admitted = [g for g in groups if _acl_admits(acl_groups, {g})]
            if not admitted:
                usable = False
                reasons.append(f"none of your groups is admitted by acl_groups ({', '.join(acl_groups) or 'empty'})")
            else:
                # `id -Gn` lists the primary (effective) group first
                primary = groups[0] if groups else None
                hit = primary if primary in admitted else admitted[0]
                if hit == primary:
                    reasons.append(f"your primary group {hit} is admitted by acl_groups")
                else:
                    reasons.append(f"group {hit} is admitted by acl_groups but is not your primary group"
                                   f"{f' ({primary})' if primary else ''}: submit with `#PBS -W group_list={hit}`")
    if not reasons:
        reasons.append("no user/group ACL")
    limits = {k: attrs[a] for k, a in (
        ("max_walltime", "resources_max.walltime"), ("ncpus", "resources_max.ncpus"),
        ("mem", "resources_max.mem"), ("ngpus", "resources_max.ngpus"), ("nodect", "resources_max.nodect"),
        ("max_run", "max_run"), ("max_user_run", "max_user_run"), ("max_queued", "max_queued"),
        ("default_walltime", "resources_default.walltime"),
    ) if attrs.get(a)}
    return {"queue": name, "type": qtype, "usable": usable, "why": "; ".join(reasons), "limits": limits}


# ----------------------------------------------------------------- Slurm parsing

def parse_scontrol_partitions(text: str) -> dict[str, dict[str, str]]:
    """`scontrol show partition` (multi-line or -o) -> {partition: {Key: value}}."""
    parts: dict[str, dict[str, str]] = {}
    cur: dict[str, str] | None = None
    for tok in re.findall(r"(\w+)=(\S*)", text):
        key, val = tok
        if key == "PartitionName":
            cur = parts.setdefault(val, {})
            continue
        if cur is not None:
            cur[key] = val
    return parts


def _slurm_list(v: str | None) -> list[str] | None:
    """'ALL' / '(null)' / '' -> None (no restriction), else the names."""
    if v is None or v.upper() in ("ALL", "(NULL)", ""):
        return None
    return [x for x in v.split(",") if x]


def slurm_partition_report(name: str, attrs: dict[str, str], groups: list[str] | None,
                           accounts: list[str] | None) -> dict:
    reasons: list[str] = []
    usable: bool | None = True
    state = attrs.get("State", "UP")
    if state.upper() != "UP":
        usable = False
        reasons.append(f"State={state}")
    allow_g = _slurm_list(attrs.get("AllowGroups"))
    if allow_g is not None:
        if groups is None:
            usable = None if usable is not False else False
            reasons.append(f"AllowGroups={','.join(allow_g)} but your groups are unknown")
        elif not set(allow_g) & set(groups):
            usable = False
            reasons.append(f"none of your groups is in AllowGroups={','.join(allow_g)}")
        else:
            reasons.append(f"group {sorted(set(allow_g) & set(groups))[0]} is in AllowGroups")
    allow_a = _slurm_list(attrs.get("AllowAccounts"))
    deny_a = _slurm_list(attrs.get("DenyAccounts")) or []
    if allow_a is not None or deny_a:
        if accounts is None:
            usable = None if usable is not False else False
            reasons.append("account restrictions but your Slurm accounts are unknown")
        else:
            ok = [a for a in accounts if (allow_a is None or a in allow_a) and a not in deny_a]
            if not ok:
                usable = False
                reasons.append(f"none of your accounts ({', '.join(accounts) or 'none'}) is allowed "
                               f"(AllowAccounts={','.join(allow_a or ['ALL'])}, DenyAccounts={','.join(deny_a) or '-'})")
            else:
                reasons.append(f"submit with account {ok[0]} (-A {ok[0]})")
    if not reasons:
        reasons.append("no group/account restriction")
    limits = {k: attrs[a] for k, a in (
        ("max_walltime", "MaxTime"), ("max_nodes", "MaxNodes"), ("max_cpus_per_node", "MaxCPUsPerNode"),
        ("max_mem_per_node", "MaxMemPerNode"), ("default_walltime", "DefaultTime"), ("total_cpus", "TotalCPUs"),
    ) if attrs.get(a) not in (None, "", "UNLIMITED")}
    return {"queue": name, "type": "partition", "usable": usable, "why": "; ".join(reasons), "limits": limits}


def parse_sacctmgr_accounts(text: str) -> list[str]:
    """`sacctmgr show assoc ... format=Account,Partition -P -n` -> account names."""
    out = []
    for line in text.splitlines():
        acct = line.split("|", 1)[0].strip()
        if acct and acct not in out:
            out.append(acct)
    return out


# ----------------------------------------------------------------- live query

def live_queue_access(server: Server, runner: Callable | None = None) -> dict:
    """Run the fixed read-only commands as the user and return
    {"user", "groups", "supported": bool, "queues": [report...], "errors": [str]}."""
    from .cluster import commands_for, run_command

    res: dict = {"user": None, "groups": None, "supported": True, "queues": [], "errors": []}
    fam = "pbs" if server.scheduler in ("pbs", "pbspro") else server.scheduler
    if fam not in ("pbs", "slurm"):
        res["supported"] = False
        res["errors"].append(f"live queue access is implemented for Slurm and PBS Pro/OpenPBS, not "
                             f"{server.scheduler}" + (" (Torque's qstat -Qf has a different format)"
                                                      if server.scheduler in PBS_FAMILY else ""))
        return res
    outputs: dict[str, str | None] = {}
    for cmd in commands_for(server, "queue_access"):
        rc, out, err = run_command(server, cmd, runner=runner)
        if rc == 0:
            outputs[cmd] = out
        else:
            outputs[cmd] = None
            res["errors"].append(f"`{cmd}` failed: " + (err.strip() or f"exit {rc}")[:300])
    if outputs.get("id -un"):
        res["user"] = outputs["id -un"].strip() or None
    if outputs.get("id -Gn") is not None:
        res["groups"] = (outputs["id -Gn"] or "").split()
    if fam == "pbs":
        text = outputs.get("qstat -Qf")
        if text is not None:
            for q, attrs in parse_qstat_qf(text).items():
                res["queues"].append(pbs_queue_report(q, attrs, res["user"], res["groups"]))
    else:
        text = outputs.get("scontrol show partition")
        acct_out = next((v for k, v in outputs.items() if k.startswith("sacctmgr")), None)
        accounts = parse_sacctmgr_accounts(acct_out) if acct_out is not None else None
        res["accounts"] = accounts
        if text is not None:
            for p, attrs in parse_scontrol_partitions(text).items():
                res["queues"].append(slurm_partition_report(p, attrs, res["groups"], accounts))
    return res


def _duration_s(value: str | None) -> int | None:
    if not value:
        return None
    if "-" in value:
        from .model import parse_walltime

        try:
            return parse_walltime(value)
        except ValueError:
            return None
    return pbs_walltime_s(value)


def _mismatch(server: Server, q: dict) -> str | None:
    """How the live queue differs from servers.yaml (None = no difference noticed)."""
    part = server.partitions.get(q["queue"])
    if part is None:
        return None if q.get("type", "").lower() == "route" else "not in servers.yaml"
    wall = q["limits"].get("max_walltime")
    secs = _duration_s(wall)
    if secs and secs != part.max_walltime_seconds:
        return f"max_walltime {part.max_walltime} but the scheduler says {wall}"
    return None


def format_live_report(server: Server, res: dict) -> str:
    from .cluster import where_label

    lines = [f"{server.name} ({where_label(server)}): queue access for user {res.get('user') or '?'}"
             f" (groups: {', '.join(res['groups']) if res.get('groups') is not None else '?'})"]
    if res.get("accounts") is not None:
        lines.append(f"Slurm accounts: {', '.join(res['accounts']) or '(none)'}")
    for e in res["errors"]:
        lines.append(f"! {e}")
    if not res["supported"]:
        return "\n".join(lines)
    lines.append("")
    word = {True: "yes", False: "NO", None: "unknown"}
    for q in res["queues"]:
        lim = ", ".join(f"{k}={v}" for k, v in q["limits"].items()) or "no limits reported"
        typ = f" [{q['type']}]" if q.get("type") and q["type"].lower() not in ("execution", "partition") else ""
        lines.append(f"{q['queue']}{typ}: usable={word[q['usable']]} - {q['why']}")
        lines.append(f"    limits: {lim}")
        mm = _mismatch(server, q)
        if mm:
            lines.append(f"    servers.yaml: {mm}")
    if not res["queues"]:
        lines.append("(no queues parsed)")
    return "\n".join(lines)


# ----------------------------------------------------------------- PBS discovery draft

def parse_pbsnodes(text: str) -> dict[str, dict]:
    """`pbsnodes -a` (long) or `pbsnodes -aSj` (table) -> {node: {ncpus, mem_gb, ngpus, queues}}."""
    nodes: dict[str, dict] = {}
    if re.search(r"^\s*vnode\s+state", text, re.M):  # -aSj / -aS table
        header_seen = False
        for line in text.splitlines():
            if line.startswith("---"):
                header_seen = True
                continue
            if not header_seen or not line.strip():
                continue
            cols = line.split()
            fracs = [c for c in cols if re.fullmatch(r"[^/\s]+/[^/\s]+", c)]
            if len(fracs) < 2:
                continue
            # columns: mem f/t, ncpus f/t, nmics f/t, ngpus f/t  (the total is after the slash)
            tot = [f.split("/")[1] for f in fracs]
            node = {"mem_gb": pbs_size_gb(tot[0]), "ncpus": _int(tot[1]),
                    "ngpus": _int(tot[3]) if len(tot) > 3 else 0, "queues": []}
            nodes[cols[0]] = node
        return nodes
    cur = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = nodes.setdefault(line.strip(), {"mem_gb": None, "ncpus": None, "ngpus": 0, "queues": []})
            continue
        m = re.match(r"^\s+([A-Za-z0-9_.]+)\s*=\s*(.*)$", line)
        if not (m and cur is not None):
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "resources_available.ncpus":
            cur["ncpus"] = _int(v)
        elif k == "resources_available.mem":
            cur["mem_gb"] = pbs_size_gb(v)
        elif k == "resources_available.ngpus":
            cur["ngpus"] = _int(v) or 0
        elif k in ("queue", "resources_available.Qlist", "resources_available.qlist"):
            cur["queues"] += [x.strip() for x in v.split(",") if x.strip()]
    return nodes


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _draft_acl(attrs: dict[str, str], enable_key: str, list_key: str) -> tuple[list[str], list[str]]:
    """Allowed names of an enabled PBS ACL for the draft, and the entries left out.

    Deny (`-`) entries are dropped (servers.yaml only lists who is allowed), and so are wildcard
    entries such as `*` (they admit everyone, i.e. no restriction) and anything that is not a
    valid Unix name (it would fail servers.yaml validation).
    """
    if not _truthy(attrs.get(enable_key)):
        return [], []
    names: list[str] = []
    left_out: list[str] = []
    for e in _acl_list(attrs.get(list_key)):
        if e.startswith("-"):
            continue
        name = e.lstrip("+").split("@")[0]
        if "*" in name or not ACCOUNT_NAME_RE.match(name):
            left_out.append(e)
        elif name not in names:
            names.append(name)
    return names, left_out


def discover_pbs(qstat_qf: str, pbsnodes: str | None = None) -> str:
    """A DRAFT servers.yaml `partitions:` block from saved `qstat -Qf` [+ pbsnodes] output."""
    queues = parse_qstat_qf(qstat_qf)
    nodes = parse_pbsnodes(pbsnodes) if pbsnodes else {}
    out = ["# DRAFT generated by `rag-drg servers discover-pbs` from qstat -Qf"
           + (" and pbsnodes" if pbsnodes else "") + " output.",
           "# REVIEW EVERY VALUE before pasting under servers.<name>: in servers.yaml, then run",
           "# `rag-drg servers validate`. Values marked TODO were not in the output; per-node values",
           "# are the MINIMUM over the nodes serving the queue (all nodes if the mapping is unknown).",
           "# Set `default: true` on exactly one queue. `access:` mirrors the queue ACLs; note that",
           "# servers.yaml admits users OR groups, while PBS requires every enabled ACL to pass.",
           "partitions:"]
    skipped = []
    for name, a in queues.items():
        if a.get("queue_type", "").lower().startswith("route"):
            skipped.append(f"{name} (route queue -> {a.get('route_destinations', '?')})")
            continue
        served = [n for n in nodes.values() if name in n["queues"]]
        pool = served or list(nodes.values())
        src = f"pbsnodes, {len(pool)} node(s){'' if served else ', queue mapping unknown'}"
        out.append(f"  {name}:")
        if not (_truthy(a.get("enabled", "True")) and _truthy(a.get("started", "True"))):
            out.append("    # NOTE: queue was disabled or stopped when discovered")
        wall = pbs_walltime_s(a.get("resources_max.walltime"))
        out.append(f'    max_walltime: "{format_walltime(wall)}"   # resources_max.walltime' if wall
                   else '    max_walltime: null   # TODO: no resources_max.walltime (unlimited?) - fill in')
        cores = _int(a.get("resources_max.ncpus"))
        node_cores = [n["ncpus"] for n in pool if n.get("ncpus")]
        if node_cores:
            c = min(node_cores)
            note = f"   # {src}" + (f" (queue resources_max.ncpus = {cores})" if cores else "")
            out.append(f"    cores_per_node: {c}{note}")
        elif cores:
            out.append(f"    cores_per_node: {cores}   # resources_max.ncpus (per JOB; check the node size)")
        else:
            out.append("    cores_per_node: null   # TODO")
        mem_q = pbs_size_gb(a.get("resources_max.mem"))
        node_mem = [n["mem_gb"] for n in pool if n.get("mem_gb")]
        if node_mem:
            out.append(f"    mem_per_node_gb: {math.floor(min(node_mem))}   # {src}"
                       + (f" (queue resources_max.mem = {a['resources_max.mem']})" if mem_q else ""))
        elif mem_q:
            out.append(f"    mem_per_node_gb: {math.floor(mem_q)}   # resources_max.mem (per JOB; check the node size)")
        else:
            out.append("    mem_per_node_gb: null   # TODO")
        gpus_q = _int(a.get("resources_max.ngpus"))
        node_gpus = [n.get("ngpus") or 0 for n in served]
        if node_gpus:
            out.append(f"    gpus_per_node: {min(node_gpus)}   # {src}")
        elif gpus_q:
            out.append(f"    gpus_per_node: {gpus_q}   # resources_max.ngpus")
        else:
            out.append("    gpus_per_node: 0")
        acl_users, bad_u = _draft_acl(a, "acl_user_enable", "acl_users")
        acl_groups, bad_g = _draft_acl(a, "acl_group_enable", "acl_groups")
        if bad_u or bad_g:
            out.append("    # ACL entries left out (wildcards mean everyone; others are not plain names): "
                       + ", ".join(bad_u + bad_g))
        if acl_users or acl_groups:
            out.append("    access:")
            if acl_users:
                out.append(f"      users: [{', '.join(acl_users)}]")
            if acl_groups:
                out.append(f"      groups: [{', '.join(acl_groups)}]")
            both = " PBS requires BOTH the user and the group ACL." if acl_users and acl_groups else ""
            out.append(f'      notes: "from qstat -Qf acl_users/acl_groups.{both} Ask the cluster admins to be added."')
        extra = [f"{k}={a[k]}" for k in ("max_run", "max_user_run", "max_queued") if a.get(k)]
        if extra:
            out.append(f'    notes: "{"; ".join(extra)}"')
    if skipped:
        out.append("# skipped: " + ", ".join(skipped))
    return "\n".join(out) + "\n"
