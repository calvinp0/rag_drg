#!/bin/bash
# TEMPLATE: Q-Chem 6.1 on Slurm (threaded, single node). Replace <...>.
# In $rem set MEM_TOTAL (MB, TOTAL for the job) ~85-90% of --mem, e.g. 56000 for 64G.
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

# --- software paths (absolute) ---
export QC=<abs path to qchem-6.1 install>
export QCAUX=$QC/qcaux                     # check the layout of your install
source $QC/qcenv.sh

export QCSCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
export QCLOCALSCR=$QCSCRATCH/local
mkdir -p "$QCLOCALSCR"

cd "$SLURM_SUBMIT_DIR"
qchem -nt "$SLURM_CPUS_PER_TASK" <job>.in <job>.out

rm -rf "$QCSCRATCH"
