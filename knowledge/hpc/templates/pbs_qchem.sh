#!/bin/bash
# TEMPLATE: Q-Chem 6.1 on PBS Pro / OpenPBS (threaded, single node). Replace <...>.
# MEM_TOTAL in $rem is TOTAL MB for the job; keep it ~85-90% of mem below.
#PBS -N <name>
#PBS -q <queue>
#PBS -l select=1:ncpus=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe

cd "$PBS_O_WORKDIR"
export QC=<abs path to qchem-6.1 install>
export QCAUX=$QC/qcaux
source $QC/qcenv.sh
export QCSCRATCH=${TMPDIR:-/scratch/$USER}/${PBS_JOBID%%.*}
export QCLOCALSCR=$QCSCRATCH/local
mkdir -p "$QCLOCALSCR"

qchem -nt 16 <job>.in <job>.out
rm -rf "$QCSCRATCH"
