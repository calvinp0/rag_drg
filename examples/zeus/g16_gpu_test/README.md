# Gaussian 16 GPU test on zeus

A timing test to find out whether the group's `g16-gpu` build (G16 C.02,
`/usr/local/g16-gpu/g16/`) runs on zeus's V100 GPUs, and how much faster it is than running on CPUs.
Nothing here has been run yet. The job is a B3LYP/6-311+G(d,p) frequency calculation on caffeine
(24 atoms), starting from a force-field geometry. It is a benchmark, not chemistry.

| File | What |
|---|---|
| `caffeine_freq_gpu.gjf` + `submit_gpu.sh` | `gpu_v100_q`, 4 cores + 1 GPU |
| `caffeine_freq_cpu.gjf` + `submit_cpu.sh` | the same job on `alon_q`, 4 cores, no GPU |

Run both from one directory on zeus:

```bash
qsub submit_gpu.sh
qsub submit_cpu.sh
```

## How the GPU job is set up

* Both scripts follow the group's DRGScripts ARC templates:
  * `#!/bin/bash -l`, `. ~/.bashrc`, `source /usr/local/g16-gpu/g16/setup.sh`, then `g16 < input > log`;
  * scratch in `/gtmp/$USER/scratch/g16/$PBS_JOBID`, deleted at the end.
* `%CPU` / `%GPUCPU` are not in the input. The script builds them at run time and passes them as
  `GAUSS_CDEF` / `GAUSS_GDEF`:
  * Gaussian pins itself to the core numbers it is given, and on a shared node PBS may assign the
    job cores other than 0-3. The script therefore uses the first 4 cores this job may run on
    (`Cpus_allowed_list`).
  * The first of those cores controls the GPU. The GPU number is CUDA's: with
    `CUDA_VISIBLE_DEVICES` set, the job's GPU is 0.
* `nvidia-smi` logs GPU utilization to `gpu_usage.csv` every 15 s, which shows whether the GPU
  was really used.

## What to send back

From `gpu_out.txt` and `cpu_out.txt`:
* the header lines: host, `CUDA_VISIBLE_DEVICES`, allowed cores, `GAUSS_CDEF` / `GAUSS_GDEF`;
* the summary: termination line, `Elapsed time`, max GPU utilization.

Also send `gpu_err.txt` / `cpu_err.txt` if they aren't empty, and the last ~30 lines of
`caffeine_freq_gpu.log` if the GPU job failed.

## What we learn

* **GPU used** (max utilization well above 0) **and faster:** GPU runs are confirmed. Then
  `servers.yaml` and the zeus card are updated, and a GPU template can be added.
* **Normal termination but 0 % GPU utilization:** Gaussian ignored the GPU. The next thing to
  try is the GPU numbering: use the physical index from `nvidia-smi` in `GAUSS_GDEF`.
* **Error termination:** the log says why. Common causes are a GPU/core mismatch, or too
  little memory per GPU.
