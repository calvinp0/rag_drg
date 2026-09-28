#!/bin/bash
#SBATCH --job-name=orca_ts
#SBATCH --partition=main
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=4G              # input: %maxcore ~3000 (75% of 4 GB, in MB)
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

ORCA_DIR=/opt/orca_6_0_1
OMPI_DIR=/opt/openmpi-4.1.6
export PATH=$ORCA_DIR:$OMPI_DIR/bin:$PATH
export LD_LIBRARY_PATH=$ORCA_DIR:$OMPI_DIR/lib:$LD_LIBRARY_PATH
ORCA_BIN=$ORCA_DIR/orca

INPUT=orca_ts.inp
SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$SCRATCH"
cp "$SLURM_SUBMIT_DIR/$INPUT" "$SCRATCH"/
cd "$SCRATCH"

"$ORCA_BIN" "$INPUT" > "$SLURM_SUBMIT_DIR/${INPUT%.inp}.out"

cp -f *.gbw *.hess *.xyz *.engrad *property.txt *_trj.xyz "$SLURM_SUBMIT_DIR"/ 2>/dev/null
rm -rf "$SCRATCH"
