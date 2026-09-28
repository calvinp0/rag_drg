# Cluster registry: `servers.yaml`

One file at the repository root, `servers.yaml`, describes every cluster the group uses
(format: [`servers-spec.md`](servers-spec.md)). From it `rag-drg` generates:

* one searchable **cluster card** per cluster (`knowledge/hpc/servers/generated/<name>.md`),
* the **ARC `servers` settings** for `~/.arc/settings.py`,
* **ready-to-run submit scripts** for every installed ESS, checked against the partition limits,
  together with the matching memory/core lines for the input file,
* optional **read-only live queries** (my jobs, job details, history, partitions, quota, fair share).

Code: `rag_drg/tools/servers.py` (CLI, MCP tools, lint hook) and `rag_drg/tools/_servers/`.

## What the group needs to provide (per cluster)

Start from [`servers.example.yaml`](../servers.example.yaml) (a fake cluster that uses every field)
and write `servers.yaml` with the real values:

1. **Access**: login host name (and an optional `~/.ssh/config` alias), scheduler
   (`slurm`, `pbs`/`pbspro`, `torque`, `sge`, `htcondor`, `local`). No usernames needed
   (`user: null` = each person's own account) and **never passwords or keys**.
2. **Partitions/queues** we are allowed to use: max walltime, cores per node, memory per node (GB),
   GPUs per node and GPU type, which one is the default, and who may use restricted ones
   (`access:`, see [Restricted queues](#restricted-queues-access)). Get them from
   `sinfo -o "%P %l %c %m %G"` / `scontrol show partition` (Slurm) or `qstat -Qf` / `pbsnodes -aSj`
   (PBS), or the cluster documentation. For PBS, `rag-drg servers discover-pbs` drafts the block.
   Write walltimes as `"72:00:00"` (quoted or not: servers.yaml is read without YAML 1.1's base-60
   numbers, so an unquoted `72:00:00` is not the integer 259200). A bare number means hours; one
   above 10000 is rejected as a likely mis-parse.
3. **Every ESS install**: absolute path of the executable (no spaces or shell metacharacters), the environment lines it needs
   (e.g. ORCA's OpenMPI `PATH`/`LD_LIBRARY_PATH`, `g16root` + `source $g16root/g16/bsd/g16.profile`,
   `QC`/`QCAUX` + `source $QC/qcenv.sh`, the Python env of Psi4/PySCF), MPI vs threads, and the
   partitions it may run on (e.g. the Gaussian GPU build only on the GPU partition).
   Keys: `orca-5`, `orca-6`, `gaussian-09`, `gaussian-16`, `gaussian-16-gpu`, `qchem-6.1`, `psi4`,
   `molpro-2024`, `molpro-2026`, `pyscf` (any `<ess>-<suffix>` works).
4. **Scratch**: where per-job scratch goes (node-local disk or `$TMPDIR`, or shared scratch).
5. **Storage**: home/project/group areas with quota, backup status, and the command that shows
   usage (`quota -s`, `lfs quota -h -u $USER /lustre`, `df -h /data/...`, `mmlsquota`, ...).
6. Optional: ARC `path` (remote base dir, usually `/home`), `max_simultaneous_jobs`,
   `ess_queues` (the queues ARC may send ESS jobs to, first = default) and, when ARC itself runs on
   the cluster as a batch job, `arc.runner` (queue, node, default cores/memory/walltime; see
   [`arc-run.md`](arc-run.md)); `commands:` overrides if a cluster needs different read-only
   query commands.

Then:

```bash
rag-drg servers validate          # or: rag-drg lint (also run in CI)
rag-drg servers render-cards      # writes knowledge/hpc/servers/generated/<name>.md
rag-drg ingest                    # index the new cards
git add servers.yaml knowledge/hpc/servers/generated/ && git commit
```

`rag-drg lint` fails when a generated card is out of date with `servers.yaml`, so re-run
`render-cards` after every change. Generated cards always say `status: draft`; fix the data in
`servers.yaml`, not in the card. The hand-written template
`knowledge/hpc/servers/_TEMPLATE.md` is still useful for house rules that don't fit the YAML.

## CLI

Everything is under one command, `rag-drg servers`. `--file F` uses another file, e.g.
`rag-drg servers --file servers.example.yaml list` to try things out.

| Command | Does |
|---|---|
| `servers list` | clusters, partitions (`*` = default), software keys |
| `servers show NAME` | the full card (same text as the generated file) |
| `servers validate` | validation problems of `servers.yaml` |
| `servers render-cards [--out DIR]` | write one card per cluster |
| `servers arc-settings [NAME ...] [--local NAME \| --no-local]` | Python `servers = {...}` and a suggested `global_ess_settings` for `~/.arc/settings.py`, plus the **required** `submit_scripts = {...}` stubs for `~/.arc/submit.py` (see below); a server with `arc.runner` becomes ARC's `'local'` server |
| `servers submit SERVER SOFTWARE INPUT [--cores N --mem GB --time T --partition P --gpus G --job-name J -o FILE]` | submit script on stdout (or `-o`), notes (input lines, warnings) on stderr |
| `servers check SERVER [PARTITION] --cores N --mem GB --time T [--gpus G --software KEY]` | check a request against the limits (exit 1 on errors) |
| `servers query SERVER {jobs,job,history,partitions,quota,fairshare,queue_access} [JOB_ID]` | read-only live query (disabled by default, see below) |
| `servers access SERVER [--user U --groups G1,G2] [--live]` | which partitions you may use (static `access:` rules); `--live` also asks the scheduler as you |
| `servers discover-pbs --from-file qstat_Qf.txt [--pbsnodes nodes.txt]` | print a DRAFT `partitions:` block from saved `qstat -Qf` (+ `pbsnodes -a`/`-aSj`) output |

`servers check` and `servers submit` also take `--user U --groups G1,G2` for restricted queues.

To run ARC itself on a cluster (runner job + `'local'` ARC settings + input.yml checks), use
`rag-drg arc compose input.yml --server NAME` (MCP `compose_arc_run`); see [`arc-run.md`](arc-run.md).

Example:

```bash
rag-drg servers submit zeus orca-6 ts1.inp --cores 16 --mem 64 --time 48:00:00 -o ts1.sh
# put in the input file: %pal nprocs 16 end | %maxcore 3072 | ...
sbatch ts1.sh
```

### What the submit scripts do

They mirror `knowledge/hpc/templates/*.sh`:

* **Parallel model** from the software entry: `mpi` (ORCA, Molpro) -> `--ntasks=N --cpus-per-task=1`
  (PBS: `mpiprocs=N`); `threads` (Gaussian, Q-Chem, Psi4, PySCF) -> `--ntasks=1 --cpus-per-task=N`.
  Jobs are single-node. Memory is requested as total memory (`--mem`, PBS `mem=`).
* **Absolute executable** plus the `env:` exports and `setup:` lines, no `module load`.
* **Per-job scratch** `<scratch.path>/<job id>` (default `${TMPDIR:-/tmp}/<job id>`), removed by a
  `trap cleanup EXIT` handler (`TERM`/`INT` exit through it), so a walltime kill or `scancel`/`qdel`
  also copies ORCA's files back and removes node-local scratch: ORCA copies the input there, runs there and copies `.gbw/.hess/.xyz/...` back;
  Gaussian uses `GAUSS_SCRDIR`; Q-Chem `QCSCRATCH`/`QCLOCALSCR` and `qchem -nt N`; Molpro
  `molpro -n N -d $SCRATCH`; Psi4 `PSI_SCRATCH` and `psi4 -n N` (or the env's `python` for a `.py`
  input); PySCF `PYSCF_TMPDIR` and `OMP_NUM_THREADS`.
* **Schedulers**: Slurm, PBS Pro/OpenPBS (`pbs`, `pbspro`: `select=1:ncpus=...`), Torque
  (`nodes=1:ppn=...`), and `local` (plain bash). SGE and HTCondor clusters can be registered
  (cards, ARC settings) but have no submit-script renderer yet.
* **Defaults**: the software's default/allowed partition, `min(16, cores/node)` cores, 90% of
  the proportional share of node memory, `min(24 h, max walltime)`.
* **Checks** (errors abort): unknown partition, cores > cores/node, memory > memory/node,
  walltime > max, GPUs on a CPU partition or more than the node has, software not allowed on the
  partition. Warnings: memory > 95% of the node, GPU partition without GPUs, GPU build without GPUs.
* **Notes** give the matching input lines: ORCA `%pal nprocs N end` + `%maxcore` (75% of memory
  per core, MB); Gaussian `%nprocshared=N` + `%mem` (88% of total; GPU: `%cpu` + `%gpucpu`);
  Q-Chem `MEM_TOTAL` (MB, 88%); Molpro `memory,<Mw>,m` per process (88% / N / 8 bytes);
  Psi4 `memory X GB` / `psi4.set_memory`; PySCF `max_memory` (MB).

## MCP tools

| Tool | Purpose |
|---|---|
| `list_servers()` | clusters, partitions, software keys |
| `server_info(name)` | the full card for one cluster |
| `render_submit_script(server, software, input_file, job_name, cores, mem_gb, walltime, partition, gpus, user, groups)` | script + input lines, or the limit violations |
| `check_resources(server, partition, cores, mem_gb, walltime, gpus, software, user, groups)` | JSON list of `{severity, message}` (`[]` = fits) |
| `queue_access(server, partition, user, groups)` | JSON list of `{partition, allowed: true/false/null, reason, notes}` |
| `compose_arc_run(input_content, server, input_file, arc_path, conda_env, conda_sh, user, groups)` | JSON `{submit_sh, arc_settings_py, arc_submit_py, findings, notes}` for ARC running on the cluster ([`arc-run.md`](arc-run.md)) |
| `cluster_query(server, what, job_id)` | read-only live query; only registered when enabled and the server is not `--readonly` |

Python (for other plugins, e.g. an input checker):

```python
from rag_drg.tools.servers import load_servers, check_resources, render_submit_script
servers = load_servers(cfg)                     # {} if there is no servers.yaml
problems = check_resources(servers["zeus"], "cpu", cores=16, mem_gb=64, walltime="24:00:00")
problems = check_resources("zeus", None, 16, 64, "24:00:00", software="orca-6", cfg=cfg)
```

## Restricted queues (`access`)

PBS queues can be limited to some users/groups (queue ACLs: `acl_user_enable`/`acl_users`,
`acl_group_enable`/`acl_groups` in `qstat -Qf <queue>`); Slurm partitions likewise
(`AllowGroups`, `AllowAccounts`, `DenyAccounts` in `scontrol show partition`). Record them per
partition so agents do not write scripts for queues the user cannot submit to:

```yaml
    partitions:
      gpu:
        ...
        access:                 # optional; absent = everyone in the group may use it
          users: [alice, bob]   # Unix user names allowed
          groups: [danagrp]     # Unix groups allowed (member of ANY listed group is enough)
          notes: "ask X to be added"
```

`queue_access(server, partition, user, groups)` answers `allowed: true` (no rule, user listed,
or a group matches), `false`, or `null` = unknown (a rule exists but the user/groups that could
admit them are not known). `check_resources` adds an **error** for a denied partition and an
**info** when unknown; `render_submit_script` refuses a denied partition and, when it picks the
default partition for a known identity, skips partitions that identity may not use. Cards,
`list_servers` and `server_info` show the rules. The Python functions never guess the identity:
pass `user`/`groups`, or `use_local_identity=True` to `queue_access`.

### Whose identity? (contract for the CLI, MCP tools, hook and REST layer)

The process evaluating the rules is not always the person submitting: on the shared server it is
a service account. The CLI, the MCP tools and the input-checker bridge
(`rag_drg/tools/cluster_limits.py`, code `cluster-access`) resolve the requesting identity with
`client_identity()` (in `rag_drg.tools.servers`, re-exported by `cluster_limits`), first match wins:

1. explicit values (`--user/--groups`, the MCP tools' `user`/`groups` arguments);
2. the context variable `CLIENT_IDENTITY` (`contextvars.ContextVar`, value `(user, [groups])`,
   either may be `None`), which a server sets per request:
   `with client_identity_scope(user, groups): ...` (or `CLIENT_IDENTITY.set(...)` / `.reset(token)`);
3. environment variables `RAG_DRG_CLIENT_USER` and `RAG_DRG_CLIENT_GROUPS` (comma-separated);
4. this process's user and groups (`getpass.getuser()`, `os.getgroups()`), **only** when
   `local_identity_allowed()`: false when `RAG_DRG_SERVER_MODE` is set to anything but
   `0/false/no/off`. **A shared MCP/HTTP server must set `RAG_DRG_SERVER_MODE=1`.**

The local identity is only *authoritative* when this machine is the cluster (`scheduler: local`,
or running on the login host): a laptop's user name and groups rarely match the cluster account.
So a denial judged from a laptop's identity is a **warning** (not an error that blocks the hook),
and `servers access` shows it as `unknown`. Verify with `--live`.

### Live check and discovery

`rag-drg servers access NAME --live` (or `cluster_query(server, "queue_access")`, both need live
commands enabled, see below) runs, as you over SSH: `id -un`, `id -Gn` and
- PBS Pro/OpenPBS: `qstat -Qf` - per queue: `usable` yes/no/unknown and why (`enabled`, `started`,
  `acl_users`, `acl_groups`; every enabled ACL must admit you), plus limits
  (`resources_max.walltime/ncpus/mem/ngpus`, `max_run`, `max_user_run`, ...). PBS compares
  `acl_groups` with the job's group (one group), so a queue is usable if ANY one of your groups
  is admitted (`-badgrp,+danagrp` still admits a member of both, via danagrp); if that group is not
  your primary group (the first of `id -Gn`), the report says to submit with `#PBS -W group_list=<group>`. Torque is not supported (different output).
- Slurm: `scontrol show partition` + `sacctmgr show assoc user=$USER format=Account,Partition -P -n`
  (`State`, `AllowGroups`, `AllowAccounts`/`DenyAccounts` vs. your accounts; `MaxTime`, ...).

It also notes queues missing from `servers.yaml` and walltimes that differ from it.

To fill `servers.yaml` for a PBS cluster such as zeus, save the scheduler's view once and draft
the block:

```bash
ssh zeus.technion.ac.il 'qstat -Qf' > qstat_Qf.txt
ssh zeus.technion.ac.il 'pbsnodes -a' > pbsnodes.txt        # or pbsnodes -aSj
rag-drg servers discover-pbs --from-file qstat_Qf.txt --pbsnodes pbsnodes.txt
```

The output is a **draft to review**: walltime from `resources_max.walltime`, cores/memory/GPUs
per node as the minimum over the nodes serving each queue (`queue =` or `resources_available.Qlist`
in `pbsnodes -a`; all nodes when that mapping is unknown, falling back to the queue's per-job
`resources_max.*`), `access:` from the ACLs (deny entries and wildcards such as `*`, which admit
everyone, are left out with a comment; an ACL with nothing left gives no `access:`),
`max_run`/`max_user_run` as notes, route queues skipped, `TODO` where nothing was found.

### ARC settings and submit scripts

`rag-drg servers arc-settings` prints three blocks. `servers` and `global_ess_settings` go into
`~/.arc/settings.py`. `queues` is `arc.ess_queues` when given; otherwise it lists the default
partition first and leaves out restricted (`access:`) and GPU partitions (ARC's queue
troubleshooting may move a job to any listed queue, and ARC never requests GPUs); a comment names
each excluded one. A server with an `arc.runner` block (ARC runs on it) is emitted as
`servers['local']` (no address/key; `excluded_queues` = the partitions not in `queues`;
`cpus`/`memory` of the first queue's node) with `global_ess_settings` and `submit_scripts` keyed
`'local'` - see [`arc-run.md`](arc-run.md). The third block, `submit_scripts`,
goes into `~/.arc/submit.py` and is **required**: ARC reads `submit_scripts[server][job_adapter]`
when it writes a job and fails with `KeyError` for a server that has no entry. Each template is the
same script as `servers submit` (newest non-GPU install of each ESS ARC supports: Gaussian, ORCA,
Molpro, Q-Chem) with ARC's `str.format` placeholders `{name}`, `{queue}`, `{cpus}`, `{memory}`
(MiB, per CPU on Slurm `--mem-per-cpu`, total on PBS `mem=...mb`), `{t_max}` and ARC's fixed file
names (`input.gjf`/`input.log`, `input.in`/`input.log` (ORCA), `input.in`/`output.out` (Q-Chem));
literal shell braces are doubled.

## Live cluster queries (off by default)

`cluster_query` / `rag-drg servers query` run **only** these read-only commands:

| what | Slurm | PBS Pro / OpenPBS (Torque) |
|---|---|---|
| `jobs` | `squeue -u $USER -o ...` | `qstat -u $USER` |
| `job` | `scontrol show job ID` | `qstat -f ID` |
| `history` | `sacct -u $USER -S now-7days -X --format=...` | `qstat -x -u $USER` (not on Torque) |
| `partitions` | `sinfo -s`, `sinfo -o "%P %a %l %D %c %m %G"` | `qstat -Q`, `pbsnodes -aSj` |
| `quota` | every `storage[].quota_command` | same |
| `fairshare` | `sshare -u $USER` | (none; set `commands.fairshare`) |
| `queue_access` | `id -un`, `id -Gn`, `scontrol show partition`, `sacctmgr show assoc user=$USER ...` | `id -un`, `id -Gn`, `qstat -Qf` (not Torque) |

Per-cluster overrides go in `commands:` and must pass the same allowlist (see the spec).
Execution: `ssh -o BatchMode=yes -o ConnectTimeout=10 <ssh_alias | user@host | host> -- <command>`
(argument list, no local shell; each argument is quoted for the remote shell, only `$USER` is
expanded there), or directly without a shell when `scheduler: local` or the host is this machine.
Job ids must match `^\d+(\.\w+)?$`; each command has a 30 s timeout; output is cut at ~8 kB.

**Identity.** The commands run with the SSH identity (keys, agent, `~/.ssh/config`) of whoever
runs the `rag-drg` process. On a shared HTTP server that is the service account: every client
would see *its* jobs and quotas, and the service account would need SSH access to the clusters.
Therefore `conf.d/servers.yaml` ships with

```yaml
cluster_commands:
  enabled: false
```

Enable it only for a **per-user stdio server** (the `rag-drg serve` your own Claude Code starts),
e.g. by adding an untracked `conf.d/zz-local.yaml` with `cluster_commands: {enabled: true}` in your
clone (per-machine overrides, `zz-*.yaml` and `*.local.yaml`, are merged after the shared
`conf.d/` files and win; they are git-ignored, so they never block `git pull`). BatchMode means it never
prompts: your key must already be loaded in an agent or be passphrase-less.
