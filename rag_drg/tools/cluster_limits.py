"""Glue between the input checker and the cluster registry.

When `servers.yaml` exists, submit scripts checked by `rag-drg check-input` (and the
`check_input` MCP tool / Claude Code hook) are also checked against the real cluster:
partition limits (walltime, cores, memory, GPUs) and whether the program executable the
script calls is one registered for that cluster.

The server is chosen as: $RAG_DRG_SERVER if set, else the single server whose partitions
include the script's partition, else the only server in the file.
"""

from __future__ import annotations

import os

from .inputcheck import EXTRA_CHECKS, Finding
from .servers import check_resources, load_servers

_REF = "docs/servers.md"


def _pick_server(servers: dict, partition: str | None):
    wanted = os.environ.get("RAG_DRG_SERVER")
    if wanted:
        return servers.get(wanted)
    if partition:
        owners = [s for s in servers.values() if partition in s.partitions]
        if len(owners) == 1:
            return owners[0]
    if len(servers) == 1:
        return next(iter(servers.values()))
    return None


def cluster_limits_check(inp, sub, cfg) -> list[Finding]:
    if sub is None or cfg is None:
        return []
    servers = load_servers(cfg)
    if not servers:
        return []
    server = _pick_server(servers, sub.partition)
    if server is None:
        return [Finding("info", "cluster-unknown",
                        "Could not tell which cluster this script is for (set RAG_DRG_SERVER); "
                        "skipped partition-limit checks.", file=sub.filename, ref=_REF)]
    findings: list[Finding] = []
    cores = sub.total_cores
    mem_gb = (sub.mem_total_mb / 1024) if sub.mem_total_mb else None
    # check_resources reads bare numbers as hours
    walltime = sub.walltime_s / 3600 if sub.walltime_s is not None else sub.walltime
    if cores and mem_gb and walltime is not None:
        for p in check_resources(server, sub.partition, int(cores), float(mem_gb), walltime, int(sub.gpus or 0)):
            findings.append(Finding(p["severity"], "cluster-limits", f"{server.name}: {p['message']}",
                                    file=sub.filename, ref=_REF))
    # Executables should be the ones registered for this cluster.
    registered = {sw.ess: [] for sw in server.software.values()}
    for sw in server.software.values():
        registered[sw.ess].append(sw.executable)
    for exe in sub.executables:
        paths = registered.get(exe.program)
        if not paths or not exe.absolute:
            continue
        if not any(p in exe.command for p in paths) and not any(
            os.path.dirname(p) in exe.command for p in paths
        ):
            findings.append(Finding(
                "warning", "cluster-executable",
                f"{server.name}: {exe.program} is called as `{exe.command.split()[0]}`, which is not an install "
                f"registered in servers.yaml ({', '.join(paths)}).",
                line=exe.line, file=sub.filename, ref=_REF,
                fix="use the registered absolute path (see `rag-drg servers show " + server.name + "`)",
            ))
    return findings


if cluster_limits_check not in EXTRA_CHECKS:
    EXTRA_CHECKS.append(cluster_limits_check)
