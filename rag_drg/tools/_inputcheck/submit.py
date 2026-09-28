"""Slurm / PBS submit-script parsing and input <-> allocation cross-checks."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .common import fmt_mb, mem_to_mb
from .model import REF, Executable, Finding, ParsedInput, ParsedSubmit

SUBMIT_EXTS = {".sh", ".slurm", ".sbatch", ".pbs", ".qsub", ".job", ".sub", ".bash", ".submit", ""}
T = REF["templates"]
EXE_PROGRAM = {"g16": "gaussian", "g09": "gaussian", "g03": "gaussian", "orca": "orca", "qchem": "qchem",
               "molpro": "molpro", "psi4": "psi4", "python": "python", "python3": "python"}
LAUNCHERS = {"mpirun", "mpiexec", "srun", "orterun", "mpiexec.hydra"}
CMD_WRAPPERS = {"time", "nohup", "exec", "env", "stdbuf", "nice", "numactl", "taskset"}
# Command words whose arguments name a program without running it.
NOT_RUNNING = {"echo", "printf", "which", "type", "command", "ldd", "test", "[", "[[", "file", "ls", "cat", "cp",
               "mv", "rm", "mkdir", "cd", "source", ".", "export", "module", "ml", "if", "fi", "then", "else",
               "elif", "for", "while", "done", "do", "set", "unset", "trap", "tar", "gzip", "sed", "grep", "ulimit",
               "readlink", "realpath", "dirname", "basename", "stat", "chmod", "ln", "head", "tail", "local",
               "declare", "readonly", "alias", "hash"}
ESS_INPUT_EXTS = (".gjf", ".com", ".gau", ".inp", ".in", ".dat", ".py", ".qcin")
PROGRAM_EXES = {"gaussian": {"gaussian"}, "orca": {"orca"}, "qchem": {"qchem"}, "molpro": {"molpro"},
                "psi4": {"psi4", "python"}, "pyscf": {"python"}}


def _slurm_mem(v: str) -> float | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgtKMGT]?)[bB]?\s*", v)
    if not m:
        return None
    return mem_to_mb(float(m.group(1)), m.group(2) or "m")


def _pbs_mem(v: str) -> float | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgtKMGT]?)([bBwW]?)\s*", v)
    if not m:
        return None
    unit = (m.group(2) + (m.group(3) or "b")).lower()
    if unit == "b":
        return float(m.group(1)) / 2**20
    return mem_to_mb(float(m.group(1)), unit)


def _walltime(v: str, scheduler: str = "slurm") -> int | None:
    """Seconds. A bare number is minutes in Slurm (--time=90) but seconds in PBS (walltime=36000)."""
    v = v.strip()
    try:
        days = 0
        if "-" in v:
            d, v = v.split("-", 1)
            days = int(d)
            parts = [int(x) for x in v.split(":")]
            parts += [0] * (3 - len(parts))
            h, m, s = parts[:3]
            return days * 86400 + h * 3600 + m * 60 + s
        parts = [int(float(x)) for x in v.split(":")]
        if len(parts) == 1:
            return parts[0] if scheduler == "pbs" else parts[0] * 60
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def _split(s: str) -> list[str]:
    try:
        return shlex.split(s, comments=False)
    except ValueError:
        return s.split()


def parse_submit(content: str, filename: str | None = None, path: Path | None = None) -> ParsedSubmit:
    sub = ParsedSubmit(path=path, filename=filename, content=content, lines=content.splitlines())
    slurm: dict[str, str] = {}
    pbs_l: list[str] = []
    for k, ln in enumerate(sub.lines, 1):
        m = re.match(r"^#(SBATCH|PBS)\s+(.*)$", ln)
        if not m:
            continue
        sub.scheduler = sub.scheduler or ("slurm" if m.group(1) == "SBATCH" else "pbs")
        body = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
        sub.directives.append((k, body))
        args = _split(body)
        if m.group(1) == "SBATCH":
            i = 0
            while i < len(args):
                a = args[i]
                if a.startswith("--"):
                    if "=" in a:
                        key, val = a[2:].split("=", 1)
                    else:
                        key, val = a[2:], (args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "")
                        i += 1 if val else 0
                    slurm[key] = val
                elif a.startswith("-") and len(a) >= 2:
                    key = {"N": "nodes", "n": "ntasks", "c": "cpus-per-task", "t": "time", "p": "partition",
                           "G": "gpus", "J": "job-name", "o": "output", "e": "error", "q": "qos", "A": "account"}.get(a[1], a[1])
                    val = a[2:].lstrip("=") if len(a) > 2 else (args[i + 1] if i + 1 < len(args) else "")
                    if len(a) == 2:
                        i += 1
                    slurm[key] = val
                i += 1
        else:
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-l", "-q", "-N") and i + 1 < len(args):
                    if a == "-l":
                        pbs_l.append(args[i + 1])
                    elif a == "-q":
                        sub.partition = args[i + 1]
                    i += 2
                    continue
                if a.startswith("-l") and len(a) > 2:
                    pbs_l.append(a[2:])
                elif a.startswith("-q") and len(a) > 2:
                    sub.partition = a[2:]
                i += 1
    if sub.scheduler == "slurm":
        _fill_slurm(sub, slurm)
    elif sub.scheduler == "pbs":
        _fill_pbs(sub, pbs_l)
    _per_node(sub)
    _variables_and_commands(sub)
    return sub


def _int(v) -> int | None:
    m = re.match(r"^\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def _fill_slurm(sub: ParsedSubmit, o: dict[str, str]) -> None:
    sub.nodes = _int(o.get("nodes"))
    sub.ntasks = _int(o.get("ntasks"))
    sub.ntasks_per_node = _int(o.get("ntasks-per-node"))
    sub.cpus_per_task = _int(o.get("cpus-per-task"))
    tasks = sub.ntasks or ((sub.ntasks_per_node or 1) * (sub.nodes or 1) if sub.ntasks_per_node else 1)
    sub.mpi_tasks = tasks
    sub.total_cores = tasks * (sub.cpus_per_task or 1)
    if "mem" in o:
        sub.mem_mb = _slurm_mem(o["mem"])
        if sub.mem_mb is not None:
            sub.mem_total_mb = sub.mem_mb * (sub.nodes or 1)
    if "mem-per-cpu" in o:
        sub.mem_per_cpu_mb = _slurm_mem(o["mem-per-cpu"])
        if sub.mem_per_cpu_mb is not None and sub.mem_total_mb is None:
            sub.mem_total_mb = sub.mem_per_cpu_mb * sub.total_cores
    if "time" in o:
        sub.walltime = o["time"]
        sub.walltime_s = _walltime(o["time"])
    sub.partition = o.get("partition")
    g = None
    for key in ("gres",):
        if key in o:
            for part in o[key].split(","):
                m = re.match(r"^gpu(?::([A-Za-z][\w-]*))?(?::(\d+))?$", part.strip())
                if m:
                    g = (g or 0) + int(m.group(2) or 1) * (sub.nodes or 1)  # --gres is per node
    for key in ("gpus", "gpus-per-node", "gpus-per-task"):
        if key in o:
            m = re.search(r"(\d+)$", o[key])
            if m:
                mult = {"gpus-per-node": sub.nodes or 1, "gpus-per-task": tasks}.get(key, 1)
                g = int(m.group(1)) * mult
    sub.gpus = g


def _per_node(sub: ParsedSubmit) -> None:
    """Per-node request (node limits apply to this, not to the job total). Values already set
    (PBS select chunks) are kept."""
    nodes = max(sub.nodes or 1, 1)
    if sub.cores_per_node is None and sub.total_cores:
        if sub.scheduler == "slurm" and sub.ntasks_per_node:
            sub.cores_per_node = sub.ntasks_per_node * (sub.cpus_per_task or 1)
        else:
            sub.cores_per_node = -(-sub.total_cores // nodes)
    if sub.mem_per_node_mb is None:
        if sub.scheduler == "slurm" and sub.mem_mb is not None:
            sub.mem_per_node_mb = sub.mem_mb  # Slurm --mem is per node
        elif sub.scheduler == "slurm" and sub.mem_per_cpu_mb is not None and sub.cores_per_node:
            sub.mem_per_node_mb = sub.mem_per_cpu_mb * sub.cores_per_node
        elif sub.mem_total_mb is not None:
            sub.mem_per_node_mb = sub.mem_total_mb / nodes
    if sub.gpus_per_node is None and sub.gpus is not None:
        sub.gpus_per_node = -(-sub.gpus // nodes)


def _fill_pbs(sub: ParsedSubmit, specs: list[str]) -> None:
    items: list[str] = []
    for s in specs:
        # "select=1:ncpus=16:mem=64gb,walltime=24:00:00" -> split resources on commas not inside select
        items += [p for p in re.split(r",(?=\w+=)", s) if p]
    total_cores = mpi = mem = gpus = nodes = None
    for it in items:
        key, _, val = it.partition("=")
        key = key.strip().lower()
        if key == "select":
            total_cores = mpi = mem = gpus = nodes = 0
            mpi_seen = mem_seen = gpu_seen = False
            for chunk in val.split("+"):
                parts = chunk.split(":")
                n = _int(parts[0]) if parts and parts[0].strip().isdigit() else 1
                res = dict(p.split("=", 1) for p in parts if "=" in p)
                c = _int(res.get("ncpus")) or 1
                nodes += n
                total_cores += n * c
                if "mpiprocs" in res:
                    mpi += n * (_int(res["mpiprocs"]) or 0)
                    mpi_seen = True
                if "mem" in res:
                    mem += n * (_pbs_mem(res["mem"]) or 0)
                    mem_seen = True
                if "ngpus" in res:
                    gpus += n * (_int(res["ngpus"]) or 0)
                    gpu_seen = True
                # a chunk must fit on one node: keep the largest chunk
                sub.cores_per_node = max(sub.cores_per_node or 0, c)
                if "mem" in res and _pbs_mem(res["mem"]) is not None:
                    sub.mem_per_node_mb = max(sub.mem_per_node_mb or 0, _pbs_mem(res["mem"]))
                if "ngpus" in res:
                    sub.gpus_per_node = max(sub.gpus_per_node or 0, _int(res["ngpus"]) or 0)
            mpi = mpi if mpi_seen else None
            mem = mem if mem_seen else None
            gpus = gpus if gpu_seen else None
        elif key == "nodes":
            m = re.match(r"^(\d+)(?::ppn=(\d+))?", val)
            if m:
                nodes = int(m.group(1))
                total_cores = nodes * int(m.group(2) or 1)
                mpi = total_cores
                pg = re.search(r"gpus=(\d+)", val)
                if pg:
                    gpus = nodes * int(pg.group(1))
        elif key == "ncpus":
            total_cores = _int(val)
        elif key == "mem":
            mem = _pbs_mem(val)
        elif key == "walltime":
            sub.walltime = val
            sub.walltime_s = _walltime(val, "pbs")
        elif key == "ngpus":
            gpus = _int(val)
        elif key == "mpiprocs":
            mpi = _int(val)
    sub.nodes, sub.total_cores, sub.mpi_tasks, sub.mem_total_mb, sub.gpus = nodes, total_cores, mpi, mem, gpus
    sub.mem_mb = mem
    sub.ntasks = mpi
    if total_cores and nodes:
        sub.cpus_per_task = None


def _expand(s: str, env: dict[str, str], depth: int = 3) -> str:
    for _ in range(depth):
        new = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), s)
        if new == s:
            break
        s = new
    return s


def _variables_and_commands(sub: ParsedSubmit) -> None:
    env: dict[str, str] = {}
    if sub.mpi_tasks:
        env.update(SLURM_NTASKS=str(sub.mpi_tasks), SLURM_NPROCS=str(sub.mpi_tasks), PBS_NP=str(sub.mpi_tasks))
    if sub.cpus_per_task:
        env["SLURM_CPUS_PER_TASK"] = str(sub.cpus_per_task)
    if sub.total_cores and sub.scheduler == "pbs":
        env["NCPUS"] = str(sub.total_cores)
    for k, ln in enumerate(sub.lines, 1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(?:export\s+)?([A-Za-z_]\w*)=(.*)$", s)
        # `VAR=val cmd args` sets VAR for cmd only: that line is a command, parsed below.
        if m and (s.startswith("export") or len(_split(re.split(r"\s+#", s, maxsplit=1)[0])) <= 1
                  or re.match(r"^[A-Za-z_]\w*=\$\(", s)):
            val = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            val = re.sub(r"^\$\(\s*which\s+([\w.-]+)\s*\)$|^`which\s+([\w.-]+)`$",
                         lambda mm: "/WHICH/" + (mm.group(1) or mm.group(2)), val)
            env[m.group(1)] = _expand(val, env)
            sub.variables[m.group(1)] = env[m.group(1)]
            continue
        which = re.findall(r"\$\(\s*which\s+([\w.-]+)\s*\)|`which\s+([\w.-]+)`", s)
        line = re.sub(r"\$\(\s*which\s+([\w.-]+)\s*\)|`which\s+([\w.-]+)`", lambda mm: "/WHICH/" + (mm.group(1) or mm.group(2)), s)
        line = _expand(line, env)
        toks = _split(line.split(" #")[0])
        launcher = None
        # Only the command word runs a program: token 0 after VAR=val assignments, or the first
        # non-option word after a wrapper (time/nohup/exec/env) or an MPI launcher. `which orca`,
        # `ldd /opt/orca/orca`, `echo "running orca"`, `test -x .../orca` do not run it.
        idx, after_wrapper = 0, False
        while idx < len(toks):
            t = toks[idx]
            if t in ("<", "<<", "|", "||", "&&", ";", "&") or t.startswith((">", "2>", "&>", "1>")):
                idx = len(toks)
                break
            if re.match(r"^[A-Za-z_]\w*=", t) or (after_wrapper and (t.startswith("-") or t.isdigit())):
                idx += 1
                continue
            base = Path(t).name
            if base in LAUNCHERS:
                launcher, after_wrapper = base, True
                idx += 1
                continue
            if base in CMD_WRAPPERS:
                after_wrapper = True
                idx += 1
                continue
            break
        if idx >= len(toks) or Path(toks[idx]).name in NOT_RUNNING:
            continue
        # After an MPI launcher the program may follow options with values (-machinefile FILE),
        # so look further; otherwise only the command word itself counts.
        end = len(toks) if launcher else idx + 1
        for idx, t in list(enumerate(toks))[idx:end]:
            if t in ("<", "<<", "|", "||", "&&", ";", "&") or t.startswith((">", "2>", "&>", "1>")):
                break
            base = Path(t).name
            prog = EXE_PROGRAM.get(base)
            if prog is None and re.fullmatch(r"python3?(\.\d+)?", base):
                prog = "python"
            if prog is None:
                continue
            ex = Executable(program=prog, command=line, line=k, launcher=launcher,
                            absolute=("/" in t and not t.startswith(".")) or bool(which), args=toks[idx + 1:])
            a = ex.args
            for j, x in enumerate(a):
                nxt = a[j + 1] if j + 1 < len(a) else ""
                if prog == "qchem" and x == "-nt":
                    ex.threads = _int(nxt)
                elif prog == "qchem" and x == "-np":
                    ex.mpi = _int(nxt)
                elif prog == "molpro" and x in ("-n", "--tasks"):
                    ex.mpi = _int(nxt.split("/")[0]) if nxt else None
                elif prog == "molpro" and x.startswith("-n") and len(x) > 2 and x[2:].isdigit():
                    ex.mpi = int(x[2:])
                elif prog == "psi4" and x in ("-n", "--nthread"):
                    ex.threads = _int(nxt)
            sub.executables.append(ex)
            break


def references(sub: ParsedSubmit, filename: str) -> bool:
    """Does the script mention this input file (by name)?"""
    rx = re.compile(r"(?<![\w.-])" + re.escape(filename) + r"(?![\w-])")
    return any(rx.search(ln) for ln in sub.lines if not ln.lstrip().startswith("#"))


def referenced_inputs(sub: ParsedSubmit) -> list[str]:
    names = []
    for ln in sub.lines:
        if ln.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"([\w.\-]+(?:" + "|".join(re.escape(e) for e in ESS_INPUT_EXTS) + r"))(?![\w])", ln):
            names.append(m.group(1))
    return list(dict.fromkeys(names))


def find_submit_script(input_path: Path) -> Path | None:
    """A submit script in the input's directory that references the input by name (newest wins)."""
    d = input_path.parent
    hits = []
    try:
        candidates = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in SUBMIT_EXTS and p != input_path]
    except OSError:
        return None
    for p in candidates[:200]:
        try:
            if p.stat().st_size > 200_000:
                continue
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if not re.search(r"^#(SBATCH|PBS)\b", text, re.M):
            continue
        if references(parse_submit(text, p.name, p), input_path.name):
            hits.append(p)
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------------ checks


