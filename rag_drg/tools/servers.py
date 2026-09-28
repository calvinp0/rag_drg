"""Cluster registry plugin: `servers.yaml` -> cards, ARC settings, submit scripts, live queries.

Spec: docs/servers-spec.md; usage: docs/servers.md.

    rag-drg servers [--file F] list | show NAME | render-cards [--out DIR] | arc-settings [NAME ...]
    rag-drg servers submit SERVER SOFTWARE INPUT [--cores N --mem GB --time T --partition P --gpus G -o FILE]
    rag-drg servers check SERVER [PARTITION] --cores N --mem GB --time T [--gpus G --software KEY]
    rag-drg servers query SERVER {jobs,job,history,partitions,quota,fairshare,queue_access} [JOB_ID]
    rag-drg servers access SERVER [--user U --groups G1,G2] [--live]
    rag-drg servers discover-pbs --from-file qstat_Qf.txt [--pbsnodes pbsnodes.txt]

Python API (also used by other plugins, e.g. an input checker)::

    from rag_drg.tools.servers import load_servers, check_resources, render_submit_script, cluster_query
    from rag_drg.tools.servers import queue_access, client_identity, CLIENT_IDENTITY, client_identity_scope
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._servers.access import (
    CLIENT_IDENTITY,
    access_problem,
    access_table,
    identity_for,
    client_identity,
    client_identity_scope,
    local_identity,
    local_identity_allowed,
    queue_access,
)
from ._servers.cluster import cluster_commands_enabled
from ._servers.cluster import cluster_query as _cluster_query
from ._servers.live_access import discover_pbs, live_queue_access
from ._servers.model import (
    QUERY_KINDS,
    Access,
    Partition,
    Scratch,
    Server,
    ServersConfigError,
    SoftwareInstall,
    Storage,
    check_file,
    load_servers,
    servers_path,
)
from ._servers.render import access_text, arc_settings, generated_dir, render_card, render_cards
from ._servers.submit import SubmitError, check_resources, render_submit_script

__all__ = [
    "Access", "CLIENT_IDENTITY", "access_problem", "client_identity", "client_identity_scope", "discover_pbs",
    "identity_for",
    "live_queue_access", "local_identity", "local_identity_allowed", "queue_access",
    "Partition", "Scratch", "Server", "ServersConfigError", "SoftwareInstall", "Storage", "SubmitError",
    "arc_settings", "check_file", "check_resources", "cluster_query", "load_servers", "render_card",
    "render_cards", "render_submit_script",
]


def cluster_query(server: Server, what: str, job_id: str | None = None, runner=None) -> str:
    """Read-only scheduler/quota query on a cluster (see rag_drg/tools/_servers/cluster.py)."""
    return _cluster_query(server, what, job_id, runner=runner)


# ----------------------------------------------------------------- summaries

def _summary(servers: dict[str, Server]) -> list[dict]:
    return [
        {
            "name": s.name,
            "scheduler": s.scheduler,
            "host": s.host,
            "description": s.description,
            "partitions": {p.name: {"max_walltime": p.max_walltime, "cores_per_node": p.cores_per_node,
                                    "mem_per_node_gb": p.mem_per_node_gb, "gpus_per_node": p.gpus_per_node,
                                    "default": p.default,
                                    "access": None if p.access is None else {
                                        "users": p.access.users, "groups": p.access.groups,
                                        "notes": p.access.notes}}
                           for p in s.partitions.values()},
            "software": {k: f"{sw.ess} {sw.version or ''}".strip() for k, sw in s.software.items()},
        }
        for s in servers.values()
    ]


def _list_text(servers: dict[str, Server]) -> str:
    if not servers:
        return ("No servers.yaml at the repository root (or it lists no servers). "
                "See docs/servers.md and servers.example.yaml.")
    lines = []
    for s in servers.values():
        parts = ", ".join(f"{p.name}{'*' if p.default else ''} ({p.max_walltime}, {p.cores_per_node}c, "
                          f"{p.mem_per_node_gb:g}GB{', ' + str(p.gpus_per_node) + ' GPU' if p.gpus_per_node else ''})"
                          for p in s.partitions.values())
        lines.append(f"{s.name}: {s.scheduler} @ {s.host or 'local'}"
                     + (f" - {s.description.strip()}" if s.description else ""))
        lines.append(f"  partitions: {parts}")
        for p in s.partitions.values():
            if p.access is not None:
                lines.append(f"  restricted: {p.name} -> only {access_text(p)}"
                             + (f" ({p.access.notes.strip()})" if p.access.notes else ""))
        lines.append(f"  software: {', '.join(s.software) or '(none)'}")
    return "\n".join(lines)


def _identity_line(user, groups, source) -> str:
    src = {"explicit": "given", "context": "set by the server for this request",
           "env": "from RAG_DRG_CLIENT_USER/RAG_DRG_CLIENT_GROUPS", "local": "this process",
           None: "unknown"}[source]
    return (f"identity ({src}): user {user or '?'}, groups "
            f"{', '.join(groups) if groups is not None else '?'}")


def _client_access_rows(server: Server, partition: str | None, explicit) -> tuple[list[dict], str]:
    """queue_access rows for the requesting client + an identity line. A denial judged from this
    machine's identity for a remote cluster is reported as unknown (allowed=None)."""
    user, groups, source, authoritative = identity_for(server, explicit)
    rows = access_table(server, user, groups) if partition is None else [queue_access(server, partition, user, groups)]
    line = _identity_line(user, groups, source)
    if source is not None and not authoritative:
        line += f" - not necessarily your account on {server.name} (this machine is not the cluster)"
        for r in rows:
            if r["allowed"] is False:
                r["allowed"] = None
                r["reason"] += " (judged from this machine's identity; check with --live or pass --user/--groups)"
    return rows, line


