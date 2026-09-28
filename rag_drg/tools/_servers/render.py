"""Generated cluster cards (Markdown) and ARC `servers` settings from servers.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .access import queue_access
from .model import Server, format_walltime
from .submit import (SUBMIT_SCHEDULERS, SubmitError, _is_gpu_build, render_arc_template, render_submit_files,
                     render_submit_script)

GENERATED_SUBDIR = Path("hpc") / "servers" / "generated"

# servers.yaml scheduler -> ARC `cluster_soft` (ARC lower-cases it and accepts
# slurm / pbs / oge / sge / htcondor / local; see arc/job/ssh.py and arc/job/local.py).
ARC_CLUSTER_SOFT = {
    "slurm": "Slurm",
    "pbs": "PBS",
    "pbspro": "PBS",
    "torque": "PBS",
    "sge": "OGE",
    "htcondor": "HTCondor",
    "local": "local",
}
# ESS that ARC can submit to a server (arc/settings/settings.py `supported_ess`); psi4 is not one.
ARC_ESS = ("gaussian", "molpro", "orca", "qchem")

_EXAMPLE_INPUT = {"orca": "job.inp", "gaussian": "job.gjf", "qchem": "job.in", "molpro": "job.in",
                  "psi4": "job.in", "pyscf": "job.py"}
_SUBMIT = {"slurm": "sbatch", "local": "bash"}


def generated_dir(cfg) -> Path:
    curated = next((s.path for s in cfg.sources if s.name == "curated" and s.type == "local" and s.path), None)
    return Path(curated or Path(cfg.root) / "knowledge") / GENERATED_SUBDIR


def _fmt(v) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def access_text(p) -> str:
    """'everyone' or 'users a, b; groups g' for a partition."""
    if p.access is None:
        return "everyone"
    bits = []
    if p.access.users:
        bits.append("users " + ", ".join(p.access.users))
    if p.access.groups:
        bits.append("groups " + ", ".join(p.access.groups))
    return "; ".join(bits)


def _code(v) -> str:
    return f"`{v}`" if v not in (None, "") else "-"


# ----------------------------------------------------------------- ARC settings

def arc_queue_split(server: Server) -> tuple[list, dict[str, str]]:
    """(partitions for ARC's `queues`, {excluded partition: why}).

    Restricted (`access:`) and GPU partitions are left out: ARC's queue troubleshooting moves a
    failed job to the next queue in the dict, and it never requests GPUs. If that would leave no
    queue at all, every partition is kept.
    """
    ordered = sorted(server.partitions.values(), key=lambda p: not p.default)
    excluded: dict[str, str] = {}
    for p in ordered:
        why = []
        if p.access is not None:
            why.append("restricted (access: " + access_text(p) + ")")
        if p.gpus_per_node:
            why.append("GPU partition (ARC does not request GPUs)")
        if why:
            excluded[p.name] = "; ".join(why)
    kept = [p for p in ordered if p.name not in excluded]
    if not kept:
        return ordered, {}
    return kept, excluded


def arc_ess_queues(server: Server, user: str | None = None,
                   groups: list[str] | None = None) -> tuple[list, dict[str, str]]:
    """(partitions ARC may submit ESS jobs to, first = ARC's default queue; {left-out partition: why}).

    servers.yaml `arc.ess_queues` in its order when given, else arc_queue_split() (the
    non-restricted, non-GPU partitions, default first). With a known identity (`user`/`groups`),
    queues that identity may not use are left out too.
    """
    names = server.arc.get("ess_queues") or []
    if names:
        kept = [server.partitions[q] for q in names if q in server.partitions]
        excluded = {p: "not in arc.ess_queues" for p in server.partitions if p not in names}
    else:
        kept, excluded = arc_queue_split(server)
        excluded = dict(excluded)
    if user is not None or groups is not None:
        usable = []
        for p in kept:
            acc = queue_access(server, p, user, groups)
            if acc["allowed"] is False:
                excluded[p.name] = "no access for " + (user or "you")
            else:
                usable.append(p)
        kept = usable
    return kept, excluded


def _node_limits(entry: dict, p) -> None:
    entry["cpus"] = p.cores_per_node
    mem = p.mem_per_node_gb
    entry["memory"] = int(mem) if float(mem).is_integer() else mem


def arc_local_entry(server: Server, user: str | None = None, groups: list[str] | None = None) -> dict:
    """ARC `servers['local']` for a cluster ARC itself runs on (servers.yaml `arc.runner`).

    `queues` = arc_ess_queues() with their max walltimes (ARC submits every job to the first one;
    see docs/arc-run.md). cpus/memory are the first queue's node: ARC gives a job
    min(8, cpus) cores and caps its memory at 95% of `memory`. `excluded_queues` keeps ARC's PBS
    queue discovery (`qstat -q`, arc/job/trsh.py) off the partitions left out.
    """
    queues, excluded = arc_ess_queues(server, user, groups)
    entry: dict = {"cluster_soft": ARC_CLUSTER_SOFT[server.scheduler]}
    if queues:
        _node_limits(entry, queues[0])
    # servers.yaml arc.cpus / arc.memory_gb: the group's own caps (e.g. 16 cores / 160 GB per job)
    if server.arc.get("cpus") is not None:
        entry["cpus"] = int(server.arc["cpus"])
    if server.arc.get("memory_gb") is not None:
        mem = server.arc["memory_gb"]
        entry["memory"] = int(mem) if float(mem).is_integer() else mem
    if queues:
        entry["queues"] = {p.name: format_walltime(p.max_walltime_seconds) for p in queues}
    if excluded:
        entry["excluded_queues"] = list(excluded)
    if server.arc.get("max_simultaneous_jobs") is not None:
        entry["max_simultaneous_jobs"] = int(server.arc["max_simultaneous_jobs"])
    return entry


def _local_server_name(servers: dict[str, Server], names: list[str], local) -> str | None:
    """Which of `names` is ARC's 'local' server: `local` (a name, None/False = none) or, with
    "auto", the one server with an `arc.runner` block."""
    if local == "auto":
        runners = [n for n in names if servers[n].arc_runner is not None]
        if len(runners) > 1:
            raise KeyError(f"several servers have an arc.runner block ({', '.join(runners)}); ARC runs on one "
                           "of them - pick it with --local NAME")
        return runners[0] if runners else None
    if not local:
        return None
    if local not in names:
        raise KeyError(f"--local {local!r} is not one of the selected servers ({', '.join(names)})")
    return local


def arc_server_entry(server: Server) -> dict:
    part = server.default_partition
    entry: dict = {"cluster_soft": ARC_CLUSTER_SOFT[server.scheduler]}
    if server.scheduler != "local" and server.host:
        entry["address"] = server.host
    if server.arc.get("path"):
        entry["path"] = server.arc["path"]
    if part is not None:
        entry["cpus"] = part.cores_per_node
        mem = part.mem_per_node_gb
        entry["memory"] = int(mem) if float(mem).is_integer() else mem
    # ARC takes the first queue as the default one.
    ordered, _ = arc_ess_queues(server)
    if ordered:
        entry["queues"] = {p.name: format_walltime(p.max_walltime_seconds) for p in ordered}
    if server.arc.get("max_simultaneous_jobs") is not None:
        entry["max_simultaneous_jobs"] = int(server.arc["max_simultaneous_jobs"])
    return entry


def arc_settings(servers: dict[str, Server], names: list[str] | None = None, local="auto", *,
                 user: str | None = None, groups: list[str] | None = None) -> str:
    """Python source for ~/.arc/settings.py: a `servers` dict and a suggested `global_ess_settings`,
    followed by the `submit_scripts` dict for ~/.arc/submit.py.

    `local`: the server ARC itself runs on, emitted as ARC's `'local'` server ("auto" = the one
    selected server with an `arc.runner` block; None = every server is remote, reached over SSH).
    `user`/`groups` (the person ARC runs as) drop the ESS queues they may not use.
    """
    settings_src, submit_src = arc_settings_parts(servers, names, local, user=user, groups=groups)
    return settings_src + "\n" + submit_src


def _local_comment(server: Server) -> list[str]:
    r = server.arc_runner
    where = f"queue {r.queue}" + (f" on node {r.host}" if r.host else "") if r else "this cluster"
    return [
        f"    # {server.name} is ARC's 'local' server: ARC.py itself runs on {server.name}",
        f"    # (a runner job in {where}; `rag-drg arc compose`), so ARC submits its ESS",
        "    # jobs with the local qsub/sbatch instead of over SSH (arc/job/local.py), runs them in the",
        "    # project directory, and needs no address/key. For that server ARC reads servers['local']",
        "    # and submit_scripts['local'].",
    ]


def arc_settings_parts(servers: dict[str, Server], names: list[str] | None = None,
                       local="auto", *, user: str | None = None,
                       groups: list[str] | None = None) -> tuple[str, str]:
    """(~/.arc/settings.py snippet, ~/.arc/submit.py snippet); see arc_settings()."""
    names = names or list(servers)
    unknown = [n for n in names if n not in servers]
    if unknown:
        raise KeyError(f"unknown server(s) {unknown}; known: {', '.join(servers) or '(none)'}")
    local_name = _local_server_name(servers, names, local)
    remote = [n for n in names if n != local_name]
    lines = ["# Generated by `rag-drg servers arc-settings` from servers.yaml. Paste into ~/.arc/settings.py."]
    if remote:
        lines += ["# Add your own 'un' (username) and 'key' (path to your SSH private key) to each remote server;",
                  "# ARC does not read ~/.ssh/config. Never commit these."]
    lines.append("servers = {")
    if local_name is not None:
        s = servers[local_name]
        lines += _local_comment(s)
        lines.append("    'local': {")
        queues, excluded = arc_ess_queues(s, user, groups)
        for k, v in arc_local_entry(s, user, groups).items():
            lines.append(f"        {k!r}: {v!r},")
        for p in queues:
            if p.access is not None:
                lines.append(f"        # queue {p.name!r} is restricted ({access_text(p)}); drop it if you are not one of them")
        for pname, why in excluded.items():
            lines.append(f"        # queue {pname!r} excluded: {why}")
        lines.append("        'un': __import__('getpass').getuser(),  # ARC runs as you on the cluster")
        lines.append("    },")
    for n in remote:
        s = servers[n]
        lines.append(f"    {n!r}: {{")
        for k, v in arc_server_entry(s).items():
            lines.append(f"        {k!r}: {v!r},")
        _, excluded = arc_ess_queues(s)
        for pname, why in excluded.items():
            lines.append(f"        # queue {pname!r} left out of 'queues': {why}")
        lines.append("        # 'un': '<your username>',")
        lines.append("        # 'key': '/home/<you>/.ssh/id_ed25519',")
        if "path" not in s.arc:
            lines.append("        # 'path': '<remote base path, e.g. /home>',  (set arc.path in servers.yaml)")
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# Suggested; a list is a priority order. 'local' must also exist in `servers` for in-core ESS.")
    lines.append("global_ess_settings = {")
    arc_name = {n: ("local" if n == local_name else n) for n in names}
    for ess in ARC_ESS:
        having = [arc_name[n] for n in names if any(sw.ess == ess for sw in servers[n].software.values())]
        if having:
            val = having[0] if len(having) == 1 else having
            lines.append(f"    {ess!r}: {val!r},")
    if any(sw.ess == "pyscf" for n in names for sw in servers[n].software.values()):
        lines.append("    'pyscf': 'local',  # ARC runs PySCF in-core on the machine ARC runs on")
    lines.append("}")
    if any(sw.ess == "psi4" for n in names for sw in servers[n].software.values()):
        lines.append("# psi4 is installed but is not in ARC's supported_ess, so it has no entry here.")
    if local_name is not None:
        djs = servers[local_name].arc.get("default_job_settings")
        if djs:
            lines += ["", "default_job_settings = {"] + [f"    {k!r}: {v!r}," for k, v in djs.items()] + ["}"]
        lines += _local_command_note(servers[local_name])
    return "\n".join(lines) + "\n", "\n".join(arc_submit_scripts(servers, names, local_name)) + "\n"


# ARC's defaults (arc/settings/settings.py) call the scheduler by absolute path.
_ARC_LOCAL_COMMANDS = {
    "PBS": ("/usr/local/bin/qsub", "/usr/local/bin/qstat -u $USER", "/usr/local/bin/qdel"),
    "Slurm": ("/usr/bin/sbatch", "/usr/bin/squeue -u $USER", "/usr/bin/scancel"),
}


def _local_command_note(server: Server) -> list[str]:
    soft = ARC_CLUSTER_SOFT[server.scheduler]
    given = server.arc.get("commands") or {}
    if given:
        # servers.yaml arc.commands: write ARC's dicts (a top-level name replaces ARC's whole dict)
        out = ["", "# Scheduler commands on this cluster (servers.yaml arc.commands)."]
        for key, name in (("submit", "submit_command"), ("status", "check_status_command"),
                          ("delete", "delete_command")):
            if key in given:
                out.append(f"{name} = {{{soft!r}: {given[key]!r}}}")
        return out
    cmds = _ARC_LOCAL_COMMANDS.get(soft)
    if not cmds:
        return []
    sub, stat, dele = cmds
    tool = "qsub" if soft == "PBS" else "sbatch"
    return [
        "",
        f"# ARC submits with submit_command[{soft!r}] = {sub!r} (check_status_command {stat!r},",
        f"# delete_command {dele!r}). If `command -v {tool}` on the runner node prints another path, override",
        "# them here (a top-level name in ~/.arc/settings.py replaces ARC's whole dict), e.g.:",
        f"# submit_command = {{{soft!r}: '/path/to/{tool}'}}",
    ]


def _version_key(sw) -> tuple:
    # the key's version (gaussian-16 > gaussian-09) first, then the full version string
    return (tuple(int(x) for x in re.findall(r"\d+", sw.key)),
            tuple(int(x) for x in re.findall(r"\d+", sw.version or "")))


def arc_ess_install(server: Server, ess: str):
    """The install ARC should use for `ess` on `server`: servers.yaml `arc.ess_installs[ess]` if set,
    else a non-GPU build, newest version first."""
    chosen = (server.arc.get("ess_installs") or {}).get(ess)
    if chosen and chosen in server.software:
        return server.software[chosen]
    cands = [sw for sw in server.software.values() if sw.ess == ess and not _is_gpu_build(sw)]
    return max(cands, key=_version_key) if cands else None


def arc_submit_scripts(servers: dict[str, Server], names: list[str], local_name: str | None = None) -> list[str]:
    """Lines of a `submit_scripts` dict for ~/.arc/submit.py (one template per ARC-supported ESS)."""
    lines = [
        "# " + "=" * 94,
        "# REQUIRED: ~/.arc/submit.py must also have a `submit_scripts[<server>][<ess>]` entry for every",
        "# server and ESS above. ARC looks up submit_scripts[server][job_adapter] when it writes a job",
        "# (arc/job/adapter.py) and raises KeyError for a server that has none. Paste the dict below",
        "# into ~/.arc/submit.py (merge it with an existing `submit_scripts`). ARC fills {name}, {un},",
        "# {queue}, {t_max}, {memory} (MiB; per CPU on Slurm, total on PBS) and {cpus} with str.format,",
        "# so literal braces are doubled ({{...}}). Review the scripts before the first run.",
        "# " + "=" * 94,
        "submit_scripts = {",
    ]
    for n in names:
        s = servers[n]
        lines.append(f"    {('local' if n == local_name else n)!r}: {{" + (f"  # {n}" if n == local_name else ""))
        for ess in ARC_ESS:
            sw = arc_ess_install(s, ess)
            if sw is None:
                continue
            others = [o.key for o in s.software.values() if o.ess == ess and o.key != sw.key]
            try:
                tmpl = render_arc_template(s, sw.key)
            except SubmitError as e:
                lines.append(f"        # {ess}: no template ({e})")
                continue
            if '"""' in tmpl or "\\" in tmpl:
                lines.append(f"        # {ess}: no template (the script contains triple quotes or backslashes)")
                continue
            note = f"  # {sw.key}" + (f"; other installs: {', '.join(others)}" if others else "")
            lines.append(f'        {ess!r}: """{tmpl}""",{note}')
        lines.append("    },")
    lines.append("}")
    return lines


# ----------------------------------------------------------------- cards

def _example_request(server: Server, key: str) -> dict:
    parts = server.partitions_for(key)
    part = server.default_partition if server.default_partition in parts else (parts[0] if parts else None)
    gpus = 0
    if part is not None and part.gpus_per_node and (key.endswith("-gpu") or "gpu" in key.split("-")):
        gpus = 1
    return {"partition": part.name if part else None, "gpus": gpus}


def render_card(server: Server) -> str:
    s = server
    tags = ["cluster", "server", s.name, s.scheduler, "partitions", "queues", "install paths",
            "absolute paths", "scratch", "quota", "submit script", *s.software.keys()]
    meta = {
        "title": f"{s.name} cluster card (generated from servers.yaml)",
        "domain": "hpc",
        "software": s.scheduler,
        "doc_type": "reference",  # a rendering of servers.yaml: rank below hand-written cards
        "status": "draft",
        "generated": True,
        "tags": list(dict.fromkeys(tags)),
    }
    out = ["---", yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip(), "---",
           f"# {s.name} cluster card", "",
           "Generated by `rag-drg servers render-cards` from `servers.yaml`. Do not edit this file: "
           "change `servers.yaml` and regenerate. The MCP tools `server_info`, `render_submit_script` "
           "and `check_resources` use the same data.", ""]
    if s.description:
        out += [s.description.strip(), ""]

    out += ["## Access", ""]
    out.append(f"* Scheduler: {s.scheduler}")
    if s.host:
        out.append(f"* Login host: `{s.host}`" + (f" (ssh alias `{s.ssh_alias}`)" if s.ssh_alias else ""))
    out.append(f"* User: {'`' + s.user + '`' if s.user else 'your own account ($USER)'}; "
               "log in with SSH keys or an agent (no passwords)")
    out.append(f"* Environment modules: {'available' if s.modules_available else 'not used'}; "
               "programs are called by the absolute paths below")
    if s.arc_runner is None:
        out.append("* ARC `servers` entry for `~/.arc/settings.py` (add your `un` and `key`):")
    else:
        r = s.arc_runner
        out.append(f"* ARC runs on this cluster: a runner job in queue `{r.queue}`"
                   + (f" on node `{r.host}`" if r.host else "")
                   + " runs your own `$ARC_PATH/ARC.py` in your conda env, and ARC submits the ESS jobs from "
                   f"there (`rag-drg arc compose input.yml --server {s.name}` writes submit.sh and the "
                   "settings; see docs/arc-run.md). ARC's `servers` entry is `'local'`:")
    out += ["", "```python", arc_settings({s.name: s}).split("\n\n# Suggested")[0].strip(), "```", ""]

    out += ["## Partitions / queues", "",
            "| Name | Max walltime | Cores/node | Mem/node (GB) | GPUs/node | Max nodes | Default | Access | Notes |",
            "|---|---|---|---|---|---|---|---|---|"]
    for p in s.partitions.values():
        gpu = f"{p.gpus_per_node} x {p.gpu_type}" if p.gpus_per_node and p.gpu_type else _fmt(p.gpus_per_node or 0)
        out.append(f"| `{p.name}` | `{p.max_walltime}` | {p.cores_per_node} | {_fmt(p.mem_per_node_gb)} | {gpu} | "
                   f"{p.max_nodes} | {_fmt(p.default)} | {access_text(p)} | {p.notes or ''} |")
    out.append("")
    restricted = [p for p in s.partitions.values() if p.access is not None]
    if restricted:
        out += ["Restricted queues (servers.yaml `access:`): only the listed users, or members of ANY listed "
                "group, may submit there. Check yours with `rag-drg servers access "
                f"{s.name}` (add `--live` to ask the scheduler itself).", ""]
        for p in restricted:
            if p.access.notes:
                out.append(f"* `{p.name}`: {p.access.notes.strip()}")
        if any(p.access.notes for p in restricted):
            out.append("")

    out += ["## Installed software", "",
            "| Key | ESS | Version | Executable | Parallel | Partitions |", "|---|---|---|---|---|---|"]
    for sw in s.software.values():
        out.append(f"| `{sw.key}` | {sw.ess} | {_fmt(sw.version)} | `{sw.executable}` | {sw.parallel} | "
                   f"{', '.join(sw.partitions) or 'any'} |")
    out.append("")
    for sw in s.software.values():
        out += [f"### {sw.key} environment", ""]
        setup = [f'export {k}="{v}"' for k, v in sw.env.items()] + list(sw.setup)
        if setup:
            out += ["```bash", *setup, "```"]
        else:
            out.append("No environment setup needed; call the executable by its absolute path.")
        if sw.notes:
            out += ["", sw.notes.strip()]
        out.append("")

    out += ["## Storage, scratch and quotas", "",
            "| Area | Path | Quota (GB) | Backed up | Check usage with |", "|---|---|---|---|---|"]
    for st in s.storage:
        out.append(f"| {st.name} | `{st.path}` | {_fmt(st.quota_gb)} | {_fmt(st.backed_up)} | {_code(st.quota_command)} |")
    sc = s.scratch
    out.append(f"| scratch{' (node-local)' if sc.node_local else ''} | {_code(sc.path)} | - | no | - |")
    out.append("")
    if sc.notes:
        out += [sc.notes.strip(), ""]

    if s.scheduler not in SUBMIT_SCHEDULERS:
        out += ["## How to submit", "",
                f"rag-drg cannot generate {s.scheduler} submit files yet (`render_submit_script` refuses). "
                f"Follow the hand-written card for {s.name} and the group's own templates.", ""]
        return "\n".join(out).rstrip() + "\n"
    out += ["## How to submit", "",
            "Generate a filled-in script with `rag-drg servers submit "
            f"{s.name} <software-key> <input> [--cores N --mem GB --time HH:MM:SS --partition P --gpus G]` "
            "(MCP: `render_submit_script`). It checks the partition limits and prints the matching "
            "memory/core lines for the input. Examples with default resources:", ""]
    submit = _SUBMIT.get(s.scheduler, "qsub")
    for sw in s.software.values():
        out += [f"### Submit {sw.key}", ""]
        req = _example_request(s, sw.key)
        inp = _EXAMPLE_INPUT.get(sw.ess, "job.in")
        try:
            files, notes, meta = render_submit_files(s, sw.key, inp, partition=req["partition"], gpus=req["gpus"])
        except (SubmitError, ValueError) as e:
            out += [f"Cannot render an example: {e}", ""]
            continue
        out += [f"Input lines: {notes[0].split(': ', 1)[1]}", ""]
        for fname, text in files.items():
            lang = "bash" if fname.endswith(".sh") else "ini"
            out += [f"`{fname}`:", "", f"```{lang}", text.rstrip(), "```", ""]
        out += [f"Submit with `{meta['submit_command']}`." if s.scheduler == "htcondor"
                else f"Submit with `{submit} job.sh`.", ""]
    return "\n".join(out).rstrip() + "\n"


def render_cards(servers: dict[str, Server], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, s in servers.items():
        path = out_dir / f"{name}.md"
        path.write_text(render_card(s))
        written.append(path)
    return written
