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

module load <orca-module> <openmpi-module-matching-orca-build>
ORCA_BIN=$(which orca)

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
