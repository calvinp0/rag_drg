---
title: "<Cluster name> - cluster card (TEMPLATE: copy to <cluster>.md and fill in)"
domain: hpc
software: slurm          # or pbs / htcondor / sge
doc_type: scaffold
status: draft
tags: [cluster, server, queues, partitions, modules, scratch, quota]
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

## Software modules

| ESS | Load command | Executable | Notes |
|---|---|---|---|
| ORCA 5 | `module load <...>` | `$(which orca)` | OpenMPI module `<...>` |
| ORCA 6 | `module load <...>` | | |
| Gaussian 16 (CPU) | `module load <...>` | `g16` | `GAUSS_SCRDIR=<...>` |
| Gaussian 16 (GPU) | `module load <...>` | `g16` | GPU partition `<...>` |
| Gaussian 09 | `module load <...>` | `g09` | |
| Psi4 | `conda activate <env>` | `psi4` | |
| Molpro 2024 / 2026 | `module load <...>` | `molpro` | |
| PySCF | `conda activate <env>` | `python` | |

## Storage and quotas

| Area | Path | Quota | Backed up | Check with |
|---|---|---|---|---|
| Home | `<...>` | `<...>` | `<yes/no>` | `<quota -s / myquota>` |
| Project/group | `<...>` | `<...>` | | `<...>` |
| Scratch (node-local) | `<$TMPDIR ...>` | | no | `df -h <...>` |

## House rules / gotchas

* `<max jobs per user, fair-share, which partition to avoid, etc.>`
