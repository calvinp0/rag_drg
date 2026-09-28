"""Resource checks and ready-to-run submit scripts generated from servers.yaml.

The script bodies mirror knowledge/hpc/templates/*.sh: programs are called by absolute path,
MPI codes (ORCA, Molpro) get ntasks=N / cpus-per-task=1, threaded codes (Gaussian, Q-Chem,
Psi4, PySCF) get ntasks=1 / cpus-per-task=N, and each ESS uses a per-job scratch directory
that is removed at the end.
"""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath

from .access import queue_access
from .model import PBS_FAMILY, Partition, Server, SoftwareInstall, format_walltime, parse_walltime

DEFAULT_WALLTIME_S = 24 * 3600
DEFAULT_MAX_CORES = 16
MEM_SHARE = 0.9  # default memory: this fraction of the node's memory per core x cores
_INPUT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$")
SUBMIT_SCHEDULERS = ("slurm", "pbs", "pbspro", "torque", "local")


class SubmitError(ValueError):
    """The request violates a partition/software limit. `.problems` = [{severity, message}]."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__("; ".join(p["message"] for p in problems if p["severity"] == "error"))


def _p(severity: str, message: str) -> dict:
    return {"severity": severity, "message": message}


def check_resources(server: Server | str, partition: str | None, cores: int, mem_gb: float,
                    walltime: str | int | float, gpus: int = 0, *, software: str | None = None,
                    cfg=None, user: str | None = None, groups: list[str] | None = None,
                    check_access: bool = True) -> list[dict]:
    """Check a resource request against servers.yaml limits.

    Returns [{"severity": "error"|"warning"|"info", "message": str}]; no "error" entries means
    the request fits. `server` may be a Server or a name (then `cfg` is needed to load
    servers.yaml). `partition=None` means the server's default partition. `software` (a
    servers.yaml software key) additionally checks that the program may run on that partition.
    `user`/`groups` (the requesting user's Unix name and groups) are checked against the
    partition's `access:` rule: an error when denied, an info when a rule exists but the identity
    is unknown (see access.queue_access). `check_access=False` skips the access check.
    """
    if isinstance(server, str):
        from .model import load_servers

        servers = load_servers(cfg) if cfg is not None else {}
        if server not in servers:
            return [_p("error", f"unknown server {server!r}; known: {', '.join(servers) or '(none)'}")]
        server = servers[server]
    out: list[dict] = []
    if partition is None:
        part = server.default_partition
        if part is None:
            return [_p("error", f"{server.name} has no partitions in servers.yaml")]
    else:
        part = server.partitions.get(partition)
        if part is None:
            return [_p("error", f"unknown partition {partition!r} on {server.name}; "
                                f"known: {', '.join(server.partitions)}")]
    where = f"{server.name}:{part.name}"
    if check_access and part.access is not None:
        acc = queue_access(server, part, user, groups)
        if acc["allowed"] is False:
            out.append(_p("error", acc["reason"]))
        elif acc["allowed"] is None:
            out.append(_p("info", acc["reason"]))

    try:
        cores = int(cores)
    except (TypeError, ValueError):
        cores = 0
    if cores < 1:
        out.append(_p("error", "cores must be a positive integer"))
    elif cores > part.cores_per_node:
        out.append(_p("error", f"{cores} cores > {part.cores_per_node} cores per node on {where}; "
                               "these ESS jobs run on a single node"))
    try:
        mem = float(mem_gb)
    except (TypeError, ValueError):
        mem = 0.0
    if mem <= 0:
        out.append(_p("error", "mem_gb must be a positive number"))
    elif mem > part.mem_per_node_gb:
        out.append(_p("error", f"{mem:g} GB > {part.mem_per_node_gb:g} GB per node on {where}"))
    elif mem > 0.95 * part.mem_per_node_gb:
        out.append(_p("warning", f"{mem:g} GB is > 95% of the node memory ({part.mem_per_node_gb:g} GB); "
                                 "the OS reserves some, so the job may not start - request a bit less"))
    elif cores >= 1 and cores <= part.cores_per_node and mem < 0.5 * cores:
        out.append(_p("warning", f"only {mem:g} GB for {cores} cores (< 0.5 GB per core) is unusually little"))
    try:
        wall = parse_walltime(walltime)
        if wall > part.max_walltime_seconds:
            out.append(_p("error", f"walltime {format_walltime(wall)} > max {format_walltime(part.max_walltime_seconds)} on {where}"))
    except ValueError as e:
        out.append(_p("error", str(e)))
    try:
        gpus = int(gpus or 0)
    except (TypeError, ValueError):
        gpus = -1
    if gpus < 0:
        out.append(_p("error", "gpus must be an integer >= 0"))
    elif gpus > 0 and part.gpus_per_node == 0:
        out.append(_p("error", f"{where} has no GPUs"))
    elif gpus > part.gpus_per_node:
        out.append(_p("error", f"{gpus} GPUs > {part.gpus_per_node} GPUs per node on {where}"))
    elif gpus == 0 and part.gpus_per_node > 0:
        out.append(_p("warning", f"{where} is a GPU partition but no GPUs are requested"))

    if software is not None:
        sw = server.software.get(software)
        if sw is None:
            out.append(_p("error", f"{software!r} is not installed on {server.name}; "
                                   f"known: {', '.join(server.software) or '(none)'}"))
        else:
            if sw.partitions and part.name not in sw.partitions:
                out.append(_p("error", f"{software} may only run on partition(s) {', '.join(sw.partitions)} "
                                       f"of {server.name}, not {part.name}"))
            if _is_gpu_build(sw) and gpus == 0:
                out.append(_p("warning", f"{software} is a GPU build but no GPUs are requested (pass gpus=N)"))
    return out


def _is_gpu_build(sw: SoftwareInstall) -> bool:
    return sw.key.endswith("-gpu") or "gpu" in sw.key.split("-")


# ----------------------------------------------------------------- per-ESS input lines

def input_lines(sw: SoftwareInstall, cores: int, mem_gb: float, gpus: int, input_file: str) -> list[str]:
    """The lines the input file needs so the program uses what the script requests."""
    mem_mb = mem_gb * 1024
    ess = sw.ess
    if ess == "orca":
        maxcore = int(0.75 * mem_mb / cores)
        return [f"%pal nprocs {cores} end", f"%maxcore {maxcore}",
                "(%maxcore is MB PER CORE, ~75% of the memory per core; ORCA itself starts the MPI "
                "processes - never run it through mpirun)"]
    if ess == "gaussian":
        g_mem = int(0.88 * mem_gb)
        mem_line = f"%mem={g_mem}GB" if g_mem >= 1 else f"%mem={int(0.88 * mem_mb)}MB"
        if gpus:
            cpus = f"0-{cores - 1}" if cores > 1 else "0"
            gcpu = f"0-{gpus - 1}" if gpus > 1 else "0"
            glist = ",".join(str(i) for i in range(gpus))
            return [f"%cpu={cpus}", f"%gpucpu={gcpu}={glist}", mem_line,
                    "(%mem is TOTAL memory, ~88% of the request; each GPU is driven by one of the %cpu "
                    "cores; GPUs speed up HF/DFT energies, gradients and frequencies only)"]
        return [f"%nprocshared={cores}", mem_line, "(%mem is TOTAL memory, ~88% of the request)"]
    if ess == "qchem":
        return [f"MEM_TOTAL {int(0.88 * mem_mb)}",
                "($rem section; MB, TOTAL for the job, ~88% of the request; threads are set by "
                f"`qchem -nt {cores}` in the script)"]
    if ess == "molpro":
        nproc = cores if sw.parallel == "mpi" else 1
        words = int(0.88 * mem_gb * 1024 ** 3 / (8 * 1e6 * nproc))
        flag = f"-n {cores}" if sw.parallel == "mpi" else f"-t {cores}"
        return [f"memory,{words},m",
                f"(mega-WORDS of 8 bytes PER PROCESS: {nproc} x {words} Mw x 8 B ~ "
                f"{nproc * words * 8 / 1000:.0f} GB of the {mem_gb:g} GB requested; the script runs `molpro {flag}`)"]
    if ess == "psi4":
        mem = int(0.88 * mem_gb)
        if input_file.endswith(".py"):
            return [f'psi4.set_memory("{mem} GB")', f"psi4.set_num_threads({cores})",
                    "(set_memory is TOTAL memory, ~88% of the request)"]
        return [f"memory {mem} GB",
                f"(psithon input; TOTAL memory ~88% of the request; threads come from `psi4 -n {cores}` in the script)"]
    if ess == "pyscf":
        return [f"mol.max_memory = {int(0.88 * mem_mb)}  # MB, TOTAL (or pass max_memory= to gto.M)",
                f"from pyscf import lib; lib.num_threads({cores})  # OMP_NUM_THREADS={cores} is also exported"]
    return []


# ----------------------------------------------------------------- script pieces

def _job_name(name: str, scheduler: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "job"
    if not n[0].isalpha():
        n = "j" + n
    return n[:15] if scheduler == "torque" else n[:64]


def _mem_str(mem_gb: float, unit_g: str, unit_m: str) -> str:
    if float(mem_gb).is_integer():
        return f"{int(mem_gb)}{unit_g}"
    return f"{int(math.ceil(mem_gb * 1024))}{unit_m}"


def _header(server: Server, part: Partition, sw: SoftwareInstall, name: str, cores: int,
            mem_gb: float, wall: str, gpus: int) -> list[str]:
    mpi = sw.parallel == "mpi"
    s = server.scheduler
    if s == "slurm":
        lines = [
            f"#SBATCH --job-name={name}",
            f"#SBATCH --partition={part.name}",
            "#SBATCH --nodes=1",
            f"#SBATCH --ntasks={cores if mpi else 1}",
            f"#SBATCH --cpus-per-task={1 if mpi else cores}",
            f"#SBATCH --mem={_mem_str(mem_gb, 'G', 'M')}",
            f"#SBATCH --time={wall}",
        ]
        if gpus:
            lines.append(f"#SBATCH --gres=gpu:{gpus}")
        lines.append("#SBATCH --output=%x-%j.out")
        return lines
    if s in ("pbs", "pbspro"):
        sel = f"select=1:ncpus={cores}" + (f":mpiprocs={cores}" if mpi else "")
        sel += f":mem={_mem_str(mem_gb, 'gb', 'mb')}" + (f":ngpus={gpus}" if gpus else "")
        return [f"#PBS -N {name}", f"#PBS -q {part.name}", f"#PBS -l {sel}",
                f"#PBS -l walltime={wall}", "#PBS -j oe"]
    if s == "torque":
        nodes = f"nodes=1:ppn={cores}" + (f":gpus={gpus}" if gpus else "")
        return [f"#PBS -N {name}", f"#PBS -q {part.name}", f"#PBS -l {nodes}",
                f"#PBS -l mem={_mem_str(mem_gb, 'gb', 'mb')}", f"#PBS -l walltime={wall}", "#PBS -j oe"]
    return [f"# local run (no scheduler): {cores} cores, {mem_gb:g} GB, walltime not enforced"]


def _job_vars(scheduler: str) -> tuple[str, str]:
    if scheduler == "slurm":
        return '"$SLURM_SUBMIT_DIR"', '"$SLURM_JOB_ID"'
    if scheduler in PBS_FAMILY:
        return '"$PBS_O_WORKDIR"', '"${PBS_JOBID%%.*}"'
    return '"$PWD"', '"$$"'


def _scratch_line(server: Server) -> str:
    base = server.scratch.path
    if base and any(v in base for v in ("$SLURM_JOB_ID", "$PBS_JOBID", "$JOBID", "${SLURM_JOB_ID}")):
        return f'SCRATCH="{base}"'
    if base:
        return f'SCRATCH="{base.rstrip("/")}/$JOBID"'
    return 'SCRATCH="${TMPDIR:-/tmp}/$JOBID"'


def _sh(value: str) -> str:
    """Double-quote a value for an `export`, keeping $VAR references working."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'


def _python_for(sw: SoftwareInstall) -> str:
    exe = PurePosixPath(sw.executable)
    return str(exe) if exe.name.startswith("python") else str(exe.parent / "python")


def _body(sw: SoftwareInstall, cores: int, gpus: int, input_file: str) -> tuple[str, list[str]]:
    """(executable variable name, body lines) for one ESS."""
    inp = PurePosixPath(input_file)
    stem = str(inp.with_suffix(""))
    ess = sw.ess
    if ess == "orca":
        out = stem + ".out"
        back = "$WORKDIR" if str(inp.parent) == "." else f"$WORKDIR/{inp.parent}"
        return "ORCA_BIN", [
            f'INPUT="{inp.name}"',
            f'cp "$WORKDIR/{input_file}" "$SCRATCH"/',
            '# for MORead / InHess also copy: cp "$WORKDIR"/*.gbw "$WORKDIR"/*.hess "$SCRATCH"/ 2>/dev/null',
            'cd "$SCRATCH"',
            f'"$ORCA_BIN" "$INPUT" > "$WORKDIR/{out}"',
            "",
            "# copy back everything useful, then clean scratch",
            f'cp -f *.gbw *.hess *.xyz *.engrad *property.txt *_trj.xyz "{back}"/ 2>/dev/null',
            'cd "$WORKDIR"',
            'rm -rf "$SCRATCH"',
        ]
    if ess == "gaussian":
        lines = ['export GAUSS_SCRDIR="$SCRATCH"']
        if gpus:
            lines.append('echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"; nvidia-smi -L')
        lines += ['cd "$WORKDIR"', f'"$GAUSSIAN" < "{input_file}" > "{stem}.log"', "", 'rm -rf "$SCRATCH"']
        return "GAUSSIAN", lines
    if ess == "qchem":
        flag = "-nt" if sw.parallel == "threads" else "-np"
        return "QCHEM", [
            'export QCSCRATCH="$SCRATCH"',
            'export QCLOCALSCR="$QCSCRATCH/local"',
            'mkdir -p "$QCLOCALSCR"',
            'cd "$WORKDIR"',
            f'"$QCHEM" {flag} {cores} "{input_file}" "{stem}.out"',
            "",
            'rm -rf "$SCRATCH"',
        ]
    if ess == "molpro":
        flag = "-n" if sw.parallel == "mpi" else "-t"
        return "MOLPRO", [
            'cd "$WORKDIR"',
            f'"$MOLPRO" {flag} {cores} -d "$SCRATCH" "{input_file}"      # writes {stem}.out',
            "",
            'rm -rf "$SCRATCH"',
        ]
    if ess in ("psi4", "pyscf"):
        lines = [f"export OMP_NUM_THREADS={cores}", f"export MKL_NUM_THREADS={cores}"]
        if ess == "psi4":
            lines.append('export PSI_SCRATCH="$SCRATCH"')
        else:
            lines.append('export PYSCF_TMPDIR="$SCRATCH"')
        lines.append('cd "$WORKDIR"')
        if ess == "psi4" and not input_file.endswith(".py"):
            lines.append(f'"$PSI4" -n {cores} "{input_file}" "{stem}.out"')
            var = "PSI4"
        else:
            lines.append(f'"$PYTHON" "{input_file}" > "{stem}.out" 2>&1')
            var = "PYTHON"
        lines += ["", 'rm -rf "$SCRATCH"']
        return var, lines
    raise ValueError(f"unsupported ESS {ess!r}")


def render_submit_script(server: Server, software_key: str, input_file: str, job_name: str | None = None,
                         cores: int | None = None, mem_gb: float | None = None,
                         walltime: str | int | float | None = None, partition: str | None = None,
                         gpus: int = 0, *, user: str | None = None,
                         groups: list[str] | None = None) -> tuple[str, list[str]]:
    """A ready-to-run submit script for `software_key` on `server`, plus notes.

    Defaults: the software's allowed/default partition, min(16, cores per node) cores,
    ~90% of the proportional share of node memory, and min(24 h, partition max) walltime.
    Notes list the matching in-input memory/core lines and any warnings.
    `user`/`groups` are checked against partition `access:` rules; a denied partition is an
    error, and the default partition skips partitions this identity may not use.
    Raises SubmitError (with `.problems`) if the request violates a limit.
    """
    if software_key not in server.software:
        raise SubmitError([_p("error", f"{software_key!r} is not installed on {server.name}; "
                                       f"known: {', '.join(server.software) or '(none)'}")])
    if server.scheduler not in SUBMIT_SCHEDULERS:
        raise SubmitError([_p("error", f"submit scripts for scheduler {server.scheduler!r} are not supported "
                                       f"(supported: {', '.join(SUBMIT_SCHEDULERS)})")])
    if not _INPUT_RE.match(input_file or "") or ".." in PurePosixPath(input_file).parts:
        raise SubmitError([_p("error", f"input_file {input_file!r} must be a relative path of letters, digits, _ . / + -")])
    sw = server.software[software_key]
    notes: list[str] = []

    if partition is None:
        allowed = server.partitions_for(software_key)
        dflt = server.default_partition
        if user is not None or groups is not None:
            usable = [p for p in allowed if queue_access(server, p, user, groups)["allowed"] is not False]
            if allowed and not usable:
                raise SubmitError([_p("error", f"{software_key} may run on {', '.join(p.name for p in allowed)} "
                                               f"of {server.name}, but " + "; ".join(
                                                   queue_access(server, p, user, groups)["reason"]
                                                   for p in allowed))])
            skipped = [p.name for p in allowed if p not in usable]
            allowed = usable
        else:
            skipped = []
        part = dflt if dflt in allowed else (allowed[0] if allowed else dflt)
        if part is None:
            raise SubmitError([_p("error", f"{server.name} has no partitions")])
        notes.append(f"partition: {part.name} (default)"
                     + (f"; skipped {', '.join(skipped)} (no access)" if skipped else ""))
    else:
        part = server.partitions.get(partition)
        if part is None:
            raise SubmitError([_p("error", f"unknown partition {partition!r} on {server.name}; "
                                           f"known: {', '.join(server.partitions)}")])
    if cores is None:
        cores = min(DEFAULT_MAX_CORES, part.cores_per_node)
        notes.append(f"cores: {cores} (default)")
    if mem_gb is None:
        mem_gb = max(1, int(part.mem_per_node_gb * min(int(cores), part.cores_per_node) / part.cores_per_node * MEM_SHARE))
        notes.append(f"memory: {mem_gb} GB (default: ~90% of this core count's share of the node)")
    if walltime is None:
        walltime = format_walltime(min(DEFAULT_WALLTIME_S, part.max_walltime_seconds))
        notes.append(f"walltime: {walltime} (default)")
    gpus = int(gpus or 0)

    problems = check_resources(server, part.name, cores, mem_gb, walltime, gpus, software=software_key,
                               user=user, groups=groups)
    if any(p["severity"] == "error" for p in problems):
        raise SubmitError(problems)
    cores = int(cores)
    mem_gb = float(mem_gb)
    wall = format_walltime(parse_walltime(walltime))
    name = _job_name(job_name or PurePosixPath(input_file).stem, server.scheduler)

    workdir, jobid = _job_vars(server.scheduler)
    var, body = _body(sw, cores, gpus, input_file)
    exe = _python_for(sw) if var == "PYTHON" else sw.executable
    label = f"{sw.ess} {sw.version}" if sw.version else sw.ess
    lines = ["#!/bin/bash",
             f"# {software_key} ({label}) on {server.name}:{part.name}; generated by `rag-drg servers submit` "
             "from servers.yaml",
             *_header(server, part, sw, name, cores, mem_gb, wall, gpus),
             "",
             f"WORKDIR={workdir}",
             f"JOBID={jobid}",
             'cd "$WORKDIR"',
             "",
             f"# --- {software_key}: absolute paths, no environment modules ---"]
    lines += [f"export {k}={_sh(v)}" for k, v in sw.env.items()]
    lines += list(sw.setup)
    lines.append(f"{var}={exe}")
    lines += ["", "# --- per-job scratch" + (" (node-local)" if server.scratch.node_local else "") + " ---",
              _scratch_line(server), 'mkdir -p "$SCRATCH"', "", *body]
    script = "\n".join(lines).rstrip() + "\n"

    ess_lines = input_lines(sw, cores, mem_gb, gpus, input_file)
    notes.insert(0, "put in the input file: " + " | ".join(ess_lines))
    for p in problems:
        notes.append(f"{p['severity']}: {p['message']}")
    if not server.scratch.path:
        notes.append("warning: servers.yaml has no scratch path for this server; using ${TMPDIR:-/tmp}")
    submit_cmd = {"slurm": "sbatch", "local": "bash"}.get(server.scheduler, "qsub")
    notes.append(f"submit with: {submit_cmd} <this script>.sh")
    return script, notes
