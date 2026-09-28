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
   GPUs per node and GPU type, which one is the default. Get them from `sinfo -o "%P %l %c %m %G"`
   (Slurm) or `qstat -Qf` / `pbsnodes -aSj` (PBS), or the cluster documentation.
3. **Every ESS install**: absolute path of the executable, the environment lines it needs
   (e.g. ORCA's OpenMPI `PATH`/`LD_LIBRARY_PATH`, `g16root` + `source $g16root/g16/bsd/g16.profile`,
   `QC`/`QCAUX` + `source $QC/qcenv.sh`, the Python env of Psi4/PySCF), MPI vs threads, and the
   partitions it may run on (e.g. the Gaussian GPU build only on the GPU partition).
   Keys: `orca-5`, `orca-6`, `gaussian-09`, `gaussian-16`, `gaussian-16-gpu`, `qchem-6.1`, `psi4`,
   `molpro-2024`, `molpro-2026`, `pyscf` (any `<ess>-<suffix>` works).
4. **Scratch**: where per-job scratch goes (node-local disk or `$TMPDIR`, or shared scratch).
5. **Storage**: home/project/group areas with quota, backup status, and the command that shows
   usage (`quota -s`, `lfs quota -h -u $USER /lustre`, `df -h /data/...`, `mmlsquota`, ...).
6. Optional: ARC `path` (remote base dir, usually `/home`) and `max_simultaneous_jobs`;
   `commands:` overrides if a cluster needs different read-only query commands.

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
| `servers arc-settings [NAME ...]` | Python `servers = {...}` and a suggested `global_ess_settings` for `~/.arc/settings.py` |
| `servers submit SERVER SOFTWARE INPUT [--cores N --mem GB --time T --partition P --gpus G --job-name J -o FILE]` | submit script on stdout (or `-o`), notes (input lines, warnings) on stderr |
| `servers check SERVER [PARTITION] --cores N --mem GB --time T [--gpus G --software KEY]` | check a request against the limits (exit 1 on errors) |
| `servers query SERVER {jobs,job,history,partitions,quota,fairshare} [JOB_ID]` | read-only live query (disabled by default, see below) |

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
* **Per-job scratch** `<scratch.path>/<job id>` (default `${TMPDIR:-/tmp}/<job id>`), removed at
  the end: ORCA copies the input there, runs there and copies `.gbw/.hess/.xyz/...` back;
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
| `render_submit_script(server, software, input_file, job_name, cores, mem_gb, walltime, partition, gpus)` | script + input lines, or the limit violations |
| `check_resources(server, partition, cores, mem_gb, walltime, gpus, software)` | JSON list of `{severity, message}` (`[]` = fits) |
| `cluster_query(server, what, job_id)` | read-only live query; only registered when enabled and the server is not `--readonly` |

Python (for other plugins, e.g. an input checker):

```python
from rag_drg.tools.servers import load_servers, check_resources, render_submit_script
servers = load_servers(cfg)                     # {} if there is no servers.yaml
problems = check_resources(servers["zeus"], "cpu", cores=16, mem_gb=64, walltime="24:00:00")
problems = check_resources("zeus", None, 16, 64, "24:00:00", software="orca-6", cfg=cfg)
```

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
clone (files in `conf.d/` are merged alphabetically, later ones win). BatchMode means it never
prompts: your key must already be loaded in an agent or be passphrase-less.
