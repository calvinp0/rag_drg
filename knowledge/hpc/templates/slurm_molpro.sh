#!/bin/bash
# TEMPLATE: Molpro 2024/2026 on Slurm. Replace <...>.
# `memory,N,m` in the input is mega-WORDS (8 bytes) PER PROCESS:
#   16 processes x memory,500,m = 16 x 4 GB = 64 GB  -> request a bit more than that.
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --mem=72G
#SBATCH --time=48:00:00
#SBATCH --output=%x-%j.log

module load <molpro-module>
SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$SCRATCH"

cd "$SLURM_SUBMIT_DIR"
molpro -n "$SLURM_NTASKS" -d "$SCRATCH" <job>.in

rm -rf "$SCRATCH"
