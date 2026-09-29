#!/bin/bash
#PBS -N water
#PBS -q alon_q
#PBS -l select=1:ncpus=8:mem=32gb
#PBS -l walltime=24:00:00
cd "$PBS_O_WORKDIR"
module load qchem
export QCSCRATCH=/tmp
qchem -nt 8 water.in water.out