def _access_text(server: Server, explicit) -> str:
    word = {True: "yes", False: "NO", None: "unknown"}
    rows, line = _client_access_rows(server, None, explicit)
    lines = [f"{server.name}: {line}"]
    for r in rows:
        lines.append(f"  {r['partition']}: {word[r['allowed']]} - {r['reason']}")
    return "\n".join(lines)


def _client_check(server: Server, partition, cores, mem_gb, walltime, gpus, software, explicit) -> list[dict]:
    """check_resources for the requesting client (access judged by access_problem)."""
    problems = check_resources(server, partition, cores, mem_gb, walltime, gpus, software=software,
                               check_access=False)
    ap = access_problem(server, partition, explicit)
    return ([ap] if ap else []) + problems


def _submit_identity(server: Server, explicit) -> tuple[str | None, list[str] | None]:
    user, groups, _source, authoritative = identity_for(server, explicit)
    return (user, groups) if authoritative else (None, None)


def _explicit_identity(user, groups):
    """(user, groups) if the caller gave either, else None (= resolve with client_identity)."""
    if user is None and groups is None:
        return None
    if isinstance(groups, str):
        groups = [g for g in groups.split(",") if g.strip()]
    return (user, groups)


def _format_problems(problems: list[dict]) -> str:
    return "\n".join(f"{p['severity']}: {p['message']}" for p in problems) or "ok: the request fits"


# ----------------------------------------------------------------- CLI

