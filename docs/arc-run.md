# Running ARC on a cluster (runner job, `rag-drg arc compose`)

How the group runs ARC on a PBS cluster such as zeus, and what `rag-drg` generates for it.

## The setup

1. On the login node you write ARC's `input.yml`.
2. A `submit.sh` puts a **runner job** in the group queue (zeus: `alon_q`), pinned to one node
   (zeus: `n170`). The job activates your conda env (`arc_env`) and runs
   `python $ARC_PATH/ARC.py input.yml`.
3. ARC, running on that node, submits every ESS job itself with the local `qsub`. The ESS jobs go
   to the queues in servers.yaml `arc.ess_queues` (on zeus several queues, e.g. `alon_q`,
   `mafat_new_q`, `zeus_long_q`, ...) and may land on any node of those queues.

Because ARC runs **on** the cluster, the cluster is ARC's `'local'` server in `~/.arc/settings.py`
(`cluster_soft: 'PBS'`): ARC then submits with the local `qsub`/`qstat`/`qdel`
(`arc/job/local.py`), writes the jobs under the project directory, and needs no address or SSH key.
Its ESS submit scripts come from `submit_scripts['local'][ess]` in `~/.arc/submit.py`.

## What goes where

| Where | What | Who |
|---|---|---|
| `servers.yaml` `arc.runner` | runner queue, node, default cores/memory/walltime, the pinned node's size | the group (shared) |
| `servers.yaml` `arc.ess_queues` | queues ARC may send ESS jobs to, the first is ARC's default | the group (shared) |
| `--arc-path` / `--conda-env` / `--conda-sh`, or `~/.config/rag-drg/user.yaml` | your ARC clone, env name, conda install | you (per user) |

```yaml
# servers.yaml (group-level facts only)
    arc:
      max_simultaneous_jobs: 20
      ess_queues: [alon_q, mafat_new_q, zeus_long_q, zeus_short_q, zeus_comb_q, alon_comb_q]
      runner:
        queue: alon_q
        host: n170            # ARC always runs (and submits) from this node
        host_cores: 32        # optional: n170's own size; default = alon_q's per-node limits
        host_mem_gb: 192
        cores: 1              # ARC itself is light; in-core xTB/conformers use these cores
        mem_gb: 8
        walltime: "3600:00:00"   # optional; default = the queue's max walltime
        extra_setup: []       # optional group-wide shell lines before ARC starts
```

Validation (`rag-drg lint`): `queue` and every `ess_queues` entry are defined partitions; `host` is a
node name; `cores`/`mem_gb` fit the pinned node (`host_cores`/`host_mem_gb`, only with `host`) or
else the queue's per-node limits; `walltime` <= the queue's max; the scheduler is Slurm/PBS/Torque.
Per-user keys (`arc_path`, `conda_sh`, `conda_env`, `env`, `python`) are rejected in servers.yaml.

```yaml
# ~/.config/rag-drg/user.yaml (yours; $XDG_CONFIG_HOME is honoured)
arc_path: /home/you/Code/ARC
conda_env: arc_env
conda_sh: /home/you/miniforge3/etc/profile.d/conda.sh
```

### Per-user values: resolution order

For each of `arc_path`, `conda_env`, `conda_sh`, first match wins:

1. explicit: `rag-drg arc compose --arc-path/--conda-env/--conda-sh`, or the MCP tool's arguments;
2. `~/.config/rag-drg/user.yaml`;
3. environment: `$ARC_PATH` (then `$arc_path`) for the clone - ARC's docs name no variable for it;
   ARC's code calls the clone directory `ARC_PATH` (`arc/common.py`), so we use that name -
   and `$CONDA_EXE` (then `$CONDA_PREFIX`) for `<conda base>/etc/profile.d/conda.sh`;
