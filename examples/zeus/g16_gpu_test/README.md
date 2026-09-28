# Gaussian 16 GPU test on zeus

A timing test to find out whether the group's `g16-gpu` build (G16 C.02,
`/usr/local/g16-gpu/g16/`) runs on zeus's V100 GPUs, and how much faster it is than running on CPUs.
Status: the GPU job ran on n302 (group test, 2026-09-28); timings not recorded yet. The job is a B3LYP/6-311+G(d,p) frequency calculation on caffeine
(24 atoms), starting from a force-field geometry. It is a benchmark, not chemistry.

| File | What |
|---|---|
| `caffeine_freq_gpu.gjf` + `submit_gpu.sh` | `gpu_v100_q`, 4 cores + 1 GPU |
| `caffeine_freq_cpu.gjf` + `submit_cpu.sh` | the same job on `alon_q`, 4 cores, no GPU |

The GPU script uses `caffeine_freq_gpu.gjf`. If that file is missing but the folder holds
exactly one other `.gjf` (for example `input.gjf`), it uses that one; otherwise set the input
name with `qsub -v INPUT=name submit_gpu.sh`. If the input can't be found, the job stops before
starting Gaussian.

Run both from one directory on zeus:

```bash
qsub submit_gpu.sh
qsub submit_cpu.sh
```

## How the GPU job is set up

* Both scripts follow the group's DRGScripts ARC templates:
  * `#!/bin/bash -l`, `. ~/.bashrc`, `source /usr/local/g16-gpu/g16/setup.sh`, then `g16 < input > log`;
  * scratch in `/gtmp/$USER/scratch/g16/$PBS_JOBID`, deleted at the end.
* Gaussian uses no GPU unless told, so the job needs `%CPU` and `%GPUCPU`. The script builds
  both lines at run time and writes them to the top of a copy of the input
  (`caffeine_freq_gpu.run.gjf`), so they also appear at the top of the `.log`. They are built at
  run time because:
  * Gaussian pins itself to the core numbers it is given, and on a shared node PBS may assign the
    job cores other than 0-3. The script therefore uses the first 4 cores this job may run on
    (`Cpus_allowed_list`).
  * The first of those cores controls the GPU. The GPU number is CUDA's: with
    `CUDA_VISIBLE_DEVICES` set, the job's GPU is 0.
* **GPU choice.** On zeus, PBS counts GPUs (`ngpus=1`) but does not say *which* GPU the job
  got: it does not set `CUDA_VISIBLE_DEVICES`, and all 4 V100s stay visible. Other users' jobs may
  already sit on GPU 0, and Gaussian then stops for lack of GPU memory. So at start the script:
  1. uses PBS's `CUDA_VISIBLE_DEVICES` if it is ever set (the proper setup; nothing else to do);
  2. otherwise reads each GPU's free memory and utilization (`gpu_state_at_start.csv`), and takes
     the GPU with the most free memory that has at least `MIN_FREE_MIB` (default 28000 MiB)
     and is not claimed by another job;
  3. hides every other GPU from Gaussian (`CUDA_VISIBLE_DEVICES=<GPU UUID>`,
     `CUDA_DEVICE_ORDER=PCI_BUS_ID`), so Gaussian sees the chosen GPU as 0 and writes
     `%GPUCPU=0=<first allowed core>`;
  4. prevents two jobs starting together from choosing the same GPU: the choice is made under
     a node-wide lock, and a claim file is left in `/tmp/g16_gpu_claims/<GPU UUID>` (job id +
     PID). Later jobs skip a GPU while that process runs, and the claim is removed when the job
     ends. This only coordinates jobs that use this script; anything else on the GPU is seen
     only through its memory use.
  5. If no GPU qualifies, the job stops with exit code 2 before running `g16`, and prints the
     GPU table.
* **Input checks before g16 starts.**
  * The input needs at least 3 blank lines and a blank last line. Copying through a terminal or
    chat can drop them, and Gaussian then fails with `QPErr --- A syntax error`.
  * A GPU needs at least `%mem` of free memory. Gaussian reserves about `%mem` on each GPU: with
    `%mem=24GB` the log shows `2879845171 words ... on each GPU`, about 23 GB.
* **Cores.** zeus does not confine a job to its cores either: `allowed cores: 0-39` on n302.
  With `%CPU=0-3`, every GPU job would pin itself to the same four cores. The script therefore
  gives each GPU its own block of the node's cores (40 cores / 4 GPUs = 10: GPU 1 gets cores
  10-19) and uses the first `NCPUS` cores of the chosen GPU's block. The block's first core
  controls the GPU (`%CPU=10,11,12,13`, `%GPUCPU=0=10`). If PBS ever does confine the job, its
  cores are used as they are.
* `nvidia-smi` logs GPU utilization to `gpu_usage.csv` every 15 s, which shows whether the GPU
  was really used.

## What to send back

From `gpu_out.txt` and `cpu_out.txt`:
* the header lines: host, `CUDA_VISIBLE_DEVICES`, allowed cores, the `cores:` line, and the `%CPU` / `%GPUCPU` lines;
* the summary: termination line, `Elapsed time`, the picked GPU index and max utilization per GPU
  (the picked GPU should be the busy one).

Also send `gpu_err.txt` / `cpu_err.txt` if they aren't empty, and the last ~30 lines of
`caffeine_freq_gpu.log` if the GPU job failed.

## What we learn

* **GPU used** (max utilization well above 0) **and faster:** GPU runs are confirmed. Then
  `servers.yaml` and the zeus card are updated, and a GPU template can be added.
* **Normal termination but the picked GPU stays at 0 %:** Gaussian ignored the GPU. Check the
  `%GPUCPU` line at the top of the `.log`.
* **Error termination:** the log says why. Common causes are a GPU/core mismatch, or too
  little memory per GPU.

## The proper fix (cluster admins)

With per-GPU scheduling, PBS would give each job its own GPU and set `CUDA_VISIBLE_DEVICES`. Then
`%GPUCPU=0=<core>` would always point at the job's GPU, and none of the selection above would be
needed. OpenPBS / PBS Pro can do this (for example through the cgroups hook's device support), but
only an administrator can enable it. Worth asking the zeus admins about.