def register_cli(subparsers):
    p = subparsers.add_parser("servers", help="cluster registry (servers.yaml): cards, ARC settings, submit scripts, queries")
    p.add_argument("--file", help="servers file to use instead of <repo>/servers.yaml (e.g. servers.example.yaml)")
    ssub = p.add_subparsers(dest="servers_cmd", required=True)

    ssub.add_parser("list", help="list clusters, partitions and installed software")
    q = ssub.add_parser("show", help="print the full card for one cluster")
    q.add_argument("name")
    ssub.add_parser("validate", help="check servers.yaml (same checks as `rag-drg lint`)")

    q = ssub.add_parser("render-cards", help="write knowledge/hpc/servers/generated/<name>.md for every cluster")
    q.add_argument("--out", help="output directory (default: knowledge/hpc/servers/generated)")

    q = ssub.add_parser("arc-settings", help="print ARC `servers` + `global_ess_settings` for ~/.arc/settings.py")
    q.add_argument("names", nargs="*", help="clusters to include (default: all)")

    q = ssub.add_parser("submit", help="print a ready-to-run submit script (notes go to stderr)")
    q.add_argument("server")
    q.add_argument("software", help="servers.yaml software key, e.g. orca-6, gaussian-16-gpu")
    q.add_argument("input", help="input file, relative to the submit directory")
    q.add_argument("--job-name")
    q.add_argument("--cores", type=int)
    q.add_argument("--mem", type=float, dest="mem_gb", help="total memory in GB")
    q.add_argument("--time", dest="walltime", help="HH:MM:SS or D-HH:MM:SS")
    q.add_argument("--partition")
    q.add_argument("--gpus", type=int, default=0)
    q.add_argument("--user", help="check queue access for this Unix user")
    q.add_argument("--groups", help="comma-separated Unix groups of that user")
    q.add_argument("-o", "--output", help="write the script to this file instead of stdout")

    q = ssub.add_parser("check", help="check a resource request against partition limits")
    q.add_argument("server")
    q.add_argument("partition", nargs="?")
    q.add_argument("--cores", type=int, required=True)
    q.add_argument("--mem", type=float, dest="mem_gb", required=True)
    q.add_argument("--time", dest="walltime", required=True)
    q.add_argument("--gpus", type=int, default=0)
    q.add_argument("--software")
    q.add_argument("--user", help="Unix user to check queue access for (default: see `servers access`)")
    q.add_argument("--groups", help="comma-separated Unix groups of that user")

    q = ssub.add_parser("access", help="which partitions/queues may I use? (servers.yaml access rules)")
    q.add_argument("server")
    q.add_argument("--user", help="user name (default: RAG_DRG_CLIENT_USER, else this process's user)")
    q.add_argument("--groups", help="comma-separated groups (default: RAG_DRG_CLIENT_GROUPS, else this process's)")
    q.add_argument("--live", action="store_true",
                   help="also ask the scheduler (qstat -Qf / scontrol) as you over SSH; needs cluster_commands")

    q = ssub.add_parser("discover-pbs", help="DRAFT a partitions: block from saved `qstat -Qf` [+ pbsnodes] output")
    q.add_argument("--from-file", required=True, dest="qstat_file", help="file with `qstat -Qf` output")
    q.add_argument("--pbsnodes", help="file with `pbsnodes -a` or `pbsnodes -aSj` output")

    q = ssub.add_parser("query", help="read-only live query (needs cluster_commands.enabled in conf.d/servers.yaml)")
    q.add_argument("server")
    q.add_argument("what", choices=QUERY_KINDS)
    q.add_argument("job_id", nargs="?")

    return {"servers": _cli}


