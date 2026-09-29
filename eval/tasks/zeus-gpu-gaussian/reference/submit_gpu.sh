#!/bin/bash -l
# TEMPLATE: Gaussian 16 on a zeus GPU (gpu_v100_q or mafat_gm_q), group-tested approach.
# Confirmed by the group on n302 (2026-09-28): the job ran on the GPU picked at start-up.
#
# Why this is not a plain GPU script: PBS on zeus counts GPUs (ngpus=N) but does not say which
# one the job got (no CUDA_VISIBLE_DEVICES; all 4 V100s visible, other users' jobs on any of
# them), and does not confine the job to its cores (all 40 visible). Hard-coding %GPUCPU=0=0
# makes g16 stop when GPU 0 is in use. This script, at start:
#   1. checks the input (blank lines; Gaussian reserves about %mem on the GPU);
#   2. picks the unclaimed GPU with the most free memory (>= %mem and >= MIN_FREE_MIB), under a
#      node lock with claim files so two jobs never pick the same GPU;
#   3. exposes only that GPU (CUDA_VISIBLE_DEVICES=<UUID>), so Gaussian sees it as GPU 0;
#   4. takes cores from that GPU's block (GPU k -> cores 10k..10k+9 on a 40-core node);
#   5. writes %CPU / %GPUCPU into a copy of the input (<input>.run.gjf) and runs g16 on it.
# The input .gjf must NOT contain %CPU / %GPUCPU / %nprocshared. Default input: input.gjf
# (qsub -v INPUT=name for name.gjf). For mafat_gm_q change the -q line below.
# Source: rag-drg examples/zeus/g16_gpu_test/ (README explains every step).
#PBS -N input
#PBS -q gpu_v100_q
#PBS -l select=1:ncpus=4:ngpus=1:mem=32gb
#PBS -l walltime=04:00:00
#PBS -o gpu_out.txt
#PBS -e gpu_err.txt

. ~/.bashrc
source /usr/local/g16-gpu/g16/setup.sh

cd "$PBS_O_WORKDIR" || exit 1
INPUT=${INPUT:-input}   # input name without .gjf (or: qsub -v INPUT=name <this script>)
NCPU=${NCPUS:-4}
NGPU=${NGPU:-1}               # GPUs for this job (match ngpus= above)
MIN_FREE_MIB=${MIN_FREE_MIB:-28000}   # a GPU needs at least this much free memory (V100: 32 GB)
CLAIMS=${GPU_CLAIMS_DIR:-/tmp/g16_gpu_claims}   # node-local: which job took which GPU

# the input must exist; if the default name is missing but there is exactly one other .gjf, use it
if [ ! -f "$INPUT.gjf" ]; then
    mapfile -t GJFS < <(ls *.gjf 2>/dev/null | grep -v '\.run\.gjf$')
    if [ "${#GJFS[@]}" -eq 1 ]; then INPUT="${GJFS[0]%.gjf}"; echo "using input ${INPUT}.gjf"
    else echo "ERROR: $INPUT.gjf not found (and not exactly one other .gjf here: ${GJFS[*]:-none}); set INPUT=name" >&2; exit 1
    fi
fi

# Gaussian needs blank lines after the route, the title and the geometry (and at the end);
# copying a file through a terminal or chat can drop them ("QPErr --- A syntax error").
BLANKS=$(grep -c '^[[:space:]]*$' "$INPUT.gjf")
if [ "$BLANKS" -lt 3 ] || [ -n "$(tail -n 1 "$INPUT.gjf" | tr -d '[:space:]')" ]; then
    echo "ERROR: $INPUT.gjf needs a blank line after the route, after the title and after the geometry" \
         "(found $BLANKS blank line(s)); not starting g16" >&2
    exit 1
fi

# Gaussian reserves about %mem of memory on each GPU (log: "... words of memory will be used on
# each GPU"), so a GPU needs at least %mem free, whatever MIN_FREE_MIB says
MEM_GB=$(grep -ioE '^%mem=[0-9]+GB' "$INPUT.gjf" | grep -oE '[0-9]+' | head -1)
if [ -n "$MEM_GB" ] && [ $((MEM_GB * 1024)) -gt "$MIN_FREE_MIB" ]; then
    MIN_FREE_MIB=$((MEM_GB * 1024))
    echo "%mem=${MEM_GB}GB: a GPU needs >= $MIN_FREE_MIB MiB free"
fi

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
# ALLOWED_CORES overrides the list (testing only)
read -ra ALLOWED <<< "$(expand "${ALLOWED_CORES:-$(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)}")"

# --- pick the GPU(s) -------------------------------------------------------------------
# zeus does not give a job its own GPU: every job on the node sees all 4, and other users'
# processes may already sit on GPU 0. Gaussian stops when its GPU lacks memory, so choose the
# GPU(s) with the most free memory and hide the others from Gaussian (CUDA_VISIBLE_DEVICES,
# by UUID so the numbering cannot mix up); Gaussian then sees them as GPUs 0..NGPU-1.
nvidia-smi --query-gpu=index,uuid,memory.free,memory.total,utilization.gpu --format=csv,noheader,nounits \
    | tr -d ' ' > gpu_state_at_start.csv
echo "GPUs at start (index,uuid,free MiB,total MiB,util %):"; cat gpu_state_at_start.csv
if [ -n "${CUDA_VISIBLE_DEVICES:-}" ] && [ "$CUDA_VISIBLE_DEVICES" != "NoDevFiles" ]; then
    echo "PBS set CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES: using the GPU(s) it assigned"
    PICKED_IDX=$(nvidia-smi --query-gpu=index --format=csv,noheader | tr '\n' ',' | sed 's/,$//')
