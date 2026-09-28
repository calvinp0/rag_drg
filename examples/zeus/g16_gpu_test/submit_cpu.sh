#!/bin/bash -l
# The same job on CPUs only (alon_q, 4 cores), for timing against submit_gpu.sh.
# Submit from this directory:  qsub submit_cpu.sh
#PBS -N g16_cpu_test
#PBS -q alon_q
#PBS -l select=1:ncpus=4:mem=32gb
#PBS -l walltime=04:00:00
#PBS -o cpu_out.txt
#PBS -e cpu_err.txt

. ~/.bashrc
source /usr/local/g16-gpu/g16/setup.sh

cd "$PBS_O_WORKDIR" || exit 1
INPUT=caffeine_freq_cpu

export GAUSS_SCRDIR="/gtmp/$USER/scratch/g16/$PBS_JOBID"
mkdir -p "$GAUSS_SCRDIR"

# --- what PBS gave this job ----------------------------------------------------------
echo "host: $(hostname)   job: $PBS_JOBID   NCPUS=${NCPUS:-unset}"
echo "allowed cores: $(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)"
echo "g16: $(command -v g16)   g16root=${g16root:-unset}"

cleanup() {
    cd "$PBS_O_WORKDIR" || true
    rm -rf "$GAUSS_SCRDIR"
}
trap cleanup EXIT
trap 'exit 143' TERM INT

touch initial_time_cpu
{ time g16 < "$INPUT.gjf" > "$INPUT.log" ; } 2> time_cpu.txt
touch final_time_cpu

echo "--- summary ---"
grep -E "Normal termination|Error termination|Elapsed time|Job cpu time" "$INPUT.log" | tail -4
