---
title: PBS (PBS Pro / OpenPBS / Torque) essentials - submitting, querying
domain: hpc
software: pbs
doc_type: card
status: draft
tags: [qsub, qstat, qdel, pbsnodes, select, ncpus, mem, walltime, torque, ppn]
---
# PBS essentials

## Submit script header (PBS Pro / OpenPBS)

```bash
#!/bin/bash
#PBS -N myjob
#PBS -q <queue>                          # see the cluster card in knowledge/hpc/servers/
#PBS -l select=1:ncpus=16:mpiprocs=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe                               # merge stdout/stderr
#PBS -o myjob.log

cd "$PBS_O_WORKDIR"
```

* **Torque** (older) uses `#PBS -l nodes=1:ppn=16` and `#PBS -l mem=64gb` instead of `select=`.
  Check which flavour the cluster runs (`qstat --version`) before writing a header.
* `$PBS_O_WORKDIR` (submit dir), `$PBS_JOBID`, `$NCPUS`, `$PBS_NODEFILE` are available.
* Jobs start in `$HOME`, not the submit directory; always `cd "$PBS_O_WORKDIR"`.

## Querying

| Want | Command |
|---|---|
| My jobs | `qstat -u $USER` (PBS Pro: `qstat -wu $USER` for wide output) |
| Details | `qstat -f <id>` |
| Finished jobs (PBS Pro) | `qstat -xf <id>` |
| Queues | `qstat -Q`, `qstat -Qf <queue>` |
| Nodes | `pbsnodes -a` (PBS Pro: `pbsnodes -aSj`) |
| Cancel | `qdel <id>` |
