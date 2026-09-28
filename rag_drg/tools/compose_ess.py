"""Compose a validated ESS input (+ matching submit script) from an explicit job spec.

    rag-drg compose SPEC.yaml [--protocol P.yaml --step sp] [--out-dir DIR] [--json]
    rag-drg compose --program orca --job sp --method DLPNO-CCSD(T) --basis cc-pVTZ \\
        --charge 0 --mult 2 --xyz radical.xyz --server example --cores 16 --mem 64 --out-dir runs/

MCP tool: ``compose_ess_job(spec, protocol=None, step=None)`` (xyz TEXT only; the shared server
never reads files named in a request).

Python API::

    from rag_drg.tools.compose_ess import compose_ess_job
    res = compose_ess_job({"program": "orca", "job": "opt+freq", "method": "wB97X-D3", "basis": "def2-TZVP",
                           "charge": 0, "multiplicity": 1, "molecule": {"xyz": xyz_text},
                           "resources": {"server": "zeus", "cores": 16, "mem_gb": 64}})
    res["ok"], res["input_name"], res["input_text"], res["submit_name"], res["submit_text"], res["notes"]

Nothing project-specific lives here: the spec is explicit, or merged over a protocol YAML the
user passes from their own project repository (see docs/compose.md). Methods are resolved via
the levels-of-theory table, basis sets via the Basis Set Exchange, memory/cores lines and the
submit script via `rag_drg.tools.servers`, and every composed input is run through
`check_input`; any error there means no files are returned.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from ._compose.molecule import Molecule, build_molecule
from ._compose.spec import JOBS, PROGRAMS, ComposeError, Spec, apply_protocol, spec_from_dict
from ._compose.theory import (CLASS_LABEL, GEOMETRY_JOBS, IMPLEMENTED, PROGRAM_JOBS, WHY_NO_JOB, jobs_for,
                              resolve_basis, resolve_dispersion, resolve_method, resolve_solvation)
from ._compose.writers import EXTENSIONS, WRITERS, Job

__all__ = ["compose_ess_job", "ComposeError", "Spec", "spec_from_dict", "apply_protocol", "supported_matrix"]

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# ------------------------------------------------------------------ helpers

def _load_cfg(cfg):
    if cfg is not None:
        return cfg
    from ..config import load_config

    return load_config()


def _job_name(spec: Spec, mol: Molecule) -> str:
    raw = spec.name or f"{mol.formula}_{spec.job.replace('+', '_')}"
    n = _NAME_RE.sub("_", raw).strip("._-") or "job"
    if not n[0].isalnum():
        n = "j" + n
    return n[:60]


def _version_notes(program: str, version: str | None, allow: bool) -> list[str]:
    v = (version or "").lower().lstrip("g").strip()
    if program == "orca":
        if not v:
            return ["ORCA version not given: ORCA 5/6 syntax written (DefGrid, RIJCOSX defaults)"]
        if v.split(".")[0] not in ("5", "6"):
            if not allow:
                raise ComposeError(f"ORCA {version}: the ORCA card covers ORCA 5 and 6 only (grid keywords and RI "
                                   "defaults changed in 5); set allow_unverified: true to write ORCA 5/6 syntax anyway")
            return [f"UNVERIFIED: ORCA {version} given, ORCA 5/6 syntax written"]
    elif program == "gaussian":
        if not v:
            return ["Gaussian version not given: G16 defaults assumed (UltraFine grid is the G16 default, "
                    "G09 uses FineGrid)"]
        if v not in ("09", "9", "16"):
            if not allow:
                raise ComposeError(f"Gaussian {version}: the Gaussian card covers G09 and G16 only")
            return [f"UNVERIFIED: Gaussian {version} given, G16 syntax written"]
    elif program == "qchem" and v and not v.startswith("6"):
        return [f"Q-Chem {version}: the Q-Chem card describes 6.1; check keywords for your version"]
    elif program == "molpro" and v and v.split(".")[0] not in ("2024", "2026"):
        return [f"Molpro {version}: the Molpro card describes 2024/2026; check the manual for your version"]
    return []


def _software_key(server, program: str, version: str | None, explicit: str | None) -> str:
    if explicit:
        sw = server.software.get(explicit)
        if sw is None:
            raise ComposeError(f"software {explicit!r} is not installed on {server.name}; known: "
                               f"{', '.join(server.software) or '(none)'}")
        if sw.ess != program:
            raise ComposeError(f"software {explicit!r} on {server.name} is {sw.ess}, not {program}")
        return explicit
    cands = [k for k, sw in server.software.items() if sw.ess == program and "gpu" not in k.lower().split("-")]
    if not cands:
        raise ComposeError(f"no {program} install on {server.name} in servers.yaml (known: "
                           f"{', '.join(server.software) or '(none)'})")
    if version:
        v = str(version).lower().lstrip("g")

        def match(k: str) -> bool:
            sw = server.software[k]
            toks = [t.lstrip("g") for t in re.split(r"[-_]", k.lower())]
            sv = str(sw.version or "").lower()
            return v in toks or sv == v or sv.startswith(v + ".")

        hits = [k for k in cands if match(k)]
        if not hits:
            raise ComposeError(f"no {program} {version} on {server.name}; installs: "
                               + ", ".join(f"{k} ({server.software[k].version or '?'})" for k in cands))
        cands = hits
    if len(cands) > 1:
        raise ComposeError(f"{server.name} has several {program} installs ({', '.join(cands)}); give `version` or "
                           "resources.software")
    return cands[0]


def _ess_lines(sw, cores: int, mem_gb: float, input_name: str) -> list[str]:
    from ._servers.submit import input_lines

    return [ln for ln in input_lines(sw, cores, mem_gb, 0, input_name) if not ln.startswith("(")]


def _resources(spec: Spec, cfg, input_name: str, job_name: str) -> dict:
    """cores, mem_gb, in-input memory/cores lines, and (with a server) the submit script."""
    from ._servers.model import ServersConfigError, SoftwareInstall, load_servers
    from ._servers.submit import SubmitError, render_submit_script

    r = spec.resources
    notes: list[str] = []
    if not r.get("server"):
        cores, mem_gb = int(r["cores"]), float(r["mem_gb"])
        sw = SoftwareInstall(key=spec.program, ess=spec.program, executable=spec.program,
                             parallel="mpi" if spec.program in ("orca", "molpro") else "threads")
        run = {"orca": "call ORCA by its absolute path (never through mpirun)",
               "qchem": f"qchem -nt {cores} {input_name} out", "psi4": f"psi4 -n {cores} {input_name}",
               "molpro": f"molpro -n {cores} {input_name}", "gaussian": "g16 < input > log",
               "pyscf": f"python {input_name}"}[spec.program]
        notes.append(f"standalone input (no server): {cores} cores, {mem_gb:g} GB; no submit script. Run: {run}")
        return {"cores": cores, "mem_gb": mem_gb, "lines": _ess_lines(sw, cores, mem_gb, input_name),
                "submit_name": None, "submit_text": None, "software": None, "notes": notes}
    try:
        servers = load_servers(cfg)
    except ServersConfigError as e:
        raise ComposeError(f"servers.yaml is invalid: {e}") from None
    name = r["server"]
    if name not in servers:
        raise ComposeError(f"unknown server {name!r}; known: {', '.join(servers) or '(none: no servers.yaml)'}")
    server = servers[name]
    key = _software_key(server, spec.program, spec.version, r.get("software"))
    try:
        script, snotes = render_submit_script(server, key, input_name, job_name=job_name, cores=r.get("cores"),
                                              mem_gb=r.get("mem_gb"), walltime=r.get("walltime"),
                                              partition=r.get("partition"))
    except SubmitError as e:
        raise ComposeError([f"submit script: {p['message']}" for p in e.problems if p["severity"] == "error"]
                           or [str(e)]) from None
    cores, mem_gb = r.get("cores"), r.get("mem_gb")
    for n in snotes:
        m = re.match(r"cores: (\d+) \(default\)", n)
        if m and cores is None:
            cores = int(m.group(1))
        m = re.match(r"memory: ([\d.]+) GB \(default", n)
        if m and mem_gb is None:
            mem_gb = float(m.group(1))
    if cores is None or mem_gb is None:
        from ._inputcheck.submit import parse_submit

        sub = parse_submit(script, "submit.sh", None)
        cores = cores or sub.total_cores
        mem_gb = mem_gb or (sub.mem_total_mb / 1024 if sub.mem_total_mb else None)
    if not cores or not mem_gb:
        raise ComposeError("internal: could not read cores/memory back from the rendered submit script")
    sw = server.software[key]
    lines = _ess_lines(sw, int(cores), float(mem_gb), input_name)
    suggested = snotes[0] if snotes and snotes[0].startswith("put in the input file:") else ""
    for ln in lines:
        if suggested and ln not in suggested:
            notes.append(f"warning: '{ln}' is not among the submit script's suggestions ({suggested})")
    notes += [f"submit script: {n}" for n in snotes[1:]]
    notes.insert(0, f"server {name}: software {key} ({sw.ess} {sw.version or ''}".rstrip() + f"), {cores} cores, "
                 f"{mem_gb:g} GB; input memory/cores lines come from the same render_submit_script call")
    return {"cores": int(cores), "mem_gb": float(mem_gb), "lines": lines, "submit_name": f"{Path(input_name).stem}.sh",
            "submit_text": script, "software": key, "notes": notes}


# ------------------------------------------------------------------ main entry point

def compose_ess_job(spec: dict, protocol: dict | None = None, step: str | None = None, *, cfg=None,
                    allow_files: bool = False) -> dict:
    """Compose an input (and submit script when a server is given) from an explicit spec.

    `protocol` is a dict (a parsed protocol YAML) merged UNDER `spec`; `step` selects one of its
    `steps`. `allow_files=True` (local CLI / Python only) lets `molecule.xyz_file` be read.

    Returns {"ok", "input_name", "input_text", "submit_name", "submit_text", "findings", "notes",
    "errors", "spec"}. On any problem `ok` is False, `errors` says why and no file text is returned.
    """
    out = {"ok": False, "input_name": None, "input_text": None, "submit_name": None, "submit_text": None,
           "findings": [], "notes": [], "errors": [], "spec": None}
    notes: list[str] = out["notes"]
    try:
        cfg = _load_cfg(cfg)
        merged, pnotes = apply_protocol(spec or {}, protocol, step)
        notes += pnotes
        mol_spec = dict(merged.get("molecule") or {}) if isinstance(merged.get("molecule"), dict) else merged.get("molecule")
        if isinstance(mol_spec, dict) and mol_spec.get("xyz_file"):
            if not allow_files:
                raise ComposeError("molecule.xyz_file is not accepted here (the shared server never reads files); "
                                   "pass the xyz text as molecule.xyz")
            try:
                mol_spec = {"xyz": Path(mol_spec["xyz_file"]).expanduser().read_text(errors="replace")}
            except OSError as e:
                raise ComposeError(f"cannot read xyz file {mol_spec['xyz_file']}: {e}") from None
            merged = {**merged, "molecule": mol_spec}
        s = spec_from_dict(merged)
        out["spec"] = _spec_summary(s)
        mol = build_molecule(s.molecule, s.charge, s.multiplicity,
                             charge_given=merged.get("charge") is not None, allow_files=False)
        notes += mol.notes

        from ..levels import load_levels

        entries, _ = load_levels(cfg)
        if not entries:
            raise ComposeError("no levels-of-theory table found (knowledge/ess/levels_of_theory.yaml)")
        level, disp = resolve_method(entries, s.program, s.method, s.dispersion, s.allow_unverified)
        notes += level.notes
        if level.cls not in IMPLEMENTED[s.program]:
            raise ComposeError(f"the composer does not write {CLASS_LABEL.get(level.cls, level.cls)} "
                               f"({level.name}) for {s.program}")
        if s.job not in PROGRAM_JOBS[s.program]:
            raise ComposeError(f"job {s.job!r} is not generated for {s.program}: "
                               f"{WHY_NO_JOB.get((s.program, s.job), 'not documented in its card')}. "
                               f"Supported: {', '.join(PROGRAM_JOBS[s.program])}")
        allowed = jobs_for(s.program, level.cls)
        if s.job not in allowed:
            raise ComposeError(f"{s.job} with {level.name} in {s.program} is not generated (only "
                               f"{', '.join(allowed)}): geometry/frequency jobs at this level need (numerical) "
                               "gradients that are rarely what you want; optimise with DFT and run a single point")
        disp_syntax, dnotes = resolve_dispersion(s.program, disp, level)
        notes += dnotes
        solv, snotes = resolve_solvation(s.program, s.solvation)
        notes += snotes
        basis = resolve_basis(s.program, s.basis, mol.elements, level, s.allow_unverified)
        notes += basis.notes
        notes += _version_notes(s.program, s.version, s.allow_unverified)

        job_name = _job_name(s, mol)
        input_name = job_name + EXTENSIONS[s.program]
        res = _resources(s, cfg, input_name, job_name)
        notes += res["notes"]
        version = s.version
        if version is None and res["software"]:
            m = re.search(r"-(g?\d+(?:\.\d+)?)$", res["software"])
            version = m.group(1) if m else None
        title = (f"{s.job} {level.name.replace('ω', 'w')}/{basis.orbital or '(built-in basis)'} {mol.formula} "
                 f"charge {mol.charge} mult {mol.multiplicity}")
        job = Job(spec=s, mol=mol, level=level, basis=basis, disp=disp_syntax, solv=solv, mem_lines=res["lines"],
                  cores=res["cores"], mem_gb=res["mem_gb"], input_name=input_name, title=title, version=version)
        text = WRITERS[s.program](job)
        if s.job in GEOMETRY_JOBS and mol.source == "smiles":
            notes.append("the job starts from the RDKit geometry; check the final structure (and for a TS, that the "
                         "guess is a real TS guess - SMILES cannot encode one)")
        if s.extra_keywords:
            notes.append(f"extra_keywords passed through unchecked by the composer: {s.extra_keywords}")

        from .inputcheck import check_input

        findings = check_input(content=text, filename=input_name, submit_content=res["submit_text"], cfg=cfg,
                               program=s.program)
        out["findings"] = [f.to_dict() for f in findings]
        errs = [f for f in findings if f.severity == "error"]
        for f in findings:
            if f.severity == "warning":
                notes.append(f"check_input warning [{f.code}]: {f.message}")
        if errs:
            raise ComposeError([f"check_input error [{f.code}]: {f.message}" for f in errs])
        out.update(ok=True, input_name=input_name, input_text=text, submit_name=res["submit_name"],
                   submit_text=res["submit_text"])
    except ComposeError as e:
        out["errors"] = e.errors
    return out


def _spec_summary(s: Spec) -> dict:
    d = {k: getattr(s, k) for k in ("program", "version", "job", "method", "basis", "dispersion", "solvation", "scf",
                                    "grid", "extra_keywords", "charge", "multiplicity", "resources", "name",
                                    "allow_unverified")}
    d["molecule"] = {k: (v if k != "xyz" else f"<{len(str(v).splitlines())} lines>") for k, v in s.molecule.items()}
    return d


def supported_matrix() -> dict[str, dict]:
    """{program: {"jobs": [...], "methods": [...]}} as implemented by the writers."""
    return {p: {"jobs": list(PROGRAM_JOBS[p]), "methods": sorted(CLASS_LABEL[c] for c in IMPLEMENTED[p])}
            for p in PROGRAMS}


def format_result(res: dict) -> str:
    if not res["ok"]:
        return "Cannot compose this job:\n" + "\n".join(f"- {e}" for e in res["errors"]) + (
            "\n\nNotes:\n" + "\n".join(f"- {n}" for n in res["notes"]) if res["notes"] else "")
    parts = [f"=== {res['input_name']} ===", res["input_text"].rstrip("\n") + "\n"]
    if res["submit_text"]:
        parts += [f"=== {res['submit_name']} ===", res["submit_text"]]
    info = [f for f in res["findings"] if f["severity"] == "info"]
    parts.append("Notes:\n" + "\n".join(f"- {n}" for n in res["notes"]))
    if info:
        parts.append("check_input notes:\n" + "\n".join(f"- [{f['code']}] {f['message']}" for f in info))
    return "\n".join(parts)


# ------------------------------------------------------------------ CLI

def register_cli(subparsers):
    p = subparsers.add_parser("compose", help="compose a validated ESS input (+ submit script) from an explicit spec")
    p.add_argument("spec_file", nargs="?", help="spec YAML (fields: see docs/compose.md); CLI options override it")
    p.add_argument("--protocol", help="protocol YAML (defaults + named steps) from your project; merged UNDER the spec")
    p.add_argument("--step", help="protocol step to use (e.g. sp, opt)")
    p.add_argument("--program", choices=PROGRAMS)
    p.add_argument("--version", dest="prog_version", help="program version, e.g. 6 (ORCA) or 16 (Gaussian)")
    p.add_argument("--job", choices=JOBS)
    p.add_argument("--method")
    p.add_argument("--basis")
    p.add_argument("--dispersion")
    p.add_argument("--solvation", help="MODEL:SOLVENT, e.g. smd:water")
    p.add_argument("--scf-convergence", choices=("loose", "normal", "tight", "verytight"))
    p.add_argument("--scf-max-iter", type=int)
    p.add_argument("--grid")
    p.add_argument("--extra", action="append", dest="extra_keywords", help="raw keyword passed through (repeatable)")
    p.add_argument("--charge", type=int)
    p.add_argument("--mult", "--multiplicity", type=int, dest="multiplicity")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--xyz", help="xyz file")
    g.add_argument("--smiles", help="SMILES (RDKit 3D embedding: a rough starting geometry)")
    p.add_argument("--server", help="cluster from servers.yaml (then a submit script is rendered)")
    p.add_argument("--software", help="servers.yaml software key (default: picked from program + version)")
    p.add_argument("--cores", type=int)
    p.add_argument("--mem", type=float, dest="mem_gb", help="TOTAL memory in GB")
    p.add_argument("--time", dest="walltime")
    p.add_argument("--partition")
    p.add_argument("--name", help="job / file name stem (default: formula_job)")
    p.add_argument("--allow-unverified", action="store_true", default=None,
                   help="write variant/unknown methods, unverified basis sets or ECPs anyway (reported in the notes)")
    p.add_argument("--out-dir", help="write the input and submit script here (default: print them)")
    p.add_argument("--force", action="store_true", help="overwrite existing files in --out-dir")
    p.add_argument("--json", action="store_true")
    return {"compose": _cli}


def _read_yaml(path: str, what: str) -> dict:
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ComposeError(f"cannot read {what} {path}: {e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ComposeError(f"{what} {path} must be a YAML mapping")
    return data


def _cli_spec(args) -> tuple[dict, dict | None, str | None]:
    spec: dict = {}
    protocol_path = args.protocol
    step = args.step
    if args.spec_file:
        spec = _read_yaml(args.spec_file, "spec")
        base = Path(args.spec_file).expanduser().resolve().parent
        # a spec file may name its protocol/step (paths relative to the spec file)
        if "protocol" in spec:
            pp = spec.pop("protocol")
            if protocol_path is None and pp:
                protocol_path = str((base / str(pp)).resolve()) if not Path(str(pp)).is_absolute() else str(pp)
        if "step" in spec:
            st = spec.pop("step")
            step = step or st
        mol = spec.get("molecule")
        if isinstance(mol, dict) and mol.get("xyz_file") and not Path(mol["xyz_file"]).expanduser().is_absolute():
            spec["molecule"] = {**mol, "xyz_file": str(base / mol["xyz_file"])}
    over = {"program": args.program, "version": args.prog_version, "job": args.job, "method": args.method,
            "basis": args.basis, "dispersion": args.dispersion, "grid": args.grid, "charge": args.charge,
            "multiplicity": args.multiplicity, "name": args.name, "allow_unverified": args.allow_unverified}
    for k, v in over.items():
        if v is not None:
            spec[k] = v
    if args.extra_keywords:
        spec["extra_keywords"] = list(args.extra_keywords)
    if args.solvation:
        model, _, solvent = args.solvation.partition(":")
        spec["solvation"] = {"model": model, "solvent": solvent}
    scf = {k: v for k, v in (("convergence", args.scf_convergence), ("max_iter", args.scf_max_iter)) if v is not None}
    if scf:
        spec["scf"] = {**(spec.get("scf") or {}), **scf}
    res = {k: v for k, v in (("server", args.server), ("software", args.software), ("cores", args.cores),
                             ("mem_gb", args.mem_gb), ("walltime", args.walltime), ("partition", args.partition))
           if v is not None}
    if res:
        spec["resources"] = {**(spec.get("resources") or {}), **res}
    if args.xyz:
        spec["molecule"] = {"xyz_file": args.xyz}
    elif args.smiles:
        spec["molecule"] = {"smiles": args.smiles}
    protocol = _read_yaml(protocol_path, "protocol") if protocol_path else None
    return spec, protocol, step


def _cli(args, cfg) -> int:
    try:
        spec, protocol, step = _cli_spec(args)
    except ComposeError as e:
        print("\n".join(e.errors), file=sys.stderr)
        return 2
    res = compose_ess_job(spec, protocol, step, cfg=cfg, allow_files=True)
    written = []
    if res["ok"] and args.out_dir:
        d = Path(args.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        files = [(res["input_name"], res["input_text"])]
        if res["submit_text"]:
            files.append((res["submit_name"], res["submit_text"]))
        clash = [n for n, _ in files if (d / n).exists()]
        if clash and not args.force:
            res["ok"] = False
            res["errors"] = [f"{d / n} exists; use --force to overwrite" for n in clash]
        else:
            for n, t in files:
                (d / n).write_text(t)
                written.append(str(d / n))
    if args.json:
        print(json.dumps({**res, "written": written}, indent=2))
    elif written:
        print("wrote " + ", ".join(written))
        print("Notes:\n" + "\n".join(f"- {n}" for n in res["notes"]))
    else:
        print(format_result(res))
    return 0 if res["ok"] else 1


# ------------------------------------------------------------------ MCP

def register_mcp(mcp, ctx) -> None:
    @mcp.tool(name="compose_ess_job")
    def compose_ess_job_tool(spec: dict, protocol: dict | None = None, step: str | None = None) -> str:
        """Compose a correct ESS input file (ORCA, Gaussian, Q-Chem, Psi4, Molpro, PySCF) plus the
        matching submit script from an explicit spec, instead of writing the input by hand. The
        result is validated with check_input; on any error no files are returned.

        Returns JSON: {ok, input_name, input_text, submit_name, submit_text, notes, findings, errors}.
        Write input_text/submit_text to files yourself; read `notes` (auxiliary basis choices, ECPs,
        rough SMILES geometry, unverified choices).

        Args:
            spec: {program: orca|gaussian|qchem|psi4|molpro|pyscf, version?, job: sp|opt|freq|opt+freq|ts|irc,
                method (e.g. wB97X-D3, B3LYP, DLPNO-CCSD(T)), basis (e.g. def2-TZVP), dispersion? (D3BJ|D4|D3),
                solvation? {model: smd|pcm|cpcm, solvent}, scf? {convergence: tight|verytight|loose, max_iter},
                grid?, extra_keywords? [raw strings], charge, multiplicity (2S+1),
                molecule: {xyz: "<xyz TEXT>"} or {smiles: "..."} (rough RDKit geometry),
                resources: {server (list_servers), cores, mem_gb, walltime, partition, software?} or
                {cores, mem_gb} for a standalone input, name?, allow_unverified? (default false)}.
                File paths are not accepted: pass xyz text.
            protocol: Optional protocol mapping (defaults + `steps: {name: {...}}`), e.g. the parsed
                YAML of the user's project protocol; merged UNDER spec.
            step: Protocol step to apply (e.g. "sp").
        """
        if not isinstance(spec, dict):
            return json.dumps({"ok": False, "errors": ["spec must be an object"]})
        if protocol is not None and not isinstance(protocol, dict):
            return json.dumps({"ok": False, "errors": ["protocol must be an object (the parsed protocol YAML); "
                                                       "the server does not read protocol files"]})
        res = compose_ess_job(spec, protocol, step, cfg=ctx.cfg, allow_files=False)
        ctx.emit({"tool": "compose_ess_job",
                  "args": {k: spec.get(k) for k in ("program", "job", "method", "basis")}
                  | {"server": (spec.get("resources") or {}).get("server") if isinstance(spec.get("resources"), dict)
                     else None, "step": step},
                  "n_results": int(res["ok"])})
        return json.dumps(res, indent=1)
