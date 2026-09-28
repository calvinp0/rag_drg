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
      ess_queues: [<partition>, ...]   # optional: queues ARC may send ESS jobs to, first = ARC's default
                                       #   (default: the partitions without `access:` or GPUs)
      cpus: 16                         # optional: ARC servers['local']['cpus'] (default: the first ESS queue's node)
      memory_gb: 160                   # optional: ARC servers['local']['memory'] (same default)
      default_job_settings:            # optional: written as ARC's default_job_settings
        job_total_memory_gb: 32
        job_cpu_cores: 16
      commands:                        # optional: ARC's scheduler commands (default: ARC's /usr/local/bin/...)
        submit: /opt/pbs/bin/qsub      #   -> submit_command, check_status_command, delete_command
        status: /opt/pbs/bin/qstat
        delete: /opt/pbs/bin/qdel
      ess_installs:                    # optional: which `software` key ARC uses per ESS
        gaussian: gaussian-16-gpu      #   (default: the newest non-GPU build)
      runner:                          # optional: ARC itself runs on this cluster as a batch job
        queue: <partition>             #   queue of the runner job (e.g. alon_q)
        host: n170                     #   optional: pin to a node (PBS Pro host=, Torque nodes=, Slurm --nodelist)
        host_cores: 32                 #   optional: the pinned node's cores / memory (default: the queue's)
        host_mem_gb: 192
        cores: 1                       #   default 1
        mem_gb: 8                      #   default 8
        walltime: "3600:00:00"         #   optional; default = the queue's max walltime
        extra_setup: []                #   optional group-wide shell lines
                                       # NOT here (per user): arc_path, conda_env, conda_sh (docs/arc-run.md)
    partitions:                        # Slurm partitions or PBS queues
      <partition>:
        max_walltime: "72:00:00"       # HH:MM:SS, or "D-HH:MM:SS" (read as a string even unquoted; no base-60)
        cores_per_node: 48
        mem_per_node_gb: 256
        gpus_per_node: 0               # optional
        gpu_type: null                 # optional, e.g. "A100"
        max_nodes: 1                   # optional
        default: true                  # optional; exactly one partition may be default
        notes: "..."                   # optional
        access:                        # optional queue ACL; absent = everyone in the group may use it
          users: [alice, bob]          # Unix user names allowed
          groups: [danagrp]            # Unix groups allowed (member of ANY listed group is enough)
          notes: "ask X to be added"   # optional
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
* `software.<key>.executable` is an absolute path without whitespace or shell metacharacters; `ess` is a known ESS; `parallel` is `mpi` or `threads`.
* `partitions` referenced by software entries exist.
* No secrets (passwords, tokens, private keys) anywhere in the file; `ssh` uses keys/agents.

Additional rules added by the implementation (see docs/servers.md):

* Unknown keys are reported (catches typos such as `mem_per_node`). Optional `notes:` strings
  are also accepted on software, storage and scratch entries.
* `host` is required unless `scheduler: local` (or an `ssh_alias` is given); `host`, `user`,
  `ssh_alias` may only contain letters, digits and `_ . @ -`.
* A software key is `<ess>` or `<ess>-<anything>` (e.g. `orca-6`, `gaussian-16-gpu`); a key
  containing `gpu` marks a GPU build.
* `storage[].quota_command` and every `commands:` entry must pass the read-only allowlist used by
  the live cluster tools (`squeue sacct sinfo sshare`, `scontrol show`, `sacctmgr show|list`, `id -un|-Gn`, `qstat`, `pbsnodes` with
  read-only flags, `quota`, `df`, `lfs quota`, `mmlsquota`, `beegfs-ctl --getquota`; arguments
  only from `[A-Za-z0-9_@%:=,./+- ]`, `$USER`, and `{job_id}` in `commands.job`). Allowed
  `commands:` keys: `jobs job history partitions quota fairshare`.
* `scratch.path` is absolute (or starts with `$TMPDIR`-style variables). A per-job directory is
  created under it unless it already contains the job id variable.
* Generated cards in `knowledge/hpc/servers/generated/` must be up to date with `servers.yaml`
  and belong to an existing server.
* servers.yaml is parsed without YAML 1.1 base-60 numbers (`72:00:00` stays a string); a numeric
  `max_walltime` is hours and is rejected above 10000 (a mis-parsed or unquoted value).
* Every ESS job is single-node: `cores <= cores_per_node` (`max_nodes` is informational).
* `partitions.<p>.access` (optional) is a mapping with only `users`, `groups` (lists of Unix
  names: letters, digits, `_ . -`, not starting with `.`/`-`) and `notes` (string); it must list
  at least one user or group. A user may use the partition when their user name is listed OR
  they belong to any listed group. Omit `access` for an unrestricted partition.
* `commands:` cannot override `queue_access` (it runs a fixed set of commands, see docs/servers.md).
* `arc.ess_queues` is a non-empty list of defined partitions without repeats.
* `arc.runner` (see docs/arc-run.md): `queue` is a defined partition; `host` is a node name
  (letters, digits, `_ . -`); `host_cores`/`host_mem_gb` only with `host`; `cores`/`mem_gb` fit the
  pinned node (`host_cores`/`host_mem_gb`) or else the queue's per-node limits; `walltime` <= the
  queue's max walltime; `extra_setup` is a list of single shell lines; the scheduler is `slurm`,
  `pbs`, `pbspro` or `torque`. Per-user keys (`arc_path`, `conda_env`, `conda_sh`, `env`, `python`)
  are errors: each person gives them to `rag-drg arc compose` or in `~/.config/rag-drg/user.yaml`.
  A server with a runner is emitted as ARC's `'local'` server by `servers arc-settings`.

Live read-only commands are off unless `conf.d/servers.yaml` sets
`cluster_commands: {enabled: true}` (see docs/servers.md for why).

Python access (implemented in `rag_drg/tools/servers.py`):

```python
from rag_drg.tools.servers import load_servers   # -> dict[name, Server] (dataclasses), {} if no file
from rag_drg.tools.servers import check_resources, render_submit_script, cluster_query, arc_settings
from rag_drg.tools.servers import queue_access   # -> {allowed: True|False|None, reason, partition, notes}
from rag_drg.tools.servers import render_arc_runner_script, arc_settings_parts   # ARC runs on the cluster
from rag_drg.tools.compose_arc import compose_arc_run   # -> {submit_sh, arc_settings_py, arc_submit_py, findings, notes}
```
