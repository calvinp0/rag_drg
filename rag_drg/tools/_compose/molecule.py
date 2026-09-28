"""Molecule input for the composer: xyz text or SMILES (RDKit embedding) -> atoms, charge, multiplicity."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..basis import ELEMENTS, Z_OF, normalize_element
from .spec import ComposeError

MAX_ATOMS = 2000


@dataclass
class Molecule:
    atoms: list[tuple[str, float, float, float]]
    charge: int
    multiplicity: int
    notes: list[str] = field(default_factory=list)
    source: str = "xyz"

    @property
    def elements(self) -> list[str]:
        return sorted({a[0] for a in self.atoms}, key=lambda s: Z_OF[s.lower()])

    @property
    def n_electrons(self) -> int:
        return sum(Z_OF[a[0].lower()] for a in self.atoms) - self.charge

    @property
    def spin_2s(self) -> int:
        return self.multiplicity - 1

    @property
    def formula(self) -> str:
        counts: dict[str, int] = {}
        for a in self.atoms:
            counts[a[0]] = counts.get(a[0], 0) + 1
        order = (["C", "H"] if "C" in counts else []) + sorted(e for e in counts if e not in ("C", "H") or "C" not in counts)
        return "".join(f"{e}{counts[e] if counts[e] > 1 else ''}" for e in order)

    def xyz_lines(self, fmt: str = "{:<2s} {:14.8f} {:14.8f} {:14.8f}") -> list[str]:
        return [fmt.format(s, x, y, z) for s, x, y, z in self.atoms]


def parse_xyz_text(text: str) -> list[tuple[str, float, float, float]]:
    """xyz text (with or without the count/comment header) -> atoms. Never a file path."""
    rows = [r for r in (text or "").strip("\n").splitlines()]
    if rows and re.fullmatch(r"\s*\d+\s*", rows[0]):
        n = int(rows[0])
        rows = rows[2:2 + n] if len(rows) >= 2 + n else rows[2:]
    atoms = []
    for r in rows:
        tok = r.replace(",", " ").split()
        if not tok:
            continue
        if len(tok) < 4:
            raise ComposeError(f"cannot read xyz line {r.strip()!r} (expected: Element x y z, in Angstrom)")
        sym = tok[0]
        if sym.isdigit() and 0 < int(sym) <= len(ELEMENTS):
            sym = ELEMENTS[int(sym) - 1]
        el = normalize_element(re.sub(r"\d+$", "", sym))
        if el is None or not re.fullmatch(r"[A-Za-z]{1,3}\d*", sym):
            raise ComposeError(f"unknown element {tok[0]!r} in xyz line {r.strip()!r} (ghost/dummy atoms are not "
                               "supported by the composer)")
        try:
            x, y, z = (float(t) for t in tok[1:4])
        except ValueError:
            raise ComposeError(f"cannot read coordinates in xyz line {r.strip()!r}") from None
        if not all(math.isfinite(c) for c in (x, y, z)):
            raise ComposeError(f"coordinates must be finite numbers in xyz line {r.strip()!r}")
        atoms.append((el, x, y, z))
    if not atoms:
        raise ComposeError("no atoms found in the xyz text")
    if len(atoms) > MAX_ATOMS:
        raise ComposeError(f"{len(atoms)} atoms is more than the composer handles ({MAX_ATOMS})")
    return atoms


def smiles_to_atoms(smiles: str) -> tuple[list[tuple[str, float, float, float]], int, int]:
    """RDKit: SMILES -> 3D (ETKDG embedding + MMFF94 relaxation). Returns (atoms, formal charge, radical mult)."""
    try:
        from rdkit import Chem, RDLogger  # noqa: PLC0415
        from rdkit.Chem import AllChem  # noqa: PLC0415
    except ImportError as e:
        raise ComposeError("SMILES input needs RDKit: pip install 'rag-drg[chem]' (or pass xyz text)") from e
    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ComposeError(f"RDKit could not parse SMILES {smiles!r}")
    charge = Chem.GetFormalCharge(mol)
    radicals = sum(a.GetNumRadicalElectrons() for a in mol.GetAtoms())
    mol = Chem.AddHs(mol)
    if mol.GetNumAtoms() > MAX_ATOMS:
        raise ComposeError(f"{mol.GetNumAtoms()} atoms is more than the composer handles ({MAX_ATOMS})")
    params = AllChem.ETKDGv3()
    params.randomSeed = 0xF00D
    if AllChem.EmbedMolecule(mol, params) != 0:
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) != 0:
            raise ComposeError(f"RDKit could not embed {smiles!r} in 3D; pass xyz text instead")
    if mol.GetNumAtoms() > 1:
        try:
            if AllChem.MMFFHasAllMoleculeParams(mol):
                AllChem.MMFFOptimizeMolecule(mol, maxIters=2000)
            else:
                AllChem.UFFOptimizeMolecule(mol, maxIters=2000)
        except Exception:  # noqa: BLE001 - the embedded geometry is still usable as a start
            pass
    conf = mol.GetConformer()
    atoms = []
    for a in mol.GetAtoms():
        p = conf.GetAtomPosition(a.GetIdx())
        atoms.append((a.GetSymbol(), float(p.x), float(p.y), float(p.z)))
    return atoms, charge, radicals + 1


def build_molecule(mol_spec: dict, charge: int, multiplicity: int | None, charge_given: bool,
                   allow_files: bool) -> Molecule:
    notes: list[str] = []
    if mol_spec.get("xyz_file"):
        if not allow_files:
            raise ComposeError("molecule.xyz_file is not accepted here (the shared server never reads files); "
                               "pass the xyz text as molecule.xyz")
        raise ComposeError("molecule.xyz_file must be read by the caller (CLI) before composing")
    if mol_spec.get("smiles"):
        smi = str(mol_spec["smiles"]).strip()
        atoms, fc, mult_guess = smiles_to_atoms(smi)
        if charge_given and charge != fc:
            raise ComposeError(f"charge {charge} disagrees with the formal charge {fc} of SMILES {smi!r}")
        charge = fc
        if multiplicity is None:
            multiplicity = mult_guess
            notes.append(f"multiplicity {multiplicity} taken from the SMILES radical count")
        notes.append(f"GEOMETRY FROM SMILES {smi!r}: RDKit ETKDG embedding + force-field (MMFF94/UFF) relaxation. "
                     "This is a ROUGH STARTING GEOMETRY (one conformer, no conformer search); optimise it before "
                     "trusting energies or frequencies.")
        source = "smiles"
    else:
        atoms = parse_xyz_text(str(mol_spec.get("xyz") or ""))
        source = "xyz"
        if multiplicity is None:
            raise ComposeError("multiplicity is required with xyz input (2S+1: singlet 1, doublet 2, triplet 3)")
    m = Molecule(atoms=atoms, charge=charge, multiplicity=multiplicity, notes=notes, source=source)
    n = m.n_electrons
    if n < 0:
        raise ComposeError(f"charge {charge} leaves {n} electrons")
    if multiplicity - 1 > n or (n - (multiplicity - 1)) % 2:
        raise ComposeError(
            f"{n} electrons ({m.formula}, charge {charge}) cannot have multiplicity {multiplicity}: an "
            f"{'odd' if n % 2 else 'even'} electron count needs an {'even' if n % 2 else 'odd'} multiplicity "
            f"(e.g. {n % 2 + 1}); check the charge and multiplicity")
    return m