def check_submit_alone(sub: ParsedSubmit) -> list[Finding]:
    f: list[Finding] = []
    fn = sub.filename
    for ex in sub.executables:
        if ex.program == "orca":
            if ex.launcher:
                f.append(Finding("error", "orca-mpirun", f"ORCA is started through {ex.launcher}; ORCA launches its own "
                                 "MPI processes and must be called directly by its full path.", ex.line,
                                 fix="\"$ORCA_DIR/orca\" job.inp > job.out   (no mpirun/srun)",
                                 ref=REF["orca"] + "#Memory and cores (gotcha)", file=fn))
            elif not ex.absolute:
                f.append(Finding("warning", "orca-path", "ORCA is called without its absolute path; parallel ORCA runs "
                                 "need the full path to the orca binary.", ex.line,
                                 fix="ORCA_BIN=/abs/path/orca_6_0_x/orca; \"$ORCA_BIN\" job.inp > job.out",
                                 ref=REF["orca"] + "#Memory and cores (gotcha)", file=fn))
        if ex.threads and sub.total_cores and ex.threads > sub.total_cores:
            f.append(Finding("error", "threads-exceed", f"{ex.program} is started with {ex.threads} threads but the "
                             f"job has {sub.total_cores} cores.", ex.line, ref=T, file=fn))
        if ex.mpi and sub.total_cores and ex.mpi > sub.total_cores:
            f.append(Finding("error", "mpi-exceed", f"{ex.program} is started with {ex.mpi} processes but the job has "
                             f"{sub.total_cores} cores.", ex.line, ref=T, file=fn))
    return f


