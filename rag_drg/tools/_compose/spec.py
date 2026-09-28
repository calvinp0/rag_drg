"""The composer's job specification: dataclass, dict/YAML form, protocol merging, validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields

PROGRAMS = ("orca", "gaussian", "qchem", "psi4", "molpro", "pyscf")
JOBS = ("sp", "opt", "freq", "opt+freq", "ts", "irc")
JOB_ALIASES = {"energy": "sp", "single_point": "sp", "singlepoint": "sp", "optfreq": "opt+freq",
               "opt_freq": "opt+freq", "opt-freq": "opt+freq", "optimization": "opt", "frequency": "freq",
               "frequencies": "freq", "optts": "ts", "tsopt": "ts"}
PROGRAM_ALIASES = {"g09": "gaussian", "g16": "gaussian", "gaussian16": "gaussian", "gaussian09": "gaussian",
                   "q-chem": "qchem", "orca6": "orca", "orca5": "orca"}
SCF_CONVERGENCE = ("loose", "normal", "tight", "verytight")
SOLVATION_MODELS = ("smd", "pcm", "cpcm")
MOLECULE_KEYS = ("xyz", "xyz_file", "smiles")
RESOURCE_KEYS = ("server", "software", "cores", "mem_gb", "walltime", "partition")


class ComposeError(ValueError):
    """The spec cannot be turned into an input. `.errors` lists every problem found."""

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class Spec:
    program: str
    job: str
    method: str
    basis: str | None = None
    version: str | None = None
    dispersion: str | None = None
    solvation: dict | None = None          # {model: smd|pcm|cpcm, solvent: water}
    scf: dict = field(default_factory=dict)  # {convergence: tight, max_iter: 200}
    grid: str | None = None
    extra_keywords: list = field(default_factory=list)
    charge: int = 0
    multiplicity: int | None = None
    molecule: dict = field(default_factory=dict)   # {xyz: text} | {xyz_file: path} | {smiles: S}
    resources: dict = field(default_factory=dict)  # {server, software, cores, mem_gb, walltime, partition}
    name: str | None = None
    allow_unverified: bool = False


SPEC_KEYS = tuple(f.name for f in fields(Spec))


def deep_merge(base, over):
    """`over` wins; dicts are merged recursively, everything else is replaced. An explicit None
    (YAML `null`) in `over` removes the key, so a step can switch off e.g. a protocol's dispersion."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = deep_merge(out.get(k), v)
        return out
    return copy.deepcopy(over) if over is not None else copy.deepcopy(base)


def apply_protocol(spec: dict, protocol: dict | None, step: str | None) -> tuple[dict, list[str]]:
    """Merge a protocol (defaults + optional named `steps`) UNDER the explicit spec.

    Precedence, lowest first: protocol top level < protocol `steps[step]` < `spec`.
    Returns (merged dict, notes)."""
    notes: list[str] = []
    if not protocol:
        if step:
            raise ComposeError(f"--step {step!r} given without a protocol")
        return dict(spec), notes
    if not isinstance(protocol, dict):
        raise ComposeError("a protocol must be a mapping (YAML dict)")
    base = {k: v for k, v in protocol.items() if k not in ("steps", "description", "protocol")}
    steps = protocol.get("steps") or {}
    if not isinstance(steps, dict):
        raise ComposeError("protocol `steps` must be a mapping of step name -> fields")
    if step:
        if step not in steps:
            raise ComposeError(f"protocol has no step {step!r}; steps: {', '.join(map(str, steps)) or '(none)'}")
        base = deep_merge(base, steps[step] or {})
        notes.append(f"protocol step {step!r} merged under the explicit arguments")
    elif steps:
        notes.append(f"protocol has steps ({', '.join(map(str, steps))}) but none was selected; "
                     "only its top-level defaults were used")
    return deep_merge(base, spec), notes


def _as_int(v, what: str, errors: list[str]) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        errors.append(f"{what} must be an integer, got {v!r}")
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        errors.append(f"{what} must be an integer, got {v!r}")
        return None
    if not f.is_integer():
        errors.append(f"{what} must be an integer, got {v!r}")
        return None
    return int(f)


