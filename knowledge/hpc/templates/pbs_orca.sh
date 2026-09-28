#!/bin/bash
# TEMPLATE: ORCA on PBS Pro / OpenPBS. Replace <...>. (Torque: -l nodes=1:ppn=16 instead of select.)
# Match `%pal nprocs 16 end` in the input; %maxcore ~75% of mem per core (MB). Full path to orca.
#PBS -N <name>
#PBS -q <queue>
#PBS -l select=1:ncpus=16:mpiprocs=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe

cd "$PBS_O_WORKDIR"
module load <orca-module> <openmpi-module>
ORCA_BIN=$(which orca)

INPUT=<job>.inp
SCRATCH=${TMPDIR:-/scratch/$USER}/${PBS_JOBID%%.*}
mkdir -p "$SCRATCH"
cp "$INPUT" "$SCRATCH"/
cd "$SCRATCH"
"$ORCA_BIN" "$INPUT" > "$PBS_O_WORKDIR/${INPUT%.inp}.out"
cp -f *.gbw *.hess *.xyz *property.txt *_trj.xyz "$PBS_O_WORKDIR"/ 2>/dev/null
rm -rf "$SCRATCH"