def _mem_check(f: list[Finding], what: str, need_mb: float | None, sub: ParsedSubmit, line: int | None, code: str,
               ref: str, fix: str, warn_frac: float = 0.9) -> None:
    alloc = sub.mem_total_mb
    if need_mb is None or not alloc:
        return
    if need_mb > alloc:
        f.append(Finding("error", code, f"{what} ({fmt_mb(need_mb)}) is more than the job's memory allocation "
                         f"({fmt_mb(alloc)}{' in ' + sub.filename if sub.filename else ''}).", line, fix=fix, ref=ref))
    elif need_mb > warn_frac * alloc:
        f.append(Finding("warning", code, f"{what} ({fmt_mb(need_mb)}) is {need_mb / alloc:.0%} of the allocation "
                         f"({fmt_mb(alloc)}); leave ~10-20% for the OS and the program itself.", line, fix=fix, ref=ref))


def cross_check(inp: ParsedInput, sub: ParsedSubmit) -> list[Finding]:
    f: list[Finding] = []
    prog = inp.program
    alloc = sub.mem_total_mb
    ess = [e for e in sub.executables if e.program != "python" or prog in ("psi4", "pyscf")]
    if prog and ess and not any(e.program in PROGRAM_EXES.get(prog, set()) for e in ess):
        f.append(Finding("warning", "submit-program", f"The submit script {sub.filename or ''} runs "
                         f"{', '.join(sorted({e.program for e in ess}))} but this is a {prog} input.", None,
                         ref=T))
    cores = sub.total_cores
    if prog == "gaussian":
        line = inp.line_of(r"^\s*%mem", re.I)
        tgt = alloc * 0.875 / 1024 if alloc else None
        _mem_check(f, "%mem", inp.memory_total_mb, sub, line, "gaussian-mem-alloc", REF["gaussian"] + "#Memory and cores (gotcha)",
                   f"%mem={int(tgt)}GB" if tgt and tgt >= 1 else "lower %mem to ~85-90% of the allocation")
        if inp.nprocs and cores:
            lp = inp.line_of(r"^\s*%(nprocshared|nproc|cpu)", re.I)
            if inp.nprocs > cores:
                f.append(Finding("error", "gaussian-nproc-alloc", f"The input uses {inp.nprocs} cores but the job only "
                                 f"has {cores} (cpus-per-task x tasks).", lp, fix=f"%nprocshared={cores} or request "
                                 f"--cpus-per-task={inp.nprocs}", ref=REF["gaussian"] + "#Memory and cores (gotcha)"))
            elif inp.nprocs < cores:
                f.append(Finding("info", "gaussian-nproc-alloc", f"The input uses {inp.nprocs} of the {cores} cores "
                                 "requested.", lp, ref=REF["gaussian"] + "#Memory and cores (gotcha)"))
        if inp.gpus and (sub.gpus or 0) < inp.gpus:
            # An unparsed GPU request (unusual syntax) must not become a blocking error.
            unparsed = sub.gpus is None and any(re.search(r"gpu", d, re.I) for _, d in sub.directives)
            f.append(Finding("info" if unparsed else "error", "gaussian-gpu-alloc", f"%gpucpu uses {inp.gpus} GPU(s) but the job requests "
                             f"{sub.gpus or 0}.", inp.line_of(r"^\s*%gpucpu", re.I), fix=f"#SBATCH --gres=gpu:{inp.gpus}",
                             ref=REF["gaussian"] + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
        elif sub.gpus and not inp.gpus:
            f.append(Finding("info", "gaussian-gpu-unused", f"The job requests {sub.gpus} GPU(s) but the input has "
                             "no %gpucpu, so Gaussian will not use them.", None,
                             ref=REF["gaussian"] + "#GPUs (G16 GPU build only; G09 has no GPU support)"))
    elif prog == "orca":
        n = inp.nprocs or 1
        lp = inp.line_of(r"^\s*%pal|^\s*!.*\bpal\d+", re.I)
        if cores and n > cores:
            f.append(Finding("error", "orca-nprocs-alloc", f"ORCA runs {n} processes but the job has {cores} cores.", lp,
                             fix=f"#SBATCH --ntasks={n} (or %pal nprocs {cores} end)", ref=REF["orca"] + "#Memory and cores (gotcha)"))
        elif sub.mpi_tasks and n != sub.mpi_tasks:
            f.append(Finding("warning", "orca-nprocs-alloc", f"ORCA nprocs = {n} but the job requests {sub.mpi_tasks} "
                             f"MPI task(s); ORCA is MPI-parallel, request --ntasks={n} (cpus-per-task=1).", lp,
                             fix=f"#SBATCH --ntasks={n} --cpus-per-task=1", ref=REF["orca"] + "#Memory and cores (gotcha)"))
        if inp.memory_per_core_mb:
            per_core = alloc / n if alloc else None
            _mem_check(f, f"%maxcore x nprocs = {inp.memory_per_core_mb:.0f} MB x {n}", inp.memory_per_core_mb * n, sub,
                       inp.line_of(r"^\s*%maxcore", re.I), "orca-maxcore-alloc", REF["orca"] + "#Memory and cores (gotcha)",
                       f"%maxcore {int(per_core * 0.75)}  (75% of memory per core)" if per_core else "lower %maxcore")
        for ex in sub.executables:
            if ex.program == "orca" and not ex.absolute and not ex.launcher and n > 1:
                f.append(Finding("error", "orca-path", f"Parallel ORCA ({n} processes) must be called by its absolute "
                                 "path.", ex.line, fix="\"/abs/path/orca_6_0_x/orca\" job.inp > job.out",
                                 ref=REF["orca"] + "#Memory and cores (gotcha)", file=sub.filename))
    elif prog == "qchem":
        _mem_check(f, "MEM_TOTAL", inp.memory_total_mb, sub, inp.line_of(r"^\s*mem_total", re.I), "qchem-mem-alloc",
                   REF["qchem"] + "#Memory and cores (gotcha)",
                   f"MEM_TOTAL {int(alloc * 0.875)}" if alloc else "lower MEM_TOTAL")
        for ex in sub.executables:
            if ex.program == "qchem" and ex.threads and cores and ex.threads < cores:
                f.append(Finding("info", "qchem-nt", f"qchem -nt {ex.threads} uses {ex.threads} of the {cores} cores.",
                                 ex.line, ref=REF["qchem"] + "#Memory and cores (gotcha)", file=sub.filename))
    elif prog == "molpro":
        n = None
        for ex in sub.executables:
            if ex.program == "molpro":
                n = ex.mpi
        n = n or sub.mpi_tasks or 1
        if inp.memory_per_process_mb:
            inp.memory_total_mb = inp.memory_per_process_mb * n
            per = alloc / n if alloc else None
            _mem_check(f, f"memory per process x {n} processes", inp.memory_per_process_mb * n, sub,
                       inp.line_of(r"^\s*memory\s*,", re.I), "molpro-mem-alloc", REF["molpro"] + "#Memory (gotcha)",
                       f"memory,{int(per * 0.85 * 2**20 / 8 / 1e6)},m  (mega-words per process)" if per else "lower memory")
    elif prog in ("psi4", "pyscf"):
        what = "psi4 memory" if prog == "psi4" else "mol.max_memory"
        before = len(f)
        _mem_check(f, what, inp.memory_total_mb, sub, None, f"{prog}-mem-alloc", REF[prog],
                   f"~{fmt_mb(alloc * 0.875)}" if alloc else "lower it")
        if prog == "pyscf":
            for x in f[before:]:
                x.severity = "warning" if x.severity == "error" else "info"
        if inp.nprocs and cores and inp.nprocs > cores:
            f.append(Finding("error", "threads-exceed", f"{inp.nprocs} threads requested in the script but the job "
                             f"has {cores} cores.", inp.line_of(r"num_threads"), ref=REF[prog]))
    return f
