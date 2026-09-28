---
title: 'zeus GPUs: PBS does not assign a GPU - pick a free one at job start'
domain: hpc
software: gaussian
version: '16'
doc_type: lesson
status: unreviewed
tags:
- gpu
- zeus
- pbs
- gpucpu
author: calvin.p
date: '2026-09-28'
---

# zeus GPUs: PBS does not assign a GPU - pick a free one at job start

## Mistake

Wrote a Gaussian GPU job for zeus (gpu_v100_q / mafat_gm_q) with a fixed %GPUCPU=0=0 (and %CPU=0-3), assuming PBS gives the job GPU 0 and those cores. g16 stopped because GPU 0 was already used by another job and lacked memory.

## Correct approach

On zeus, ngpus=N is only a count: PBS sets no CUDA_VISIBLE_DEVICES and no cpuset, so all 4 V100s and all 40 cores are visible to every job. At job start pick the GPU with the most free memory (at least %mem free: Gaussian reserves about %mem on the GPU), expose only it via CUDA_VISIBLE_DEVICES=<UUID>, take cores from that GPU's block, and write %CPU/%GPUCPU into the input. Use knowledge/hpc/templates/pbs_zeus_gaussian_gpu.sh, which does all of this with claim files against two jobs picking the same GPU.

## Evidence / source

Group tests on n302, 2026-09-28: GPU 0 busy failed; the template's approach ran (log: Will use 1 GPUs, %GPUCPU=0=20).
