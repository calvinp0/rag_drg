"""Data classes shared by the input checker and its extension checks (``EXTRA_CHECKS``)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

SEVERITIES = ("error", "warning", "info")

# Knowledge cards that explain the checks (paths relative to knowledge/, usable with read_document).
REF = {
    "gaussian": "ess/gaussian/gaussian-essentials.md",
    "orca": "ess/orca/orca-essentials.md",
    "qchem": "ess/qchem/qchem-essentials.md",
    "molpro": "ess/molpro/molpro-essentials.md",
    "psi4": "ess/psi4/psi4-essentials.md",
    "pyscf": "ess/pyscf/pyscf-essentials.md",
    "capabilities": "ess/capabilities.md",
    "levels": "ess/levels_of_theory.yaml",
    "templates": "hpc/templates/",
}


@dataclass
class Finding:
    """One problem found in an input or submit script.

    severity: "error" (will fail or silently give wrong results), "warning" (very likely a
        mistake), "info" (worth a look; the checker is not sure).
    code:     short stable id, e.g. "orca-maxcore-missing", "parity".
    message:  human-readable explanation.
    line:     1-based line number in `file` (None if not tied to a line).
    fix:      suggested fix, if any.
    ref:      knowledge card (path under knowledge/, optionally "#Section") explaining the rule.
    file:     file name the line refers to (input or submit script); None = the input.
    """

    severity: str
    code: str
    message: str
    line: int | None = None
    fix: str | None = None
    ref: str | None = None
    file: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        where = f"{self.file or ''}{':' if self.file and self.line else ''}{self.line or ''}"
        s = f"[{self.severity}] {self.code}" + (f" ({where})" if where else "") + f": {self.message}"
        if self.fix:
            s += f"\n    fix: {self.fix}"
        if self.ref:
            s += f"\n    see: {self.ref}"
        return s


@dataclass
class Atom:
    symbol: str                   # normalised element symbol ("C", "Cl"); "" for dummies/ghosts
    xyz: tuple[float, float, float] | None = None   # Cartesian coordinates in `ParsedInput.units`
    line: int | None = None       # 1-based line in the input
    ghost: bool = False           # ghost / dummy / point charge: no electrons, skipped in checks
    label: str = ""               # the label as written ("C1", "H:", "Bq")


@dataclass
class ParsedInput:
    """What the checker understood from an ESS input. Every field may be None/empty when the
    parser could not determine it; extension checks must tolerate that.

    program:        "gaussian" | "orca" | "qchem" | "molpro" | "psi4" | "pyscf" | None
                    (None = only a submit script is being checked)
    filename:       basename of the input (e.g. "job.inp"); path: full path in file mode
    content/lines:  the raw text and its lines
    charge, multiplicity: as written (multiplicity 2S+1); spin_2s: Molpro/PySCF native 2S
    atoms:          parsed atoms (see Atom); geometry_complete False when the geometry comes from a
                    file / checkpoint / Z-matrix we could not fully read (electron checks skipped)
    units:          "angstrom" | "bohr" (for `atoms`)
    method, basis:  main method/functional and orbital basis as written; methods: all method-like
                    tokens seen; aux_basis: auxiliary basis names (ORCA /C, /J ...)
    job_type:       normalised-ish job type ("sp", "opt", "ts", "freq", "irc", ...)
    memory_total_mb:     total memory the program will use (Gaussian %mem, Q-Chem MEM_TOTAL,
                         Psi4 memory, PySCF max_memory, ORCA maxcore*nprocs, Molpro mem*n if n known)
    memory_per_core_mb:  ORCA %maxcore; memory_per_process_mb: Molpro `memory` card (MB per process)
    nprocs:         cores/threads/processes requested in the input (Gaussian %nprocshared or %cpu
                    count, ORCA %pal/PALn, Psi4 set_num_threads)
    gpus:           Gaussian %gpucpu GPU count
    jobs:           number of chained jobs (Gaussian --Link1--, Q-Chem @@@)
    extra:          program-specific details (e.g. "link0", "route", "rem", "blocks")
    """

    program: str | None = None
    filename: str | None = None
    path: Path | None = None
    content: str = ""
    lines: list[str] = field(default_factory=list)
    charge: int | None = None
    multiplicity: int | None = None
    spin_2s: int | None = None
    atoms: list[Atom] = field(default_factory=list)
    geometry_complete: bool = False
    units: str = "angstrom"
    method: str | None = None
    methods: list[str] = field(default_factory=list)
    basis: str | None = None
    aux_basis: list[str] = field(default_factory=list)
    job_type: str | None = None
    memory_total_mb: float | None = None
    memory_per_core_mb: float | None = None
    memory_per_process_mb: float | None = None
    nprocs: int | None = None
    gpus: int | None = None
    jobs: int = 1
    extra: dict = field(default_factory=dict)

    def line_of(self, pattern: str, flags: int = 0) -> int | None:
        """1-based line of the first regex match (or None)."""
        import re

        rx = re.compile(pattern, flags)
        for i, ln in enumerate(self.lines, 1):
            if rx.search(ln):
                return i
        return None


@dataclass
class Executable:
    """A program invocation found in a submit script."""

    program: str                  # "gaussian" | "orca" | "qchem" | "molpro" | "psi4" | "python" | ...
    command: str                  # the (variable-expanded) command line
    line: int                     # 1-based line in the script
    absolute: bool = False        # called by absolute path (or $(which ...))
    launcher: str | None = None   # "mpirun" / "mpiexec" / "srun" if it is started through one
    threads: int | None = None    # qchem -nt / psi4 -n
    mpi: int | None = None        # qchem -np / molpro -n
    args: list[str] = field(default_factory=list)


@dataclass
class ParsedSubmit:
    """Resources requested by a Slurm (#SBATCH) or PBS (#PBS) script.

    scheduler:       "slurm" | "pbs" | None
    nodes, ntasks, cpus_per_task:   as requested (Slurm; for PBS select= chunks -> nodes,
                                    mpiprocs -> ntasks, ncpus -> cpus per chunk)
    total_cores:     cores the job gets in total; mpi_tasks: MPI ranks (Slurm ntasks, PBS mpiprocs)
    mem_mb:          memory per node (--mem / PBS mem per chunk); mem_per_cpu_mb: --mem-per-cpu
    mem_total_mb:    total memory of the allocation (what inputs are compared with)
    walltime:        as written; walltime_s: seconds
    partition:       Slurm partition / PBS queue
    gpus:            GPUs requested (--gres=gpu:N, --gpus, ngpus=)
    executables:     program invocations found (see Executable)
    variables:       simple VAR=value assignments in the script
    directives:      raw scheduler directives [(line, text)]
    """

    path: Path | None = None
    filename: str | None = None
    content: str = ""
    lines: list[str] = field(default_factory=list)
    scheduler: str | None = None
    nodes: int | None = None
    ntasks: int | None = None
    ntasks_per_node: int | None = None
    cpus_per_task: int | None = None
    total_cores: int | None = None
    mpi_tasks: int | None = None
    mem_mb: float | None = None
    mem_per_cpu_mb: float | None = None
    mem_total_mb: float | None = None
    walltime: str | None = None
    walltime_s: int | None = None
    partition: str | None = None
    gpus: int | None = None
    executables: list[Executable] = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    directives: list[tuple[int, str]] = field(default_factory=list)
