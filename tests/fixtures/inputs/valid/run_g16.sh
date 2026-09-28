#!/bin/bash
#SBATCH --job-name=g16_opt
#SBATCH --partition=main
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G                     # input: %mem=56GB
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

export g16root=/opt/gaussian
source $g16root/g16/bsd/g16.profile
G16=$g16root/g16/g16
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$GAUSS_SCRDIR"

cd "$SLURM_SUBMIT_DIR"
"$G16" < g16_opt.gjf > g16_opt.log

rm -rf "$GAUSS_SCRDIR"
