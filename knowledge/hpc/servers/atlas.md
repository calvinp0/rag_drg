---
title: "Atlas (HTCondor) - cluster card"
domain: hpc
software: htcondor
doc_type: reference
status: draft
tags: [cluster, server, atlas, htcondor, condor_submit, condor_q, tech-ui02, ce_dana, storage, screen, arc, drgscripts, submit.sub, held, wastingmemory]
---
# Atlas cluster card

Sources:
* the group's Atlas wiki page (2026-09-28);
* `condor_status` / `condor_config_val` / `ls /Local/ce_dana` output from `tech-ui02` (2026-09-28);
* the group's scripts in [DanaResearchGroup/DRGScripts](https://github.com/DanaResearchGroup/DRGScripts)
  (`Servers/Atlas/`, indexed as source `drgscripts`).

Items marked *to fill in* are not known yet; don't guess them. Atlas is part of the Technion's share
of the ATLAS experiment computing, and **its admin is very strict**: follow the rules below exactly.
The machine-readable version is the `atlas` entry in `servers.yaml` (card: `generated/atlas.md`).

## Rules (from the group wiki)

* **Do not install any software, and do not create conda environments.** The only exception is
  cloning git repositories you actively develop (e.g. to run a specific branch), into `~/Code`.
  If in doubt, ask an experienced group member first.
* **Run jobs only from Storage** (`/storage/ce_dana/<user>/runs/...`), never from Home.
* **No compute- or memory-intensive work on the login nodes.** Use
  `condor_submit -interactive job.sub` for interactive work, and close that job when done. ARC
  itself is the group's accepted exception (see *ARC on Atlas*).
* Any `job.sh` an HTCondor submit file runs must be executable: `chmod u+x job.sh`.

## Access and file systems

* Login: `ssh <user>@tech-ui02.hep.technion.ac.il`. The wiki notes `tech-ui02` is usually the less
  busy login node. The first login uses a temporary LDAP password, which must be changed with
  `passwd` within a week. Set up SSH-key login (`ssh-keygen`, `ssh-copy-id`).

| Area | Path | Quota | Backed up | Use |
|---|---|---|---|---|
| Home | `/srv01/technion/<user>` | 10 GB (hard 12 GB), ~625k files | yes | `~/Code` git clones, `~/.arc`, dotfiles |
| Storage | `/storage/ce_dana/<user>` | group `ce_dana`: 5 TB; **per user: ~149,000 files (hard 150,000)** | **no** | runs (`runs/`), scratch (`scratch/`) |
| Local | `/Local/ce_dana` | read-only for users | - | the group's software, conda and code (installed by the admin / one designated member) |

* The login message shows your quotas. The **file-count limit on Storage** is the one people hit:
  ARC and Gaussian runs create many files, so clean up old runs and scratch.
* Storage is not backed up: copy important results elsewhere.

## Scheduler: HTCondor

* HTCondor is a high-throughput system. There are no queues or partitions: a submit description
  asks for `request_cpus` / `request_memory`, and HTCondor matches the job to a machine slot
  (partitionable slots).
* Commands:
  * `condor_submit submit.sub` (alias `sb`);
  * `condor_q` (alias `st`);
  * `condor_q -better-analyze <job id>` to see why a job doesn't start;
  * `condor_rm <job id>` to remove a job.
* **Machines** (`condor_status`, 2026-09-28): about 200 worker nodes `tech-wnNNN`.

  | Nodes | Cores | Memory |
  |---|---|---|
  | most | 58 | 256 GB |
  | wn049-wn072 | 76 | 256 GB |
  | wn077-wn100, wn161-wn176 | 42 | 256 GB |
  | wn210 | 72 | 251 GB |
  | wn249 | 154 | 256 GB |
  | wn188, wn217-wn220 | 58 | **1 TB** |

  A job that fits on every machine: at most 42 cores and about 250 GB.

* **Jobs are held (`condor_q` status H) automatically** (`SYSTEM_PERIODIC_HOLD`;
  `condor_config_val`, 2026-09-28) when:
  * `MEMORY_EXCEEDED`: the job's resident memory exceeds `request_memory`;
  * `TIME_EXCEEDED`: it has been running more than **72 h** (120 h for jobs of users with
    `HiMemUser` set that request more than 60 GB);
  * `WastingMemory`: the job requests **more than 8 GB** and, after its first hour, uses
    **less than 20%** of that. Requests of 8 GB or less are never held for this.

  **Held jobs are removed after 24 h** (`SYSTEM_PERIODIC_REMOVE`). So request memory close to what
  the job really uses. With too little the job is held for exceeding it; with far too much (over
  8 GB and five times the real use) it is held for wasting memory. Either way it is deleted a day
  later. This matters for ARC's `job_memory`, and a job must finish within 72 h.

### Submit descriptions

The group's pattern (DRGScripts `.arc/submit.py`, `RMG/submit.sub`) is two files:
1. **`submit.sub`**, which sets:
   * `universe = vanilla`, `executable = job.sh`, `should_transfer_files = no`;
   * `log` / `output` / `error` files, `+JobName = "..."`;
   * `request_cpus = N`, `request_memory = <MB>MB`;
   * `getenv = True`, and `environment = "VAR=value ..."` for scratch paths;
   * and ends with `queue`.
2. **`job.sh`**, the script that runs the program. It must be executable.

The job runs in the submit directory (no file transfer). Scratch goes in
`/storage/ce_dana/<user>/scratch/<program>/<job name>/`; the wiki says to create
`scratch/g09` and `scratch/orca` there before running ARC with Gaussian. The job script deletes
the scratch directory at the end.

