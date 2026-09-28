#!/bin/bash -l
# Gaussian 16 (C.02, GPU build) GPU test on zeus: 4 cores + 1 V100.
# Submit from this directory:  qsub submit_gpu.sh
#PBS -N g16_gpu_test
#PBS -q gpu_v100_q
#PBS -l select=1:ncpus=4:ngpus=1:mem=32gb
#PBS -l walltime=04:00:00
#PBS -o gpu_out.txt
#PBS -e gpu_err.txt

. ~/.bashrc
source /usr/local/g16-gpu/g16/setup.sh

cd "$PBS_O_WORKDIR" || exit 1
INPUT=caffeine_freq_gpu
NCPU=${NCPUS:-4}

export GAUSS_SCRDIR="/gtmp/$USER/scratch/g16/$PBS_JOBID"
mkdir -p "$GAUSS_SCRDIR"

# --- what PBS gave this job ----------------------------------------------------------
echo "host: $(hostname)   job: $PBS_JOBID   NCPUS=${NCPUS:-unset}"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
echo "allowed cores: $(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)"
echo "g16: $(command -v g16)   g16root=${g16root:-unset}"
nvidia-smi -L

# Gaussian pins itself to the core numbers in %CPU, so take them from the cores this job may
# use (the PBS cpuset) instead of assuming 0-3; the first core controls the GPU (%GPUCPU).
expand() {  # "0-3,8,10-11" -> "0 1 2 3 8 10 11"
    local p i out=()
    IFS=',' read -ra parts <<< "$1"
    for p in "${parts[@]}"; do
        if [[ $p == *-* ]]; then for ((i=${p%-*}; i<=${p#*-}; i++)); do out+=("$i"); done
        else out+=("$p"); fi
    done
    echo "${out[@]}"
}
read -ra ALLOWED <<< "$(expand "$(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)")"
CPUS=("${ALLOWED[@]:0:$NCPU}")
CPU_LINE="%CPU=$(IFS=,; echo "${CPUS[*]}")"
# GPU numbers are CUDA's: with CUDA_VISIBLE_DEVICES set, the job's first GPU is 0.
GPU_LINE="%GPUCPU=0=${CPUS[0]}"
echo "$CPU_LINE   $GPU_LINE"

# Gaussian uses no GPU unless told: put %CPU / %GPUCPU at the top of the input (they then
# appear at the top of the .log too). The .gjf in this directory stays unchanged.
{ echo "$CPU_LINE"; echo "$GPU_LINE"; cat "$INPUT.gjf"; } > "$INPUT.run.gjf"

# --- GPU utilization every 15 s, to show the GPU really works --------------------------
nvidia-smi --query-gpu=timestamp,index,uuid,utilization.gpu,memory.used --format=csv -l 15 > gpu_usage.csv &
SMI_PID=$!

cleanup() {
    kill "$SMI_PID" 2>/dev/null
    cd "$PBS_O_WORKDIR" || true
    rm -rf "$GAUSS_SCRDIR"
}
trap cleanup EXIT
trap 'exit 143' TERM INT

touch initial_time_gpu
{ time g16 < "$INPUT.run.gjf" > "$INPUT.log" ; } 2> time_gpu.txt
touch final_time_gpu

echo "--- summary ---"
grep -iE "gpu" "$INPUT.log" | head -20
grep -E "Normal termination|Error termination|Elapsed time|Job cpu time" "$INPUT.log" | tail -4
echo "max GPU utilization (%): $(awk -F', ' 'NR>1{gsub(/ %/,"",$4); if ($4>m) m=$4} END{print m+0}' gpu_usage.csv)"
