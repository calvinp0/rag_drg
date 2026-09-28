---
title: "<Cluster name> - cluster card (TEMPLATE: copy to <cluster>.md and fill in)"
domain: hpc
software: slurm          # or pbs / htcondor / sge
doc_type: scaffold
status: draft
tags: [cluster, server, queues, partitions, install paths, absolute paths, scratch, quota]
---
# <Cluster name> cluster card

Copy this file to `knowledge/hpc/servers/<cluster>.md`, fill in every `<...>`, and open a PR.
Agents read this card before writing submit scripts or running commands on the cluster.

## Access

* Login host: `<host>`; access from outside via `<VPN / jump host>`.
* ARC `servers` entry (for `~/.arc/settings.py`):
  ```python
  '<cluster>': {'cluster_soft': '<Slurm|PBS|HTCondor>', 'address': '<host>', 'un': '<user>',
                'key': '~/.ssh/<key>', 'path': '<remote base path>', 'cpus': <cores/node>, 'memory': <GB/node>},
  ```

## Partitions / queues

| Name | Max walltime | Cores/node | Mem/node | GPUs | Use for |
|---|---|---|---|---|---|
| `<partition>` | `<HH:MM:SS>` | `<n>` | `<GB>` | `<type x n / none>` | `<...>` |

## Software installation paths

We call every program by absolute path; environment modules are not used (check `module avail`
once per cluster and note the result here). These values replace the `<abs path ...>`
placeholders in `knowledge/hpc/templates/`.

| ESS | Install directory | Executable | Environment setup lines |
|---|---|---|---|
| ORCA 5.0.x | `<...>/orca_5_0_4` | `<...>/orca_5_0_4/orca` | OpenMPI: `<...>/openmpi-4.1.x` (PATH + LD_LIBRARY_PATH) |
| ORCA 6.0.x | `<...>/orca_6_0_x` | `<...>/orca_6_0_x/orca` | OpenMPI: `<...>` |
| Gaussian 16 (CPU) | `g16root=<...>` | `$g16root/g16/g16` | `source $g16root/g16/bsd/g16.profile`; `GAUSS_SCRDIR=<...>` |
| Gaussian 16 (GPU) | `g16root=<...>` | `$g16root/g16/g16` | GPU partition `<...>` |
| Gaussian 09 | `g09root=<...>` | `$g09root/g09/g09` | `source $g09root/g09/bsd/g09.profile` |
| Q-Chem 6.1 | `QC=<...>` | `qchem` (after `source $QC/qcenv.sh`) | `QCAUX=<...>`, `QCSCRATCH=<...>` |
| Psi4 | `<...>/envs/<env>` | `<...>/envs/<env>/bin/psi4` | `PSI_SCRATCH=<...>` |
| Molpro 2024 | `<...>` | `<...>/bin/molpro` | |
| Molpro 2026 | `<...>` | `<...>/bin/molpro` | |
| PySCF | `<...>/envs/<env>` | `<...>/envs/<env>/bin/python` | `PYSCF_TMPDIR=<...>` |

## Storage and quotas

| Area | Path | Quota | Backed up | Check with |
|---|---|---|---|---|
| Home | `<...>` | `<...>` | `<yes/no>` | `<quota -s / myquota>` |
| Project/group | `<...>` | `<...>` | | `<...>` |
| Scratch (node-local) | `<$TMPDIR ...>` | | no | `df -h <...>` |

## House rules / gotchas

* `<max jobs per user, fair-share, which partition to avoid, etc.>`
