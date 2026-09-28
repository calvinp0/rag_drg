#!/bin/bash
# TEMPLATE: Gaussian 16 (or 09) CPU job on Slurm. Replace <...>.
# Gaussian is shared-memory: 1 task, N cpus. In the input use %nprocshared=N and
# %mem ~85-90% of --mem (it is TOTAL memory, not per core).
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G                     # input: %mem=56GB
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

module load <gaussian-module>         # sets g16root / g09root
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$GAUSS_SCRDIR"

cd "$SLURM_SUBMIT_DIR"
g16 < <job>.gjf > <job>.log           # or: g09 < <job>.gjf > <job>.log

rm -rf "$GAUSS_SCRDIR"
