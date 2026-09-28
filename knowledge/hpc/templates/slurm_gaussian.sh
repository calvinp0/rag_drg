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

# --- software paths (absolute) ---
export g16root=<abs path to the directory that CONTAINS g16/>   # G09: g09root, g09/bsd/g09.profile, g09/g09
source $g16root/g16/bsd/g16.profile
G16=$g16root/g16/g16
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$GAUSS_SCRDIR"

cd "$SLURM_SUBMIT_DIR"
"$G16" < <job>.gjf > <job>.log

rm -rf "$GAUSS_SCRDIR"
