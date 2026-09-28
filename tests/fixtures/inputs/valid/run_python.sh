#!/bin/bash
#SBATCH --job-name=pyscf_uks
#SBATCH --partition=main
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

PYTHON=/home/me/miniforge3/envs/chem/bin/python
PSI4=/home/me/miniforge3/envs/chem/bin/psi4

export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK
export PSI_SCRATCH=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
export PYSCF_TMPDIR=$PSI_SCRATCH
mkdir -p "$PSI_SCRATCH"

cd "$SLURM_SUBMIT_DIR"
"$PYTHON" pyscf_uks.py
"$PSI4" -n $SLURM_CPUS_PER_TASK psi4_opt.dat psi4_opt.out
"$PYTHON" psi4_sp.py

rm -rf "$PSI_SCRATCH"
