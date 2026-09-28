---
title: "Atlas (HTCondor) - cluster card"
domain: hpc
software: htcondor
doc_type: card
status: draft
tags: [cluster, server, atlas, htcondor, condor_submit, condor_q, ce_dana, arc, drgscripts, submit.sub]
---
# Atlas cluster card

Facts here come from the group's scripts in
[DanaResearchGroup/DRGScripts](https://github.com/DanaResearchGroup/DRGScripts) (`Servers/Atlas/`,
indexed as source `drgscripts`). Items marked *to fill in* are not known yet; don't guess them.
Atlas is not in `servers.yaml` yet, because the machine sizes and the login host are still missing.

## Access and scheduler

* The scheduler is **HTCondor**, not PBS or Slurm:
  * submit with `condor_submit submit.sub` (alias `sb`);
  * list jobs with `condor_q` (alias `st`, which prints job status, CPUs, memory, name and time).
  * HTCondor has no queues: a submit description asks for `request_cpus` / `request_memory`, and
    HTCondor matches the job to a machine.
* *To fill in:* the login host name, and the machine sizes (cores and memory; `condor_status`).
* Shared group area: `/Local/ce_dana/` holds the software, the group conda
  (`/Local/ce_dana/anaconda3`) and group code clones (`/Local/ce_dana/Code/{ARC,RMG-Py,RMG-database,T3,TCKDB}`).
  Runs and scratch live on `/storage/ce_dana/<user>/`.
* `.bash_aliases` (DRGScripts) initialises that conda, exports `arc_path`, `rmgpy_path`, `t3_path`,
  `tckdb_path` and aliases (`arce`, `rmge`, `t3e`, `arc`, `rmg`, `runs`, `sl` = `screen -ls`).

## Submit descriptions (HTCondor)

The group's pattern (DRGScripts `.arc/submit.py`, `RMG/submit.sub`) is two files:
1. **`submit.sub`**, which sets:
   * `universe = vanilla`, `executable = job.sh`, `should_transfer_files = no`;
   * `log` / `output` / `error` files, `+JobName = "..."`;
   * `request_cpus = N`, `request_memory = <MB>MB`;
   * `getenv = True`, and `environment = "VAR=value ..."` for scratch paths;
   * and ends with `queue`.
2. **`job.sh`**, the script that runs the program.

The job runs in the submit directory (no file transfer). Scratch goes in
`/storage/ce_dana/<user>/scratch/<program>/<job name>/` and the job script deletes it at the end.

## Software (from DRGScripts `.arc/submit.py`)

| Program | Path / setup |
|---|---|
| Gaussian 09 | `g09root=/Local/ce_dana`; `source /Local/ce_dana/g09/bsd/g09.login` (csh); `/Local/ce_dana/g09/g09 < input.gjf > output.out`; `GAUSS_SCRDIR` under `/storage/ce_dana/<user>/scratch/g09/` |
| ORCA 5.0.4 | `/Local/ce_dana/orca_5_0_4_linux_x86-64_shared_openmpi411/orca` with OpenMPI 4.1.1 in `/Local/ce_dana/openmpi-4.1.1` (`bin` on PATH, `lib` on LD_LIBRARY_PATH) |
| Molpro | the job runs `/Local/ce_dana/molpro-mpp-2022.2.3/bin/molpro -n N -t 1 -d $MOLPRO_SCRDIR`, but the submit file puts `molpro-mpp-2021.2.1/bin` on PATH. *To fill in:* which version is meant |
| Q-Chem | `QC=/Local/ce_dana/Q-Chem`; `/Local/ce_dana/Q-Chem/bin/qchem -nt N input.in output.out`; `QCSCRATCH` under `/storage/ce_dana/<user>/scratch/qchem/`. *To fill in:* the version |

* `incore_commands` in the same file calls `g16`. *To fill in:* whether Gaussian 16 is installed on Atlas.

## ARC on Atlas (DRGScripts `.arc/settings.py`)

* `servers['local']`: `cluster_soft` HTCondor, `path` `/storage/ce_dana/`, `cpus` 8, `memory` 256.
* `global_ess_settings`: gaussian, orca and molpro all go to `local`.
* `default_job_settings`: `job_total_memory_gb` 6, `job_cpu_cores` 8.
* **ARC runs on the head node, inside a `screen` session, not as a batch job** (group workflow,
  confirmed 2026-09-28). This is the opposite of zeus, where ARC runs as a PBS job on n170. Only
  the ESS jobs that ARC spawns go through HTCondor.

### Running and monitoring ARC on Atlas

1. On the head node, start a named screen: `screen -S <run name>`.
2. Inside it: `arce` (= `conda activate arc_env`), then `cd /storage/ce_dana/<user>/runs/<run>`
   (the `runs` alias goes to `/storage/ce_dana/<user>/runs`), then `arc` (=
   `python $arc_path/ARC.py input.yml`).
3. Detach with `Ctrl-a d`; ARC keeps running on the head node.
4. **Is ARC itself still running?** Check the screen: `screen -ls` (alias `sl`) lists the
   sessions, and `screen -r <run name>` reattaches to see ARC's output.
5. **ARC's ESS jobs:** they are HTCondor jobs, so use `condor_q` (alias `st` shows status,
   CPUs, memory, job name and time).
6. Stop a run: `screen_quit <run name>` (a function in `.bash_aliases`), or `Ctrl-c` inside the
   screen. Remove the ESS jobs ARC already submitted with `condor_rm`.

Don't write a batch "runner" submit file for ARC on Atlas. `rag-drg arc compose` generates a
zeus-style PBS runner and does not apply here.
* `pipe_submit` in the same file is a Slurm (`#SBATCH -p normal`) template left over from another
  cluster; it does not apply to Atlas's HTCondor.