def _cli(args: argparse.Namespace, cfg) -> int:
    path = Path(args.file) if args.file else servers_path(cfg)
    cmd = args.servers_cmd
    if cmd == "discover-pbs":
        try:
            qf = Path(args.qstat_file).read_text()
            nodes = Path(args.pbsnodes).read_text() if args.pbsnodes else None
        except OSError as e:
            print(e, file=sys.stderr)
            return 1
        print(discover_pbs(qf, nodes), end="")
        return 0
    if cmd == "validate":
        problems = check_file(path) if path.is_file() else []
        print("\n".join(problems) if problems else f"{path.name}: ok" if path.is_file() else f"no {path}")
        return 1 if problems else 0
    try:
        servers = load_servers(cfg, path)
    except ServersConfigError as e:
        print(e, file=sys.stderr)
        return 1

    def get(name: str) -> Server | None:
        if name not in servers:
            print(f"unknown server {name!r}; known: {', '.join(servers) or '(none)'}", file=sys.stderr)
            return None
        return servers[name]

    if cmd == "list":
        print(_list_text(servers))
        return 0
    if cmd == "show":
        s = get(args.name)
        if not s:
            return 1
        print(render_card(s))
        return 0
    if cmd == "render-cards":
        out = Path(args.out) if args.out else generated_dir(cfg)
        for f in render_cards(servers, out):
            print(f"wrote {f}")
        if not servers:
            print(f"no servers in {path}", file=sys.stderr)
        return 0
    if cmd == "arc-settings":
        try:
            print(arc_settings(servers, args.names or None), end="")
        except KeyError as e:
            print(e.args[0], file=sys.stderr)
            return 1
        return 0
    if cmd == "submit":
        s = get(args.server)
        if not s:
            return 1
        try:
            user, groups = _submit_identity(s, _explicit_identity(args.user, args.groups))
            script, notes = render_submit_script(
                s, args.software, args.input, job_name=args.job_name, cores=args.cores, mem_gb=args.mem_gb,
                walltime=args.walltime, partition=args.partition, gpus=args.gpus, user=user, groups=groups,
            )
        except SubmitError as e:
            print(_format_problems(e.problems), file=sys.stderr)
            return 1
        if args.output:
            Path(args.output).write_text(script)
            print(f"wrote {args.output}", file=sys.stderr)
        else:
            print(script, end="")
        for n in notes:
            print(f"# {n}", file=sys.stderr)
        return 0
    if cmd == "check":
        s = get(args.server)
        if not s:
            return 1
        problems = _client_check(s, args.partition, args.cores, args.mem_gb, args.walltime, args.gpus,
                                 args.software, _explicit_identity(args.user, args.groups))
        print(_format_problems(problems))
        return 1 if any(p["severity"] == "error" for p in problems) else 0
    if cmd == "access":
        s = get(args.server)
        if not s:
            return 1
        print(_access_text(s, _explicit_identity(args.user, args.groups)))
        if not args.live:
            return 0
        if not cluster_commands_enabled(cfg):
            print("--live needs live cluster commands; set `cluster_commands: {enabled: true}` in "
                  "conf.d/ (see docs/servers.md).", file=sys.stderr)
            return 1
        print()
        try:
            print(cluster_query(s, "queue_access"))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if cmd == "query":
        if not cluster_commands_enabled(cfg):
            print("Live cluster commands are disabled; set `cluster_commands: {enabled: true}` in "
                  "conf.d/servers.yaml (see docs/servers.md).", file=sys.stderr)
            return 1
        s = get(args.server)
        if not s:
            return 1
        try:
            print(cluster_query(s, args.what, args.job_id))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    return 2


# ----------------------------------------------------------------- MCP

