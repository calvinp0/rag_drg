---
title: "zeus (Technion) - cluster card"
domain: hpc
software: pbs
doc_type: reference
status: draft
tags: [cluster, server, zeus, gpu, gpucpu, cuda_visible_devices, drgscripts, setup.sh, opt/pbs, technion, pbs, qsub, queues, alon_q, alon_comb_q, mafat_new_q, zeus_long_q, zeus_short_q, zeus_combined_q, n170, gd004, grinberg-dana_prj, arc, arc_env, max_queued, walltime]
---
# zeus cluster card

Facts here come from the group and from `qstat -Qf` / `pbsnodes -a` (2026-09-28). Items marked
*to fill in* are not known yet; don't guess them. The machine-readable version is the `zeus` entry
in `servers.yaml` (card: `generated/zeus.md`); `rag-drg arc compose`, `render_submit_script` and
`check_resources` read it from there.

## Access

* Login host: `zeus.technion.ac.il`. The scheduler is PBS (`qsub`, `qstat`, `qdel`, `pbsnodes`).
* Programs are called by absolute path. zeus does have environment modules (Lmod, `module avail`:
  `/usr/local/modules`, with OpenMPI, CUDA, GCC and Intel oneAPI). The group's scripts don't need them.
* Storage: check your quota with `quota -vs`.
* Scratch for ESS jobs goes under `/gtmp/` (group rule). Generated scripts use `/gtmp/$USER/<job id>`
  and remove it when the job ends. It is not recorded yet whether `/gtmp` is shared or local to each node.
* Conda is available to every user, but conda envs are per user: list yours with `conda env list`
  on the login node.

## Running ARC on zeus

ARC does not run on the login node itself; it runs as a batch job:

1. On the login node, write ARC's `input.yml` (check it with `check_arc_input` / `rag-drg arc check`).
2. Write `submit.sh` with PBS directives for queue `alon_q`, pinned to node `n170`
   (`#PBS -q alon_q`, `#PBS -l select=1:ncpus=...:host=n170`). Let `rag-drg arc compose` generate it.
3. The job activates the conda env `arc_env` and runs `python $ARC_PATH/ARC.py input.yml`.
4. `qsub submit.sh` from the login node.

ARC runs only on `n170`, because it submits its own ESS jobs with `qsub` from there.
Submitting with `qsub` from n170 works (confirmed by the group, 2026-09-28).

**ARC sends its ESS jobs to `alon_q` only** (group rule). In `~/.arc/settings.py` the `local`
server's `queues` is `{'alon_q': '3600:00:00'}`, and every other queue is in `excluded_queues`.
The other CPU queues below are for ESS jobs submitted by hand.

* The ARC clone path and the conda install (`conda.sh`) are per user. Set them in
  `~/.config/rag-drg/user.yaml` or with `--arc-path` / `--conda-sh`, not in the shared `servers.yaml`.
* In `~/.arc/settings.py`, zeus is ARC's `local` server (ARC runs on it).
* PBS commands: the group's `~/.arc/settings.py` calls `/opt/pbs/bin/qsub`, `/opt/pbs/bin/qstat` and
  `/opt/pbs/bin/qdel`. `command -v qsub` on the login node prints `/usr/local/bin/qsub`, which is
  ARC's default.

### The group's working ARC setup (DRGScripts)

DRGScripts is a set of group templates and can go stale. Where it disagrees with what is on the
cluster, the cluster wins.

