---
title: "zeus (Technion) - cluster card"
domain: hpc
software: pbs
doc_type: card
status: draft
tags: [cluster, server, zeus, technion, pbs, qsub, queues, alon_q, alon_comb_q, mafat_new_q, zeus_long_q, zeus_short_q, zeus_combined_q, zeus_comb_short, n170, gd004, grinberg-dana_prj, arc, arc_env, max_queued, walltime]
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
* `qsub` is `/usr/local/bin/qsub`, which is ARC's default, so `submit_command` needs no override.
  ARC also assumes `/usr/local/bin/qstat` and `/usr/local/bin/qdel`.

## Node n170 (`pbsnodes n170`, 2026-09-28)

| Field | Value |
|---|---|
| PBS vnode name | `gd004` (`resources_available.host = n170`, so `select=...:host=n170` matches it) |
| Cores | 384 (`resources_available.ncpus`) |
| Memory | 1584807020 kB, about 1511 GiB (`resources_available.mem`) |
| GPUs | none listed |
| Queues it serves | `alon_q`, `zeus_combined_q`, `zeus_comb_short` (`resources_available.qlist`) |

ARC's own runner job needs few cores, but other `alon_q`, `zeus_combined_q` and `zeus_comb_short`
jobs can share n170 with it.

## Queues

Our group's Unix group on zeus is `grinberg-dana_prj`. A queue's `acl_groups` controls who may
submit to it. Queues without an ACL are open to every zeus user.

These are the queues the group uses for ESS jobs. The values come from `qstat -Qf` (2026-09-28).
"Default walltime" is the value PBS uses when a job doesn't ask for one; when a queue has no
maximum set, PBS enforces no walltime limit.

| Queue | Max walltime | Default walltime | Max cores (whole queue) | Per-user limits | Who may submit |
|---|---|---|---|---|---|
| `alon_q` | none set | 3600 h | 1536 | none set | `grinberg-dana_prj`, `halupovich_prj` |
| `alon_comb_q` | none set | 24 h | 3072 | 600 cores running | `grinberg-dana_prj`, `halupovich_prj` |
| `mafat_new_q` | none set | 3600 h | 4352 | 40 jobs queued | about 20 project groups, including `grinberg-dana_prj` |
| `zeus_long_q` | 336 h | none set | 1120 running (all users) | **10 jobs queued, 20 running** | everyone |
| `zeus_short_q` | 3 h | 3 h | 1360 running (all users) | 600 jobs, 600 cores running | everyone |
| `zeus_combined_q` | 24 h | 24 h | none set | 400 jobs queued, 600 jobs or 600 cores running | everyone |
| `zeus_comb_short` | 3 h | 3 h | none set | 1000 jobs, 600 cores running | everyone |

Rules and gotchas:

* **`zeus_long_q` accepts only 10 queued jobs per user.** ARC can submit dozens of conformer and
  TS jobs at once, so sending ARC's ESS jobs to `zeus_long_q` quickly hits this limit. Prefer
  `alon_q` or `mafat_new_q` for ARC.
* Queue priority: `alon_q` is 148. `zeus_long_q`, `zeus_short_q` and `alon_comb_q` are 100.
  `zeus_combined_q` and `zeus_comb_short` are 80. `mafat_new_q` has none set.
* `zeus_long_q`'s per-user core limit is written `[u:PBS_PBS_GENERIC=600]` in its configuration,
  not the usual `PBS_GENERIC`. It may not be enforced; ask the admins before relying on it.
* There is no queue called `zeus_comb_q`: the combined queues are `zeus_combined_q` (24 h) and
  `zeus_comb_short` (3 h).
* *To fill in:* the cores and memory per node for each queue except n170's. Get them with
  `pbsnodes -a` (see `docs/arc-run.md`).

## Software installation paths

*To fill in* (the absolute paths of ORCA, Gaussian, Q-Chem, Psi4, Molpro and PySCF on zeus).
See `_TEMPLATE.md` for the table layout.