def register_mcp(mcp, ctx) -> None:
    cfg = ctx.cfg

    def _load() -> dict[str, Server]:
        return load_servers(cfg)

    @mcp.tool()
    def list_servers() -> str:
        """List the group's clusters from servers.yaml: scheduler, login host, partitions
        (max walltime, cores/node, memory/node, GPUs; * = default) and installed software keys.
        Restricted queues (queue ACLs) are listed with who may use them; check a person with
        queue_access(). Call this before writing a submit script or choosing where to run a calculation."""
        try:
            servers = _load()
        except ServersConfigError as e:
            return str(e)
        ctx.emit({"tool": "list_servers", "args": {}, "n_results": len(servers)})
        return _list_text(servers)

    @mcp.tool()
    def server_info(name: str) -> str:
        """Everything about one cluster: access, partitions/limits, absolute executables and
        environment setup lines of each ESS, storage/quota commands, scratch, and an example submit
        script per ESS (Markdown).

        Args:
            name: Cluster name from list_servers().
        """
        try:
            servers = _load()
        except ServersConfigError as e:
            return str(e)
        ctx.emit({"tool": "server_info", "args": {"name": name}, "n_results": int(name in servers)})
        if name not in servers:
            return f"Unknown server {name!r}. Known: {', '.join(servers) or '(none)'}"
        return render_card(servers[name])

    @mcp.tool(name="render_submit_script")
    def render_submit_script_tool(
        server: str,
        software: str,
        input_file: str,
        job_name: str | None = None,
        cores: int | None = None,
        mem_gb: float | None = None,
        walltime: str | None = None,
        partition: str | None = None,
        gpus: int = 0,
        user: str | None = None,
        groups: list[str] | None = None,
    ) -> str:
        """Render a ready-to-run Slurm/PBS submit script for one ESS job on a cluster, with the
        correct absolute executable, environment, parallel model and scratch handling, validated
        against the partition limits. Also returns the memory/core lines the INPUT must contain
        (ORCA %pal/%maxcore, Gaussian %nprocshared/%mem, Q-Chem MEM_TOTAL, Molpro memory, ...).

        Args:
            server: Cluster name (list_servers()).
            software: Software key on that cluster, e.g. orca-6, gaussian-16, gaussian-16-gpu, qchem-6.1,
                psi4, molpro-2024, pyscf.
            input_file: Input file name relative to the submit directory (e.g. "ts1.inp").
            job_name: Defaults to the input file stem.
            cores: Cores (default min(16, cores per node)).
            mem_gb: TOTAL memory in GB (default ~90% of the proportional share of the node).
            walltime: "HH:MM:SS" or "D-HH:MM:SS" (default min(24 h, partition max)).
            partition: Partition/queue (default: the software's allowed default partition).
            gpus: GPUs to request (GPU builds such as gaussian-16-gpu need >= 1).
            user: The requesting person's Unix user name on the cluster, to check restricted queues
                (queue ACLs). Omit if unknown.
            groups: That person's Unix groups on the cluster (e.g. from `id -Gn`). Omit if unknown.
        """
        args = {"server": server, "software": software, "input_file": input_file, "cores": cores,
                "mem_gb": mem_gb, "walltime": walltime, "partition": partition, "gpus": gpus}
        try:
            servers = _load()
        except ServersConfigError as e:
            return str(e)
        if server not in servers:
            ctx.emit({"tool": "render_submit_script", "args": args, "n_results": 0})
            return f"Unknown server {server!r}. Known: {', '.join(servers) or '(none)'}"
        try:
            ident_user, ident_groups = _submit_identity(servers[server], _explicit_identity(user, groups))
            script, notes = render_submit_script(
                servers[server], software, input_file, job_name=job_name, cores=cores, mem_gb=mem_gb,
                walltime=walltime, partition=partition, gpus=gpus, user=ident_user, groups=ident_groups,
            )
        except SubmitError as e:
            ctx.emit({"tool": "render_submit_script", "args": args, "n_results": 0})
            return "Cannot render the script:\n" + _format_problems(e.problems)
        ctx.emit({"tool": "render_submit_script", "args": args, "n_results": 1})
        return "```bash\n" + script + "```\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes)

    @mcp.tool(name="check_resources")
    def check_resources_tool(
        server: str,
        partition: str | None,
        cores: int,
        mem_gb: float,
        walltime: str,
        gpus: int = 0,
        software: str | None = None,
        user: str | None = None,
        groups: list[str] | None = None,
    ) -> str:
        """Check a job's resources against a cluster partition's limits (walltime, cores/node,
        memory/node, GPUs, and optionally whether `software` may run there). Returns a JSON list of
        {severity: error|warning|info, message}; an empty list means it fits.

        Args:
            server: Cluster name.
            partition: Partition/queue name, or null for the default partition.
            cores: Cores per job (single node).
            mem_gb: TOTAL memory in GB.
            walltime: "HH:MM:SS" or "D-HH:MM:SS".
            gpus: GPUs requested.
            software: Optional software key (e.g. gaussian-16-gpu) to check partition restrictions.
            user: The requesting person's Unix user name on the cluster (restricted queues). Optional.
            groups: That person's Unix groups on the cluster. Optional.
        """
        try:
            servers = _load()
        except ServersConfigError as e:
            return str(e)
        if server in servers:
            problems = _client_check(servers[server], partition, cores, mem_gb, walltime, gpus, software,
                                     _explicit_identity(user, groups))
        else:
            problems = check_resources(server, partition, cores, mem_gb, walltime, gpus, software=software, cfg=cfg)
        ctx.emit({"tool": "check_resources",
                  "args": {"server": server, "partition": partition, "cores": cores, "mem_gb": mem_gb,
                           "walltime": walltime, "gpus": gpus, "software": software},
                  "n_results": len(problems)})
        return json.dumps(problems, indent=1)

    @mcp.tool(name="queue_access")
    def queue_access_tool(server: str, partition: str | None = None, user: str | None = None,
                          groups: list[str] | None = None) -> str:
        """Which partitions/queues of a cluster may this person submit to? Evaluates the
        servers.yaml `access:` rules (queue ACLs: listed users, or members of ANY listed group).
        Returns a JSON list of {partition, allowed: true|false|null, reason, notes}; null means the
        queue is restricted and the user/groups are unknown - then ask the user (`id -Gn` on the
        cluster) or pass them.

        Args:
            server: Cluster name.
            partition: One partition/queue, or null for all of them.
            user: The person's Unix user name on the cluster. Optional.
            groups: The person's Unix groups on the cluster. Optional.
        """
        try:
            servers = _load()
        except ServersConfigError as e:
            return str(e)
        if server not in servers:
            ctx.emit({"tool": "queue_access", "args": {"server": server}, "n_results": 0})
            return f"Unknown server {server!r}. Known: {', '.join(servers) or '(none)'}"
        rows, _line = _client_access_rows(servers[server], partition, _explicit_identity(user, groups))
        ctx.emit({"tool": "queue_access", "args": {"server": server, "partition": partition},
                  "n_results": len(rows)})
        return json.dumps(rows, indent=1)

    # Never on the shared HTTP server: commands would run with the service account's SSH identity,
    # so `$USER`, quotas and queue access would describe the service account, not the requester.
    from ._servers.access import local_identity_allowed

    if cluster_commands_enabled(cfg) and not ctx.readonly and local_identity_allowed():

        @mcp.tool(name="cluster_query")
        def cluster_query_tool(server: str, what: str, job_id: str | None = None) -> str:
            """Run a READ-ONLY scheduler/quota command on a cluster over SSH and return its output
            (truncated to ~8 kB). Only allowlisted commands run; nothing is submitted or cancelled.

            Args:
                server: Cluster name.
                what: jobs (my queued/running jobs) | job (one job's details; needs job_id) |
                    history (my jobs of the last 7 days) | partitions (partition/queue state) |
                    quota (storage usage) | fairshare (my fair-share / priority) |
                    queue_access (which queues I may use, from qstat -Qf / scontrol + id -Gn, with limits).
                job_id: Numeric job id (e.g. 123456 or 123456.pbs01), only with what="job".
            """
            try:
                servers = _load()
            except ServersConfigError as e:
                return str(e)
            ctx.emit({"tool": "cluster_query", "args": {"server": server, "what": what, "job_id": job_id},
                      "n_results": int(server in servers)})
            if server not in servers:
                return f"Unknown server {server!r}. Known: {', '.join(servers) or '(none)'}"
            try:
                return cluster_query(servers[server], what, job_id)
            except ValueError as e:
                return f"Refused: {e}"


# ----------------------------------------------------------------- lint

def lint(cfg) -> list[str]:
    """servers.yaml validation + generated cards that are stale or belong to no server."""
    from ..chunking import split_front_matter

    problems: list[str] = []
    path = servers_path(cfg)
    servers: dict[str, Server] | None = {}
    if path.is_file():
        problems += check_file(path)
        servers = None if problems else load_servers(cfg)
    gen = generated_dir(cfg)
    if gen.is_dir() and servers is not None:
        for f in sorted(gen.glob("*.md")):
            if f.name.lower() == "readme.md":
                continue
            meta, _ = split_front_matter(f.read_text(errors="replace"))
            if not (meta or {}).get("generated"):
                continue
            rel = f.relative_to(cfg.root) if f.is_relative_to(cfg.root) else f
            if f.stem not in servers:
                problems.append(f"{rel}: generated card for a server that is not in servers.yaml; delete it")
            elif f.read_text() != render_card(servers[f.stem]):
                problems.append(f"{rel}: out of date with servers.yaml; run `rag-drg servers render-cards`")
        for name in servers:
            if not (gen / f"{name}.md").is_file():
                problems.append(f"servers.yaml: no generated card for {name!r}; run `rag-drg servers render-cards`")
    return problems
