#!/bin/bash
#SBATCH --job-name=qchem_freq_ts
#SBATCH --partition=main
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

export QC=/opt/qchem-6.1
export QCAUX=$QC/qcaux
source $QC/qcenv.sh

export QCSCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
export QCLOCALSCR=$QCSCRATCH/local
mkdir -p "$QCLOCALSCR"

cd "$SLURM_SUBMIT_DIR"
qchem -nt "$SLURM_CPUS_PER_TASK" qchem_freq_ts.in qchem_freq_ts.out

rm -rf "$QCSCRATCH"
