#!/bin/bash
# TEMPLATE: ORCA (5.x / 6.x) on Slurm, node-local scratch. Replace <...>.
# ORCA is MPI-parallel: request N tasks, set `%pal nprocs N end` in the input to the same N,
# and call ORCA by its FULL PATH (never through mpirun).
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=4G              # input: %maxcore ~3000 (75% of 4 GB, in MB)
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

# --- software paths (absolute; see the cluster card for the real values) ---
ORCA_DIR=<abs path to orca_6_0_x or orca_5_0_4>   # directory that contains the `orca` binary
OMPI_DIR=<abs path to the OpenMPI this ORCA build expects>
export PATH=$ORCA_DIR:$OMPI_DIR/bin:$PATH
export LD_LIBRARY_PATH=$ORCA_DIR:$OMPI_DIR/lib:$LD_LIBRARY_PATH
ORCA_BIN=$ORCA_DIR/orca

INPUT=<job>.inp
SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$SCRATCH"
cp "$SLURM_SUBMIT_DIR/$INPUT" "$SCRATCH"/
# cp "$SLURM_SUBMIT_DIR"/*.gbw "$SLURM_SUBMIT_DIR"/*.hess "$SCRATCH"/ 2>/dev/null  # for MORead / inhess
cd "$SCRATCH"

"$ORCA_BIN" "$INPUT" > "$SLURM_SUBMIT_DIR/${INPUT%.inp}.out"

# Copy back everything useful, then clean scratch.
cp -f *.gbw *.hess *.xyz *.engrad *property.txt *_trj.xyz "$SLURM_SUBMIT_DIR"/ 2>/dev/null
rm -rf "$SCRATCH"