The group keeps its working zeus scripts in
[DanaResearchGroup/DRGScripts](https://github.com/DanaResearchGroup/DRGScripts), under
`Servers/Zeus/`. That repository is indexed too (source `drgscripts`); copy from it rather than
writing these files from scratch.

* `ARC/submit.sh` (the runner):
  * `#!/bin/bash -l`, `#PBS -q alon_q`, `#PBS -l select=1:ncpus=1:host=n170`, walltime 72 h;
  * runs `conda activate arc_env`, then `python $arc_path/ARC.py input.yml`.
* `.bashrc` exports `arc_path="$HOME/Code/ARC/"`, with RMG-Py and T3 alongside. It also sources
  `/usr/local/g09/setup.sh` and defines aliases (`arce`, `arc`, `sb='qsub submit.sh'`,
  `st='qstat -u $USER'`).
* `ARC/.arc/settings.py`:
  * `servers['local']` is PBS with `cpus` 16 and `memory` 160, so ARC's jobs stay well below the
    node size;
  * `global_ess_settings`: gaussian, orca and molpro all go to `local`;
  * `default_job_settings`: `job_total_memory_gb` 32, `job_cpu_cores` 16;
  * scheduler commands in `/opt/pbs/bin`.
* `ARC/.arc/submit.py` has one PBS template per ESS:
  * common to all: `#!/bin/bash -l`, `. ~/.bashrc`, `#PBS -q alon_q`,
    `select=1:ncpus={cpus}:mem={memory}mb:mpiprocs={cpus}`, `-o out.txt -e err.txt`;
  * each job runs in `/gtmp/{un}/scratch/<program>/$PBS_JOBID`, and a `TERM` trap copies results
    back and deletes the scratch directory.
  * Gaussian: `source /usr/local/g16-gpu/g16/setup.sh` ("faster gaussian 16 installation
    (rev C.02)"), then `g16 < input.gjf > input.log`; `check.chk` is copied in and back.
  * ORCA: `source /usr/local/orca6/setup.sh` and `source /usr/local/openmpi-4.1.1/setup.sh`
    (OpenMPI 4.1.1 on PATH and LD_LIBRARY_PATH), then `$OrcaDir/orca input.in > input.log`.
  * Molpro: `molpro26 -n {cpus} -d "$MOLPRO_SCRDIR" input.in`. The integral files in that directory
    can be hundreds of GB for CCSD(T) or MRCI; never copy them back.
  * Q-Chem: `. /usr/local/qchem/qcenv.sh`, `QCSCRATCH=$QCHEM_SCRDIR/scratch`, then
    `qchem -nt {cpus} input.in output.out`.

`servers.yaml` mirrors these: `arc.cpus`, `arc.memory_gb`, `arc.default_job_settings`,
`arc.commands`, `arc.ess_installs` and each install's `setup:` lines. `rag-drg arc compose`
therefore writes settings that match the group's file.

## Node n170 (`pbsnodes n170`, 2026-09-28)

| Field | Value |
|---|---|
| PBS vnode name | `gd004` (`resources_available.host = n170`, so `select=...:host=n170` matches it) |
| Cores | 384 (`resources_available.ncpus`) |
| Memory | 1584807020 kB, about 1511 GiB (`resources_available.mem`) |
| GPUs | none listed |
| Queues it serves | `alon_q`, plus `zeus_combined_q` and `zeus_comb_short` (`resources_available.qlist`) |

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

Rules and gotchas:

* **`zeus_long_q` accepts only 10 queued jobs per user.** Keep that in mind when submitting many
  jobs by hand.
* Queue priority: `alon_q` is 148. `zeus_long_q`, `zeus_short_q` and `alon_comb_q` are 100.
  `zeus_combined_q` is 80. `mafat_new_q` has none set.
* `zeus_long_q`'s per-user core limit is written `[u:PBS_PBS_GENERIC=600]` in its configuration,
  not the usual `PBS_GENERIC`. It may not be enforced; ask the admins before relying on it.
* "zeus_comb_q" means `zeus_combined_q` (24 h). `zeus_comb_short` (3 h) is a different queue,
  and the group doesn't use it.

## GPU queues

GPU jobs go to `gpu_v100_q` or `mafat_gm_q`. The CPU queues above have no GPUs.

| Queue | Nodes | Per node | Max walltime | Who may submit |
|---|---|---|---|---|
| `gpu_v100_q` | 2: n301, n302 (vnodes zg001, zg002) | 40 cores, ~376 GiB, 4x Tesla V100-SXM2-32GB | 480 h | everyone (`acl_group_enable = False`) |
| `mafat_gm_q` | 1: n304 (vnode gm002) | 40 cores, ~754 GiB, 4x Tesla V100-SXM2-32GB | none set (3600 h default) | everyone (ACL not enabled; see below) |

* **Gaussian 16 on the GPUs works** (group test on n302, gpu_v100_q, 2026-09-28) with the
  approach in the template `hpc/templates/pbs_zeus_gaussian_gpu.sh`; use that script, don't write
  a plain GPU script. Timings (GPU vs CPU) are not recorded yet.
* **PBS does not assign a specific GPU.** `ngpus=N` is only a count: PBS does not set
  `CUDA_VISIBLE_DEVICES`, and all 4 V100s stay visible to every job on the node. Other users'
  processes can already be running on any of them (seen on n302). A Gaussian job hard-coded to
  GPU 0 stops when GPU 0 lacks free memory (group experience).
  * Pick a free GPU at job start: the one with the most free memory, e.g. at least 28 GB of 32.
    Expose only that one (`CUDA_VISIBLE_DEVICES=<UUID>`) and write `%GPUCPU=0=<core>` into the
    input.
  * The template `hpc/templates/pbs_zeus_gaussian_gpu.sh` does this, with a node-wide lock and
    claim files so that two jobs starting together don't pick the same GPU. It also takes the
    job's cores from the chosen GPU's block, because PBS doesn't confine jobs to their cores
    either.
* **Gaussian reserves about `%mem` on the GPU.** With `%mem=24GB` the log shows
  `2879845171 words of memory will be used on each GPU`, about 23 GB. A GPU needs at least `%mem`
  of free memory, or g16 stops.
* The `%CPU` / `%GPUCPU` lines go at the top of the input. Each GPU needs a controlling core, and
  that core must also be listed in `%CPU`. The log then shows the thread / CPU / GPU table
  (`Will use 1 GPUs`) to confirm the setup.
  * The proper fix is per-GPU scheduling in PBS (set up by the admins), which would set
    `CUDA_VISIBLE_DEVICES` for each job.
* Request GPUs in the select statement:
  `#PBS -l select=1:ncpus=4:ngpus=1:mem=32gb`. zeus's nodes publish `resources_available.ngpus`.
* NVIDIA driver 580.159.03 with CUDA 13.0 (`nvidia-smi` on n302 and n304, 2026-09-28).
* `mafat_gm_q`: `qstat -Qf` lists `acl_groups = arad_prj,dagan_prj,frankel_prj`, but
  `acl_group_enable` is not set, so PBS does not enforce the list: anyone in the group can use
  this queue (confirmed by the group, 2026-09-28).
* `gpu_v100_q` has only 8 GPUs in total (2 nodes) and is shared with every zeus user.
* The group uses only these two GPU queues, not `vkm_gm_q` (n303, 2 GPUs) or `train_gpu_q`.

## Nodes per queue (`pbsnodes -a`, 2026-09-28)

Memory is `resources_available.mem` converted to GiB. Several queues share nodes, so their totals
overlap.

| Queue | Nodes | Cores per node | Memory per node | Total cores |
|---|---|---|---|---|
| `alon_q` | 4: n170-n173 (vnodes gd001-gd004) | 384 | ~1511 GiB | 1536 |
| `mafat_new_q` | 15: n131, n134, n137-n149 | 256 | ~1007 GiB | 3840 (queue cap 4352) |
| `alon_comb_q` | 28: n017-n033, n088-n099 except n091 | 80 or 128 | 377 GiB or ~1007 GiB | 2768 |
| `zeus_long_q` | 15: n034-n040, n057-n064 | 80 | 377 GiB | 1200 |
| `zeus_short_q` | 17: n017-n033 | 80 | 377 GiB | 1360 |
| `zeus_combined_q` | 100, mixed | 80-384 | 377-1512 GiB | 14824 |

* A single ESS job must fit on one node: at most 80 cores and about 377 GiB on `zeus_long_q`,
  `zeus_short_q` and `alon_comb_q`, and up to 384 cores and about 1.5 TB on `alon_q`.
* `servers.yaml` records the smallest node of each mixed queue (80 cores, 377 GiB), so a request
  that passes `check_resources` fits on any node of that queue.
* `alon_comb_q` sets no `default_chunk.qlist`, unlike the other queues, so it is not certain
  that its jobs land only on the 28 nodes listing it in their `qlist`.
* n170 also serves `zeus_combined_q` and `zeus_comb_short`, so other users' jobs can run next to
  ARC's runner there. The other `alon_q` nodes are n171-n173; n171 serves `alon_q` only.

## Software installation paths

From `ls -l /usr/local` on zeus (2026-09-28). The same entries are in `servers.yaml` (`software:`),
which `render_submit_script` / `rag-drg compose` use.

| Program | Executable | Setup | Readable by |
|---|---|---|---|
| ORCA 5.0.4 | `/usr/local/orca-5.0.4/orca` (`orca5` is a link) | `source /usr/local/orca-5.0.4/setup.sh`; OpenMPI 4.1.1 | everyone |
| ORCA 6.0.0 | `/usr/local/orca-6.0.0/orca` (`orca6` and `orca` are links) | `source /usr/local/orca6/setup.sh`; `source /usr/local/openmpi-4.1.1/setup.sh` (OpenMPI 4.1.1) | everyone |
| Gaussian 09 | `/usr/local/g09/g09` | `source /usr/local/g09/setup.sh` | Unix group `gaussian` |
| Gaussian 16 | `/usr/local/g16/g16` | `source /usr/local/g16/setup.sh` | Unix group `gaussian` |
| Gaussian 16 C.02, GPU build | `/usr/local/g16-gpu/g16/g16` | `source /usr/local/g16-gpu/g16/setup.sh`; **the group's preferred G16, run on CPU queues** | Unix group `gaussian` |
| Q-Chem 6.1 | `/usr/local/qchem6.1/bin/qchem` (`qchem` is a link) | `. /usr/local/qchem/qcenv.sh` | `grinberg-dana_prj` only (the group's licence) |
| Molpro 2024 | `/usr/local/molpro-2024/bin/molpro` (`/usr/local/bin/molpro` points here) | none | Unix group `molpro` |
| Molpro 2026 | `/usr/local/molpro-2026/bin/molpro` (the group's ARC calls it as `molpro26`) | none | Unix group `molpro` |
| Molpro 2022 | `/usr/local/molpro-2022` | not in `servers.yaml` | Unix group `molpro` |
| xTB 6.5.1 | `/usr/local/xtb-6.5.1/bin/xtb` (`/usr/local/xtb` is a link) | none | everyone |

* **Psi4 and PySCF are not installed system-wide.** They live in each user's own conda env, so a
  submit script must activate the user's env. Ask the user which env to use (`conda env list`),
  or create one.
* Gaussian, Molpro and Q-Chem need membership of the Unix group shown (`id` lists your groups).
* **GPU runs:** Gaussian 16 (`g16-gpu`) on `gpu_v100_q` works with
  `hpc/templates/pbs_zeus_gaussian_gpu.sh` (see *GPU queues*). ARC does not use the GPUs. Other
  ESS on the GPUs have not been tried.
