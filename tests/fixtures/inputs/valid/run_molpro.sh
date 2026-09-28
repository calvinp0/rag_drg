#!/bin/bash
#SBATCH --job-name=molpro_ccsdt
#SBATCH --partition=main
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --mem=72G
#SBATCH --time=48:00:00
#SBATCH --output=%x-%j.log

MOLPRO=/opt/molpro/2024.1/bin/molpro
SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$SCRATCH"

cd "$SLURM_SUBMIT_DIR"
"$MOLPRO" -n "$SLURM_NTASKS" -d "$SCRATCH" molpro_ccsdt.com

rm -rf "$SCRATCH"
