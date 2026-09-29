#!/bin/bash
#PBS -N input
#PBS -q gpu_v100_q
#PBS -l select=1:ncpus=4:ngpus=1:mem=32gb
cd "$PBS_O_WORKDIR"
source /usr/local/g16-gpu/g16/setup.sh
{ echo '%CPU=0-3'; echo '%GPUCPU=0=0'; cat input.gjf; } > run.gjf
g16 < run.gjf > input.log
