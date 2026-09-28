---
title: "zeus (Technion) - cluster card"
domain: hpc
software: pbs
doc_type: card
status: draft
tags: [cluster, server, zeus, technion, pbs, qsub, queues, alon_q, n170, arc, arc_env]
---
# zeus cluster card

Facts here come from the group. Items marked *to fill in* are not known yet; don't guess them.
The machine-readable version (queue limits, install paths, access rules) belongs in
`servers.yaml`, and `rag-drg arc compose` / `render_submit_script` read it from there.

## Access

* Login host: `zeus.technion.ac.il`. The scheduler is PBS (`qsub`, `qstat`, `qdel`, `pbsnodes`).
* Programs are called by absolute path. The group does not use environment modules; this is not
  yet checked on zeus with `module avail`.

## Running ARC on zeus

ARC does not run on the login node itself; it runs as a batch job:

1. On the login node, write ARC's `input.yml` (check it with `check_arc_input` / `rag-drg arc check`).
2. Write `submit.sh` with PBS directives for queue `alon_q`, pinned to node `n170`
   (`#PBS -q alon_q`, `#PBS -l select=1:ncpus=...:host=n170`). Let `rag-drg arc compose` generate it.
3. The job activates the conda env `arc_env` and runs `python $ARC_PATH/ARC.py input.yml`.
4. `qsub submit.sh` from the login node.

ARC runs only on `n170`, because it submits its own ESS jobs with `qsub` from there.
Submitting with `qsub` from n170 works (confirmed by the group, 2026-09-28).

* The ARC clone path and the conda install (`conda.sh`) are per user. Set them in
  `~/.config/rag-drg/user.yaml` or with `--arc-path` / `--conda-sh`, not in the shared `servers.yaml`.
* In `~/.arc/settings.py`, zeus is ARC's `local` server (ARC runs on it).
* *To fill in:* the path of `qsub` on n170 (`command -v qsub`). ARC's defaults assume
  `/usr/local/bin/qsub`, `/usr/local/bin/qstat` and `/usr/local/bin/qdel`. If the path is
  different, override `submit_command` etc. in `~/.arc/settings.py`.
* *To fill in:* n170's cores and memory (`pbsnodes n170`).

## Queues

ESS jobs, whether submitted by hand or by ARC, can go to any of these queues:
`alon_q`, `mafat_new_q`, `zeus_long_q`, `zeus_short_q`, `zeus_comb_q`, `alon_comb_q`.

| Queue | Max walltime | Size | Notes |
|---|---|---|---|
| `alon_q` | 3600 h | about 1500 CPUs in total, across several nodes | ARC's runner job goes here, pinned to `n170`; ESS jobs can run on any of its nodes |
| `mafat_new_q` | *to fill in* | *to fill in* | |
| `zeus_long_q` | *to fill in* | *to fill in* | |
| `zeus_short_q` | *to fill in* | *to fill in* | |
| `zeus_comb_q` | *to fill in* | *to fill in* | |
| `alon_comb_q` | *to fill in* | *to fill in* | |

Get the missing values with `qstat -Qf` and `pbsnodes -a` (or `rag-drg servers discover-pbs`).

## Software installation paths

*To fill in* (the absolute paths of ORCA, Gaussian, Q-Chem, Psi4, Molpro and PySCF on zeus).
See `_TEMPLATE.md` for the table layout.
