#!/bin/bash
# TEMPLATE: Gaussian 16/09 on PBS Pro / OpenPBS. Replace <...>.
# %nprocshared=16 and %mem ~85-90% of the requested mem (TOTAL, not per core).
#PBS -N <name>
#PBS -q <queue>
#PBS -l select=1:ncpus=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe

cd "$PBS_O_WORKDIR"
module load <gaussian-module>
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/${PBS_JOBID%%.*}
mkdir -p "$GAUSS_SCRDIR"
g16 < <job>.gjf > <job>.log
rm -rf "$GAUSS_SCRDIR"
