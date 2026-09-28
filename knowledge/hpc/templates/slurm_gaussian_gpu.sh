#!/bin/bash
# TEMPLATE: Gaussian 16 GPU build on Slurm. Replace <...>.
# Input Link0 must match the allocation, e.g. for 16 cores + 2 GPUs:
#   %cpu=0-15
#   %gpucpu=0-1=0,1        (GPU 0,1 controlled by cores 0,1; those cores must be in %cpu)
#   %mem=100GB
# GPUs help HF/DFT energies, gradients, frequencies only (not MP2/CCSD(T)).
#SBATCH --job-name=<name>
#SBATCH --partition=<gpu-partition>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --gres=gpu:2
#SBATCH --mem=120G
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.log

module load <gaussian-gpu-module>
export GAUSS_SCRDIR=${TMPDIR:-/scratch/$USER}/$SLURM_JOB_ID
mkdir -p "$GAUSS_SCRDIR"
echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"; nvidia-smi -L

cd "$SLURM_SUBMIT_DIR"
g16 < <job>.gjf > <job>.log

rm -rf "$GAUSS_SCRDIR"
