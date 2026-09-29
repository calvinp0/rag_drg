"""Shared settings for the Arkane AEC/BAC templates. Edit LEVEL here, and nowhere else.

Every script in this folder imports this module, so copy the whole folder into a fresh run
directory. The ARC input writers and the fit scripts then use the same level of theory; separate
copies per script drift apart, and corrections then get fitted or stored under the wrong key.

Run the scripts with the RMG environment (rmg_env: rmgpy + arkane importable). Run ARC with arc_env.
"""

import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- edit this
# The level of theory to fit corrections for, as ARC and the ESS spell it. ARC passes `method` straight
# into the ESS input, so use that program's keyword (check with lookup_level_of_theory): two spellings
# Arkane normalises to the same key can still differ in whether the ESS accepts them. The normalised
# LevelOfTheory is the key in RMG-database/input/quantum_corrections/data.py, and ARC's sp_level (or
# arkane_level_of_theory) must normalise to the same key when you later run thermo.
LEVEL = {
    "method": "",     # e.g. the ESS keyword of the functional or wavefunction method
    "basis": "",
    "software": "",   # the ESS ARC runs it with; corrections are specific to it
}

# Optional ARC `ess_settings` for the generated inputs, e.g. {"<ess>": ["local"]} when ARC itself runs
# inside a batch job and the ESS runs on that node. None: use the ess_settings from your ARC settings.
ESS_SETTINGS: Optional[dict] = None
# --------------------------------------------------------------------------

# ARC species label -> Arkane reference label. The right-hand side must match SPECIES_LABELS in
# RMG-Py arkane/encorr/ae.py; these are the species AE.fit knows about.
AEC_SPECIES = {
    "Br2": "Dibromine",
    "BrH": "Hydrogen bromide",
    "CH3": "Methyl",
    "CH3Cl": "Chloromethane",
    "CH4": "Methane",
    "Cl2": "Dichlorine",
    "ClH": "Hydrogen chloride",
    "F2": "Difluorine",
    "FH": "Hydrogen fluoride",
    "H2": "Dihydrogen",
    "H2O": "Water",
    "H2S": "Hydrogen sulfide",
    "H3N": "Ammonia",
    "N2": "Dinitrogen",
    "O2": "Dioxygen",
    "S2": "Disulfur",
}

# BAC species are labelled spcs<index>, where index is ReferenceSpecies.index in reference_sets['main'];
# fit_bac.py uses the label to map results back to the reference species.
BAC_LABEL_PREFIX = "spcs"

# The file ARC leaves in each job folder, per ESS (ARC arc/settings/settings.py: output_filenames).
ARC_OUTPUT_FILENAMES = {
    "cfour": "output.out",
    "gaussian": "input.log",
    "molpro": "input.out",
    "orca": "input.log",
    "qchem": "output.out",
    "terachem": "output.out",
    "xtb": "output.out",
}


def level_dict() -> dict:
    """LEVEL for an ARC input; stops if it has not been filled in."""
    if not all(LEVEL.get(k) for k in ("method", "basis", "software")):
        raise SystemExit("Set LEVEL (method, basis, software) in aec_bac_common.py first.")
    return {"method": LEVEL["method"], "basis": LEVEL["basis"], "software": LEVEL["software"]}


def arkane_level_of_theory():
    """The Arkane key the corrections are fitted and stored under.

    For a composite level (freq and energy at different levels), return
    CompositeLevelOfTheory(freq=LevelOfTheory(...), energy=LevelOfTheory(...)) here instead.
    """
    from arkane.modelchem import LevelOfTheory

    return LevelOfTheory(**level_dict())


def output_filename(software: str = None) -> str:
    software = software or level_dict()["software"]
    try:
        return ARC_OUTPUT_FILENAMES[software.lower()]
    except KeyError:
        raise ValueError(f"Add the ARC output file name for {software!r} to ARC_OUTPUT_FILENAMES") from None


def newest_job_output(species_dir: Path, job_type: str) -> Optional[Path]:
    """The ESS output of the newest `<job_type>_a<N>` folder (e.g. sp_a2595) under `species_dir`.

    ARC starts a new folder for every attempt (troubleshooting, restarts), so a species can have
    several. Folders without an output (failed or still running) are skipped.
    """
    if not species_dir.is_dir():
        return None
    pattern = re.compile(rf"^{re.escape(job_type)}_a?\d+$")
    folders = [p for p in species_dir.iterdir() if p.is_dir() and pattern.match(p.name)]
    for folder in sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True):
        out = folder / output_filename()
        if out.is_file():
            return out
    return None


def project_name(prefix: str) -> str:
    """e.g. aec_<method>_<basis>; ARC project names must be valid folder names."""
    level = level_dict()
    return re.sub(r"[^A-Za-z0-9_+-]", "", f"{prefix}_{level['method']}_{level['basis']}")


def xyz_to_str(xyz_dict: dict) -> str:
    return "\n".join(f"{s:2s} {x:14.8f} {y:14.8f} {z:14.8f}"
                     for s, (x, y, z) in zip(xyz_dict["symbols"], xyz_dict["coords"]))


def write_arc_input(path: Path, data: dict) -> None:
    """Write an ARC input.yml; multi-line strings (xyz, adjlist) as YAML literal blocks."""
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper, s):
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)

    _Dumper.add_representer(str, _str)
    if ESS_SETTINGS:
        data = {**data, "ess_settings": ESS_SETTINGS}
    path.write_text(yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False))
