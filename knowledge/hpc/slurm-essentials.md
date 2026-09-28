---
title: Slurm essentials - submitting, querying, quotas
domain: hpc
software: slurm
doc_type: card
status: draft
tags: [sbatch, squeue, sacct, scontrol, sinfo, scancel, sshare, quota, lfs quota, gres, gpu, mem-per-cpu, cpus-per-task, ntasks]
---
# Slurm essentials

## Submit script header

```bash
#!/bin/bash
#SBATCH --job-name=myjob
#SBATCH --partition=<partition>          # see the cluster card in knowledge/hpc/servers/
#SBATCH --nodes=1
#SBATCH --ntasks=1                       # MPI ranks (ORCA/Molpro start their own; keep 1 unless told otherwise)
#SBATCH --cpus-per-task=16               # threads/cores for Gaussian, Psi4, PySCF
#SBATCH --mem=64G                        # per node  (or --mem-per-cpu=4G, not both)
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.out               # %x job name, %j job id
#SBATCH --gres=gpu:1                     # only for GPU jobs
```

* ORCA and Molpro run as MPI programs: request `--ntasks=N --cpus-per-task=1` and let ORCA's
  `%pal nprocs N` / `molpro -n N` start the processes. Gaussian/Psi4/PySCF are shared-memory:
  request `--ntasks=1 --cpus-per-task=N`.
* `$SLURM_CPUS_PER_TASK`, `$SLURM_NTASKS`, `$SLURM_JOB_ID`, `$SLURM_SUBMIT_DIR` are available in the job.
* Keep ESS memory below the Slurm allocation (Gaussian `%mem` ≈ 85-90% of `--mem`; ORCA
  `%maxcore` ≈ 75% of mem-per-core) or the job is OOM-killed.

## Querying

| Want | Command |
|---|---|
| My jobs | `squeue -u $USER` (`--start` for estimated start times) |
| Details of one job | `scontrol show job <id>` |
| Finished job accounting | `sacct -j <id> --format=JobID,JobName,State,Elapsed,MaxRSS,ReqMem,ExitCode` |
| Partitions / nodes | `sinfo -s`; `sinfo -N -l`; GPUs: `sinfo -o "%P %G %D %t"` |
| Cancel | `scancel <id>`; all mine: `scancel -u $USER` |
| Fair-share / priority | `sshare -u $USER`, `sprio -j <id>` |
| Limits of my account | `sacctmgr show assoc user=$USER format=account,partition,maxjobs,maxsubmit,grptres` |

## Quotas and storage

Storage quotas are cluster-specific (see the cluster card). Common commands:

* `quota -s` (NFS home), `lfs quota -hu $USER /path/to/lustre` (Lustre), `df -h $HOME`,
  `du -sh <dir>` for what is using space. Many clusters provide a wrapper (e.g. `myquota`).
* Run ESS scratch on node-local disk (`$TMPDIR`, `/scratch/$USER/$SLURM_JOB_ID`), copy results
  back at the end, and clean up; full scratch or home is a common cause of `Erroneous write`
  (Gaussian) or silent ORCA crashes.
