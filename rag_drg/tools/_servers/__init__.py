"""Implementation of the `servers.yaml` cluster registry (see docs/servers-spec.md).

The public entry point is `rag_drg.tools.servers`; this package holds the pieces:

    model.py    dataclasses, loading and validation of servers.yaml
    submit.py   check_resources() and render_submit_script()
    render.py   generated cluster cards and ARC settings (remote servers, or 'local' with arc.runner)
    arc_runner.py  the batch job that runs ARC itself on a cluster, per-user ARC/conda paths
    cluster.py  read-only live scheduler/quota queries (allowlisted)
    access.py   partition `access:` rules and the requesting client's identity
    live_access.py  live queue-access report (qstat -Qf / scontrol) and the discover-pbs draft
"""
