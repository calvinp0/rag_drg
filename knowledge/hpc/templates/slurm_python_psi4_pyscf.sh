#!/bin/bash
# TEMPLATE: Psi4 / PySCF (Python, shared-memory threads) on Slurm. Replace <...>.
# Set memory inside the script too: psi4.set_memory("56 GB") / mol.max_memory = 56000 (MB).
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

PYTHON=<abs path to the env>/bin/python        # e.g. /home/<user>/miniforge3/envs/<env>/bin/python
PSI4=<abs path to the env>/bin/psi4

export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
export PSI_SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID    # Psi4
export PYSCF_TMPDIR=$PSI_SCRATCH                               # PySCF
mkdir -p "$PSI_SCRATCH"

cd "$SLURM_SUBMIT_DIR"
"$PYTHON" <script>.py            # Psi4 psithon input instead: "$PSI4" -n $SLURM_CPUS_PER_TASK input.dat output.dat

rm -rf "$PSI_SCRATCH"
