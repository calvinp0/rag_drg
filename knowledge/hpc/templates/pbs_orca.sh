#!/bin/bash
# TEMPLATE: ORCA on PBS Pro / OpenPBS. Replace <...>. (Torque: -l nodes=1:ppn=16 instead of select.)
# Match `%pal nprocs 16 end` in the input; %maxcore ~75% of mem per core (MB). Full path to orca.
#PBS -N <name>
#PBS -q <queue>
#PBS -l select=1:ncpus=16:mpiprocs=16:mem=64gb
#PBS -l walltime=24:00:00
#PBS -j oe

cd "$PBS_O_WORKDIR"
# --- software paths (absolute; see the cluster card for the real values) ---
ORCA_DIR=<abs path to orca_6_0_x or orca_5_0_4>   # directory that contains the `orca` binary
OMPI_DIR=<abs path to the OpenMPI this ORCA build expects>
export PATH=$ORCA_DIR:$OMPI_DIR/bin:$PATH
export LD_LIBRARY_PATH=$ORCA_DIR:$OMPI_DIR/lib:$LD_LIBRARY_PATH
ORCA_BIN=$ORCA_DIR/orca

INPUT=<job>.inp
SCRATCH=${TMPDIR:-/scratch/$USER}/${PBS_JOBID%%.*}
mkdir -p "$SCRATCH"
cp "$INPUT" "$SCRATCH"/
cd "$SCRATCH"
"$ORCA_BIN" "$INPUT" > "$PBS_O_WORKDIR/${INPUT%.inp}.out"
cp -f *.gbw *.hess *.xyz *property.txt *_trj.xyz "$PBS_O_WORKDIR"/ 2>/dev/null
rm -rf "$SCRATCH"