def spec_from_dict(d: dict) -> Spec:
    """Validate a dict (from YAML / JSON / CLI) and build a Spec. Collects all problems at once."""
    if not isinstance(d, dict):
        raise ComposeError("the spec must be a mapping")
    errors: list[str] = []
    unknown = sorted(set(d) - set(SPEC_KEYS))
    if unknown:
        errors.append(f"unknown spec field(s): {', '.join(unknown)} (known: {', '.join(SPEC_KEYS)})")

    program = str(d.get("program") or "").strip().lower()
    program = PROGRAM_ALIASES.get(program, program)
    if program not in PROGRAMS:
        errors.append(f"program must be one of {', '.join(PROGRAMS)}; got {d.get('program')!r}")
    job = str(d.get("job") or "").strip().lower()
    job = JOB_ALIASES.get(job, job)
    if job not in JOBS:
        errors.append(f"job must be one of {', '.join(JOBS)}; got {d.get('job')!r}")
    method = str(d.get("method") or "").strip()
    if not method:
        errors.append("method is required (e.g. B3LYP, wB97X-D3, DLPNO-CCSD(T))")
    basis = d.get("basis")
    basis = str(basis).strip() if basis not in (None, "") else None
    version = d.get("version")
    version = str(version).strip() if version not in (None, "") else None

    solv = d.get("solvation")
    if solv in (None, "", {}):
        solv = None
    elif isinstance(solv, dict):
        model = str(solv.get("model") or "").lower().replace("-", "")
        solvent = str(solv.get("solvent") or "").strip()
        if model not in SOLVATION_MODELS:
            errors.append(f"solvation.model must be one of {', '.join(SOLVATION_MODELS)}; got {solv.get('model')!r}")
        if not solvent:
            errors.append("solvation.solvent is required (e.g. water)")
        extra = set(solv) - {"model", "solvent"}
        if extra:
            errors.append(f"unknown solvation field(s): {', '.join(sorted(extra))}")
        solv = {"model": model, "solvent": solvent}
    else:
        errors.append("solvation must be a mapping {model: smd|pcm|cpcm, solvent: NAME}")
        solv = None

    scf = d.get("scf") or {}
    if not isinstance(scf, dict):
        errors.append("scf must be a mapping {convergence: tight, max_iter: 200}")
        scf = {}
    else:
        extra = set(scf) - {"convergence", "max_iter"}
        if extra:
            errors.append(f"unknown scf field(s): {', '.join(sorted(extra))}")
        conv = scf.get("convergence")
        if conv is not None:
            conv = str(conv).lower().replace("-", "").replace("_", "")
            if conv not in SCF_CONVERGENCE:
                errors.append(f"scf.convergence must be one of {', '.join(SCF_CONVERGENCE)}; got {scf['convergence']!r}")
        mi = _as_int(scf.get("max_iter"), "scf.max_iter", errors)
        if mi is not None and mi < 1:
            errors.append("scf.max_iter must be >= 1")
        scf = {k: v for k, v in (("convergence", conv), ("max_iter", mi)) if v is not None}

    extra_kw = d.get("extra_keywords") or []
    if isinstance(extra_kw, str):
        extra_kw = [extra_kw]
    elif isinstance(extra_kw, dict):
        extra_kw = [f"{k} {v}" for k, v in extra_kw.items()]
    elif not isinstance(extra_kw, list):
        errors.append("extra_keywords must be a string, a list of strings or a mapping")
        extra_kw = []
    extra_kw = [str(x) for x in extra_kw if str(x).strip()]

    charge = _as_int(d.get("charge", 0) if d.get("charge") is not None else 0, "charge", errors)
    mult = _as_int(d.get("multiplicity"), "multiplicity", errors)
    if mult is not None and mult < 1:
        errors.append(f"multiplicity must be >= 1 (2S+1), got {mult}")

    mol = d.get("molecule") or {}
    if isinstance(mol, str):
        mol = {"xyz": mol}
    if not isinstance(mol, dict):
        errors.append("molecule must be a mapping with one of: xyz (text), xyz_file (path, local CLI only), smiles")
        mol = {}
    extra = set(mol) - set(MOLECULE_KEYS)
    if extra:
        errors.append(f"unknown molecule field(s): {', '.join(sorted(extra))} (use xyz, xyz_file or smiles)")
    given = [k for k in MOLECULE_KEYS if mol.get(k)]
    if len(given) != 1:
        errors.append("molecule needs exactly one of: xyz (text), xyz_file (path, local CLI only), smiles"
                      + (f"; got {', '.join(given)}" if given else ""))

    res = d.get("resources") or {}
    if not isinstance(res, dict):
        errors.append("resources must be a mapping {server, software, cores, mem_gb, walltime, partition}")
        res = {}
    extra = set(res) - set(RESOURCE_KEYS)
    if extra:
        errors.append(f"unknown resources field(s): {', '.join(sorted(extra))} (known: {', '.join(RESOURCE_KEYS)})")
    res = dict(res)
    if res.get("cores") is not None:
        res["cores"] = _as_int(res["cores"], "resources.cores", errors)
        if res["cores"] is not None and res["cores"] < 1:
            errors.append("resources.cores must be >= 1")
    if res.get("mem_gb") is not None:
        try:
            res["mem_gb"] = float(res["mem_gb"])
            if res["mem_gb"] <= 0:
                errors.append("resources.mem_gb must be > 0")
        except (TypeError, ValueError):
            errors.append(f"resources.mem_gb must be a number (GB), got {res['mem_gb']!r}")
    if not res.get("server") and (res.get("cores") is None or res.get("mem_gb") is None):
        errors.append("resources: give a server from servers.yaml (then a submit script is rendered), or "
                      "cores + mem_gb for a standalone input (no submit script)")
    if not res.get("server"):
        for k in ("software", "walltime", "partition"):
            if res.get(k):
                errors.append(f"resources.{k} only makes sense with resources.server")

    name = d.get("name")
    if name is not None:
        name = str(name).strip() or None
    allow = d.get("allow_unverified", False)
    if not isinstance(allow, bool):
        errors.append("allow_unverified must be true or false")
        allow = False
    for k in ("dispersion", "grid"):
        if d.get(k) is not None and not isinstance(d.get(k), str):
            errors.append(f"{k} must be a string")

    if errors:
        raise ComposeError(errors)
    return Spec(program=program, job=job, method=method, basis=basis, version=version,
                dispersion=(d.get("dispersion") or None), solvation=solv, scf=scf, grid=(d.get("grid") or None),
                extra_keywords=extra_kw, charge=charge or 0, multiplicity=mult, molecule=dict(mol),
                resources=res, name=name, allow_unverified=allow)
