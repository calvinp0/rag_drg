"""Implementation of the `servers.yaml` cluster registry (see docs/servers-spec.md).

The public entry point is `rag_drg.tools.servers`; this package holds the pieces:

    model.py    dataclasses, loading and validation of servers.yaml
    submit.py   check_resources() and render_submit_script()
    render.py   generated cluster cards and ARC settings
    cluster.py  read-only live scheduler/quota queries (allowlisted)
"""