4. defaults: env `arc_env`; conda.sh is found **by the job** (`conda info --base` if `conda` is on
   the job's PATH, else `$CONDA_EXE`, else `~/miniforge3`, `~/mambaforge`, `~/miniconda3`,
   `~/anaconda3`); an unknown clone becomes
   `ARC_PATH="${ARC_PATH:?set ARC_PATH to your ARC clone ...}"`, so the job stops at once with that
   message instead of running someone else's ARC.

Steps 2 and 3 read the machine `rag-drg` runs on, so run `rag-drg arc compose` on the cluster's
login node (or pass the values). They are **skipped when `RAG_DRG_SERVER_MODE` is set**: the shared
MCP server knows nobody's paths, so the agent/thin client must pass `arc_path` (and `conda_sh`
unless `conda` is on the job's PATH) to `compose_arc_run`. The notes say where each value came from.

**Why no `#PBS -V`.** `-V` copies the whole qsub-time environment (an activated env, a different
`PATH`/`LD_LIBRARY_PATH`) into a job that runs for weeks and makes the run depend on the shell it
was submitted from. The script instead names the clone and conda.sh, or finds them itself. If you
rely on `$ARC_PATH` at run time, export it in `~/.bashrc` or submit with `qsub -v ARC_PATH submit.sh`.

## The runner script

`render_arc_runner_script(server, input_file="input.yml", job_name=None, *, arc_path, conda_env,
conda_sh, user, groups, **overrides)` (overrides: `queue host cores mem_gb walltime extra_setup`):

```bash
#PBS -N ARC_<project>
#PBS -q alon_q
#PBS -l select=1:ncpus=1:mem=8gb:host=n170      # PBS Pro/OpenPBS: host is a resource of the chunk
#PBS -l walltime=3600:00:00
#PBS -o ARC_<project>.out
#PBS -e ARC_<project>.err
cd "$PBS_O_WORKDIR"
ARC_PATH=/home/you/Code/ARC                     # or ${ARC_PATH:?...}
CONDA_SH=...; source "$CONDA_SH"; conda activate arc_env
python "$ARC_PATH/ARC.py" input.yml &  wait     # trap: TERM (walltime/qdel) stops ARC, prints how to resume
```

Torque pins with `#PBS -l nodes=n170:ppn=1` (+ `#PBS -l mem=`), Slurm with `#SBATCH --nodelist=n170`.
All interpolated values are validated and shell-quoted.

Verified in ARC's code: `ARC.py` takes the input file as its only positional argument
(`parse_command_line_arguments`: `file`, plus `-d/--debug`, `-q/--quiet`); the project directory
is the input file's directory unless the input sets `project_directory`, so submit from the
directory that holds `input.yml`. To resume after the runner ended, run ARC on
`<project>/restart.yml`: ESS jobs ARC already submitted keep running and are picked up.

## How ARC chooses queue, memory, cores and walltime (arc/job/adapter.py, trsh.py, scheduler.py)

* **Queue**: every job is first submitted to the **first** queue of `servers['local']['queues']`
  (`write_submit_script`: `default_queue = next(iter(queues))`). ARC changes the queue only when a
  job was **killed for its walltime** (`ServerTimeLimit`: PBS `job killed: walltime ... exceeded
  limit`, Slurm `DUE TO TIME LIMIT`): the scheduler doubles `max_job_time` and calls
  `troubleshoot_queue()` -> `trsh_job_queue(max_time=24)`, which keeps the queues not yet tried with
  a walltime >= 24 h and picks the one with the **shortest** walltime. When none is left, on PBS it
  asks `groups` + `qstat -q` / `qstat -Qf` for other enabled queues whose `acl_groups` (or name)
  match your groups, minus `excluded_queues`. So the other `ess_queues` are used only after a
  walltime kill; `excluded_queues` (every partition not in `ess_queues`) keeps the discovery off
  queues servers.yaml knows you should not use (queues servers.yaml does not list can still be found).