else
    # Two jobs starting together on one node would both see the same GPU as free, so choose
    # under a node-wide lock and leave a claim file (job id + PID) that later jobs skip while
    # its process is alive. The claim is removed when this job ends.
    mkdir -p -m 1777 "$CLAIMS" 2>/dev/null
    exec 9>>"$CLAIMS/.lock"; flock -w 120 9 || echo "warning: no GPU lock after 120 s; choosing anyway" >&2
    claimed() {  # is this UUID claimed by a job whose process is still running?
        local f="$CLAIMS/$1" pid
        [ -f "$f" ] || return 1
        pid=$(awk '{print $2}' "$f")
        if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then return 0; fi
        rm -f "$f" 2>/dev/null; return 1   # stale claim
    }
    # most free memory first, then least busy; keep unclaimed GPUs with enough free memory
    PICK=()
    while IFS= read -r row; do
        claimed "$(cut -d, -f2 <<< "$row")" && continue
        PICK+=("$row"); [ "${#PICK[@]}" -ge "$NGPU" ] && break
    done < <(sort -t, -k3,3nr -k5,5n gpu_state_at_start.csv | awk -F, -v min="$MIN_FREE_MIB" '$3 >= min')
    if [ "${#PICK[@]}" -lt "$NGPU" ]; then
        flock -u 9
        echo "ERROR: need $NGPU free GPU(s) with >= $MIN_FREE_MIB MiB free and not claimed by another" \
             "job, found ${#PICK[@]}; not starting g16" >&2
        ls -l "$CLAIMS" >&2
        exit 2
    fi
    MY_CLAIMS=()
    for row in "${PICK[@]}"; do
        u=$(cut -d, -f2 <<< "$row"); echo "$PBS_JOBID $$ $(hostname)" > "$CLAIMS/$u"; MY_CLAIMS+=("$CLAIMS/$u")
    done
    flock -u 9
    export CUDA_DEVICE_ORDER=PCI_BUS_ID
    export CUDA_VISIBLE_DEVICES=$(printf '%s\n' "${PICK[@]}" | cut -d, -f2 | paste -sd,)
    PICKED_IDX=$(printf '%s\n' "${PICK[@]}" | cut -d, -f1 | paste -sd,)
    echo "picked GPU index(es) $PICKED_IDX -> CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
fi
# --- pick the cores ----------------------------------------------------------------------
# If PBS confined the job to its cores (a cpuset), use those. zeus does not: every job sees
# all 40 cores, so jobs pinned to "0-3" would pile onto the same cores. Instead each GPU owns
# an equal block of the node's cores (GPU k -> cores k*B .. k*B+B-1, B = cores / GPUs), and the
# job uses the first NCPU cores of its GPU's block. Jobs on different GPUs never share cores.
NGPU_NODE=$(wc -l < gpu_state_at_start.csv)
if [ "${#ALLOWED[@]}" -le $((NCPU + 1)) ]; then
    CPUS=("${ALLOWED[@]:0:$NCPU}")
    echo "cores: PBS confined the job to ${ALLOWED[*]}"
else
    BLOCK=$(( ${#ALLOWED[@]} / NGPU_NODE ))
    if [ "$NCPU" -gt $((BLOCK * NGPU)) ]; then
        echo "ERROR: $NCPU cores > $BLOCK cores per GPU x $NGPU GPU(s) on this node" >&2; exit 2
    fi
    POOL=()
    for k in ${PICKED_IDX//,/ }; do POOL+=("${ALLOWED[@]:$((k * BLOCK)):$BLOCK}"); done
    # the first core of each GPU's block controls that GPU; fill the rest from the pool
    CTRL=(); for k in ${PICKED_IDX//,/ }; do CTRL+=("${ALLOWED[$((k * BLOCK))]}"); done
    CPUS=("${CTRL[@]}")
    for c in "${POOL[@]}"; do
        [ "${#CPUS[@]}" -ge "$NCPU" ] && break
        [[ " ${CTRL[*]} " == *" $c "* ]] || CPUS+=("$c")
    done
    echo "cores: no cpuset (all ${#ALLOWED[@]} visible); using GPU block(s) of $BLOCK cores -> ${CPUS[*]}"
fi
CPU_LINE="%CPU=$(IFS=,; echo "${CPUS[*]}")"
nvidia-smi topo -m 2>/dev/null | head -8

# %GPUCPU=<GPUs as Gaussian sees them>=<one controlling core each, also in %CPU>
GPU_LINE="%GPUCPU=0-$((NGPU - 1))=$(IFS=,; echo "${CPUS[*]:0:$NGPU}")"
[ "$NGPU" -eq 1 ] && GPU_LINE="%GPUCPU=0=${CPUS[0]}"
echo "$CPU_LINE   $GPU_LINE"

# Gaussian uses no GPU unless told: put %CPU / %GPUCPU at the top of the input (they then
# appear at the top of the .log too). The .gjf in this directory stays unchanged.
{ echo "$CPU_LINE"; echo "$GPU_LINE"; cat "$INPUT.gjf"; } > "$INPUT.run.gjf"

# --- GPU utilization every 15 s, to show the GPU really works --------------------------
nvidia-smi --query-gpu=timestamp,index,uuid,utilization.gpu,memory.used --format=csv -l 15 > gpu_usage.csv &
SMI_PID=$!

cleanup() {
    kill "$SMI_PID" 2>/dev/null
    rm -f "${MY_CLAIMS[@]}" 2>/dev/null
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
echo "picked GPU index(es): $PICKED_IDX"
echo "max utilization per GPU index (%):"
awk -F', ' 'NR>1{gsub(/ %/,"",$4); if ($4>m[$2]) m[$2]=$4} END{for (i in m) print "  GPU " i ": " m[i]}' gpu_usage.csv | sort