## Software (`ls /Local/ce_dana`, 2026-09-28)

| Program | Install | Used by the group's scripts |
|---|---|---|
| Gaussian 09 | `/Local/ce_dana/g09` (readable by group `ce_dana` only): `g09root=/Local/ce_dana`, `source /Local/ce_dana/g09/bsd/g09.login` (csh), `/Local/ce_dana/g09/g09` | yes (ARC) |
| Gaussian 16 | **not in `/Local/ce_dana`** (no `g16` directory). DRGScripts' `incore_commands` calls `g16`; *to fill in:* whether `command -v g16` finds one elsewhere | - |
| ORCA 5.0.4 | `/Local/ce_dana/orca_5_0_4_linux_x86-64_shared_openmpi411/orca`, OpenMPI 4.1.1 in `/Local/ce_dana/openmpi-4.1.1` | yes (ARC) |
| ORCA 4.0.1.2 | `/Local/ce_dana/orca_4_0_1_2_linux_x86-64_openmpi202`, OpenMPI 2.0.2 in `/Local/ce_dana/openmpi-2.0.2` | no |
| Molpro | `/Local/ce_dana/molpro-mpp-2020.2.1`, `-2021.2.1`, `-2022.2.3`. **Use the latest, 2022.2.3** (group rule) | ARC's job runs `molpro-mpp-2022.2.3/bin/molpro -n N -t 1 -d $MOLPRO_SCRDIR` (its submit file still puts 2021.2.1 on PATH) |
| Q-Chem 6.1.1 | `/Local/ce_dana/Q-Chem` (`version.txt`): `QC=/Local/ce_dana/Q-Chem`, `source $QC/qcenv.sh`, `bin/qchem -nt N` | ARC template exists |
| Also | CFOUR v2.00beta (serial), CREST 2.12 and 3.0.2, Julia 1.8.0 (RMS), NVIDIA HPC SDK, group conda `/Local/ce_dana/anaconda3` | |

Psi4 and PySCF are not in `/Local/ce_dana`.

## Environment (`.bash_aliases`, from the wiki / DRGScripts)

* In `~/.bashrc`:
  * comment out `[ -z "$PS1" ] && return`, so the file also works inside jobs;
  * enable the `~/.bash_aliases` block.
* `.bash_aliases`:
  * initialises the group conda;
  * exports `arc_path`, `rmgpy_path`, `rmgdb_path`, `t3_path`, `tckdb_path`, which point at the
    shared clones in `/Local/ce_dana/Code/`. Only developers repoint them to `~/Code`.
  * sets up Julia for RMS.
  * defines aliases: `arce` / `rmge` / `t3e` / `tcke` / `rmse` (activate the conda envs), `arc`,
    `rmg`, `arkane`, `t3`, `sb`, `st`, `runs`.
* The group maintains the shared ARC and RMG clones and `arc_env`. Ask Alon for updates.

## ARC on Atlas

* **ARC runs on the head (login) node, inside a `screen` session, not as a batch job** (wiki;
  confirmed by the group). This is the opposite of zeus, where ARC runs as a PBS job on n170.
  Only the ESS jobs that ARC spawns go through HTCondor.
* `~/.arc/settings.py` (wiki):
  * `servers['local'] = {'path': '/storage/ce_dana/', 'cluster_soft': 'HTCondor', 'un': '<user>',
    'cpus': 8, 'memory': 40}`;
  * `global_ess_settings`: gaussian, orca and molpro all go to `local`;
  * `supported_ess = ['gaussian', 'molpro', 'orca']`.

  DRGScripts' copy has `'memory': 256` and `default_job_settings = {'job_total_memory_gb': 6,
  'job_cpu_cores': 8}`. *To fill in:* which of the two is current.
* `~/.arc/submit.py`: the HTCondor templates from DRGScripts (`Servers/Atlas/.arc/submit.py`).
  Its `pipe_submit` is a Slurm template left over from another cluster and does not apply here.

### Running and monitoring ARC on Atlas

1. Make a project folder on Storage, e.g. `/storage/ce_dana/<user>/runs/ARC/<run>/`, and put
   `input.yml` there.
2. Start a named screen on the head node: `screen -S <run>`.
3. Inside it: `arce`, then `arc`. `arc` runs `python $arc_path/ARC.py input.yml` and tees
   `stdout.log` / `stderr.log`.
4. Detach with `Ctrl-a d`; ARC keeps running on the head node.
5. **Is ARC itself still running?** `screen -ls` lists the sessions, and `screen -r <run>`
   reattaches to show ARC's output.
6. **ARC's ESS jobs** are HTCondor jobs: check them with `condor_q` / `st`, and with
   `condor_q -better-analyze <id>` for held or idle ones.
7. Stop a run: `screen -X -S <run> quit`, then `condor_rm` any ESS jobs it left behind.

Don't write a batch "runner" submit file for ARC on Atlas. `rag-drg arc compose` generates a
zeus-style PBS runner and does not apply here.

## What rag-drg can do for Atlas

* `servers.yaml` has `atlas`:
  * one pseudo-partition, `vanilla`: 72 h, 42 cores and 251 GB (the smallest machine);
  * software: G09, ORCA 5.0.4, Molpro 2022.2.3, Q-Chem 6.1.1;
  * scratch in `/storage/ce_dana/$USER/scratch`.
* `check_resources` works (e.g. a 100 h request is refused).
* **Submit-file generation for HTCondor is not implemented**: `render_submit_script` and
  `compose_ess_job --server atlas` refuse with a clear error. Write the group's `submit.sub` +
  `job.sh` pair (DRGScripts `.arc/submit.py` templates), or compose only the input (no `server`).