* **Memory**: `job_memory` (GB, default 14; ARC's GB are GiB) is capped at 95% of
  `servers['local']['memory']` (with a warning in ARC's log) and requested as
  `ceil(GB x 1024 x 1.10)` MiB (`x 1.05` when capped) - total memory on PBS (`mem=...mb`).
* **Cores**: `min(8, servers['local']['cpus'])` (`default_job_settings['job_cpu_cores']`).
* **Walltime**: `max_job_time` hours (default 120; values <= 0 or > 9999 become 120).

The generated `'local'` entry therefore has `queues` = `arc.ess_queues` in order with their max
walltimes (`HH:MM:SS`, as ARC's `convert_to_hours` expects), minus the queues the requesting user
may not use when their identity is known, and `cpus`/`memory` = the first queue's node.

## `rag-drg arc compose`

```bash
rag-drg arc compose input.yml --server zeus [--out-dir DIR] [--json] [--force]
    [--arc-path DIR --conda-env NAME --conda-sh FILE] [--queue Q --host N --cores C --mem GB --time T]
    [--user U --groups G1,G2] [--servers-file F]
```

Writes `submit.sh`, `arc_settings.py` and `arc_submit.py` to the input's directory (or `--out-dir`),
never into `~/.arc` and never over existing files without `--force`; with errors it writes nothing
(unless `--force`). Merge `arc_settings.py` into `~/.arc/settings.py` and `arc_submit.py` into
`~/.arc/submit.py` once, then `qsub submit.sh`. `rag-drg servers arc-settings zeus` prints the same
settings (`--no-local` for ARC on a workstation reaching zeus over SSH; `--local NAME` to choose
when several servers have a runner).

Checks (`findings`: `{severity, code, message, fix}`):

* per ESS queue, whether ARC's request fits (memory request vs memory per node, `max_job_time` vs max
  walltime, cores vs cores per node), respecting `access:` rules when the identity is known:
  **error** `arc-job-fits-no-queue` when it fits no usable queue; **warning**
  `arc-job-misfits-some-queues` naming the queues it does not fit (stronger wording when that is
  the default queue); **warning** `arc-job-memory-capped` when ARC would silently cap `job_memory`;
* `ess_settings`: an ESS routed to `'local'` must be installed on the server (servers.yaml
  `software`) and have a template (Gaussian, ORCA, Molpro, Q-Chem); routing to the server's own name
  is an error (ARC runs there: use `'local'`); other servers.yaml clusters are a warning (SSH from the
  runner node, not in the generated settings); unknown servers/ESS are errors; in-core ESS (xTB,
  PySCF, TorchANI, ...) are noted - they run inside the runner job on its cores;
* the runner job against its queue/node and access; `project:` present; valid YAML;
* the findings of `rag_drg.tools.arc_input.check_arc_input` when that checker is installed.

The notes also say where ARC's scheduler commands live: ARC's defaults call
`/usr/local/bin/qsub`, `/usr/local/bin/qstat`, `/usr/local/bin/qdel`; if `command -v qsub` on the
runner node differs, override `submit_command` (etc.) in `~/.arc/settings.py`. The cluster must
accept `qsub` from compute nodes (ARC stops with "PBS job submission attempted from a compute node"
otherwise).

MCP: `compose_arc_run(input_content, server, input_file="input.yml", arc_path=None, conda_env=None,
conda_sh=None, user=None, groups=None)` returns the same dict as JSON. Pass the user's paths; the
shared server never uses its own.

## For zeus (to fill in)

`servers.yaml` needs, from `rag-drg servers discover-pbs` and the group: the partitions `alon_q`
(max walltime 3600 h, multi-node), `mafat_new_q`, `zeus_long_q`, `zeus_short_q`, `zeus_comb_q`,
`alon_comb_q` with their per-node cores/memory and `access:`; `arc.ess_queues` in the order ARC
should prefer; `arc.runner` with `queue: alon_q`, `host: n170` and n170's `host_cores`/`host_mem_gb`.
