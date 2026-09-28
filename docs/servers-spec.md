# `servers.yaml` specification (cluster registry)

One file at the repository root, `servers.yaml`, is the single source of truth for every
cluster the group uses. Cluster cards, ARC `servers` settings and filled-in submit scripts
are generated from it; the input checker validates resources against it; the read-only
cluster tools use its commands.

```yaml
servers:
  <name>:                              # short id, e.g. "zeus"; used everywhere else
    description: "..."                 # optional
    scheduler: slurm                   # slurm | pbs | pbspro | torque | sge | htcondor | local
    host: login.example.ac.il          # login node hostname
    user: null                         # null -> the invoking user's $USER
    ssh_alias: null                    # optional ~/.ssh/config Host alias used instead of host
    modules_available: false           # we call programs by absolute path
    arc:                               # optional extras for the generated ARC `servers` entry
      path: /home                      # ARC 'path' (remote base path)
      max_simultaneous_jobs: 20
    partitions:                        # Slurm partitions or PBS queues
      <partition>:
        max_walltime: "72:00:00"       # HH:MM:SS, or "D-HH:MM:SS"
        cores_per_node: 48
        mem_per_node_gb: 256
        gpus_per_node: 0               # optional
        gpu_type: null                 # optional, e.g. "A100"
        max_nodes: 1                   # optional
        default: true                  # optional; exactly one partition may be default
        notes: "..."                   # optional
    scratch:
      path: "/scratch/$USER"           # node-local or shared scratch base; $USER / $SLURM_JOB_ID allowed
      node_local: true
    storage:                           # optional list
      - name: home
        path: "/home/$USER"
        quota_gb: 50                   # optional
        backed_up: true                # optional
        quota_command: "quota -s"      # read-only command that reports usage
    software:                          # key = <ess>[-<version>], e.g. orca-6, orca-5, gaussian-16, gaussian-16-gpu,
      <key>:                           #   gaussian-09, qchem-6.1, psi4, molpro-2024, molpro-2026, pyscf
        ess: orca                      # orca | gaussian | qchem | psi4 | molpro | pyscf
        version: "6.0.1"
        executable: /abs/path/orca_6_0_1/orca
        env:                           # exported before running (values may reference $VARS)
          PATH: "/abs/path/orca_6_0_1:/abs/path/openmpi-4.1.6/bin:$PATH"
          LD_LIBRARY_PATH: "/abs/path/orca_6_0_1:/abs/path/openmpi-4.1.6/lib:$LD_LIBRARY_PATH"
        setup: []                      # extra shell lines, e.g. ["source $g16root/g16/bsd/g16.profile"]
        parallel: mpi                  # mpi (ORCA, Molpro: ntasks=N) | threads (Gaussian, Q-Chem, Psi4, PySCF: cpus-per-task=N)
        partitions: [<partition>]      # optional: where it may run (e.g. GPU build only on gpu partition)
    commands:                          # optional overrides of the scheduler's default read-only commands
      jobs: "squeue -u $USER"
      quota: "quota -s"
```

Rules (enforced by `rag-drg lint`):

* `scheduler` is one of the listed values; every partition has `max_walltime`,
  `cores_per_node`, `mem_per_node_gb`; at most one partition has `default: true`.
* `software.<key>.executable` is an absolute path; `ess` is a known ESS; `parallel` is `mpi` or `threads`.
* `partitions` referenced by software entries exist.
* No secrets (passwords, tokens, private keys) anywhere in the file; `ssh` uses keys/agents.

Python access (implemented in `rag_drg/tools/servers.py`):

```python
from rag_drg.tools.servers import load_servers   # -> dict[name, Server] (dataclasses), {} if no file
```
