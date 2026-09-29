#!/bin/bash
# TEMPLATE: PBS submit script for a Gaussian 16/09 job (PBS Pro / OpenPBS, submit with qsub).
# Replace <...>. On zeus use `rag-drg servers submit` / render_submit_script instead (real paths).
# %nprocshared=16 and %mem ~85-90% of the requested mem (TOTAL, not per core).
#PBS -N <name>
#PBS -q <queue>
#PBS -l select=1:ncpus=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe

cd "$PBS_O_WORKDIR"
# --- software paths (absolute) ---
export g16root=<abs path to the directory that CONTAINS g16/>   # G09: g09root, g09/bsd/g09.profile, g09/g09
source $g16root/g16/bsd/g16.profile
G16=$g16root/g16/g16
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/${PBS_JOBID%%.*}
mkdir -p "$GAUSS_SCRDIR"
"$G16" < <job>.gjf > <job>.log
rm -rf "$GAUSS_SCRDIR"
