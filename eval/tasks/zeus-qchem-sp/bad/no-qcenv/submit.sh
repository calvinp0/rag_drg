#!/bin/bash
#PBS -N water
#PBS -q alon_q
#PBS -l select=1:ncpus=8:mem=32gb
#PBS -l walltime=24:00:00
cd "$PBS_O_WORKDIR"
export QCSCRATCH=/gtmp/$USER/$PBS_JOBID
mkdir -p $QCSCRATCH
/usr/local/qchem6.1/bin/qchem -nt 8 water.in water.out
