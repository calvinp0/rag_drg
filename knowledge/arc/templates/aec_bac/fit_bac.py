#!/usr/bin/env python3
"""BAC step 2: compute H298 for the reference species from the ARC run and store it in the database.

For each spcs<index> it takes the newest freq_* output, and the energy from the newest sp_* output
(falling back to freq_*, then opt_*: ARC skips the sp job when sp_level equals opt_level). H298 includes
the scaled ZPE and the AECs, but no BACs. It is stored as calculated_data[<LevelOfTheory>] of the
reference species, and the ReferenceDatabase is saved: this rewrites the YAML files under
RMG-database/input/reference_sets/main. BACJob (run_bac_fit.py) reads its training data from there.

Needs the AECs for LEVEL in quantum_corrections/data.py first (fit_aec.py). If the AECs change,
rerun this script and run_bac_fit.py.

Run with rmg_env, in the ARC project directory:   python fit_bac.py [--dry-run]
"""

import argparse
from collections import Counter
from pathlib import Path
from typing import Optional, Tuple

from arkane.common import symbol_by_number
from arkane.encorr.corr import assign_frequency_scale_factor, get_atom_correction
from arkane.encorr.reference import CalculatedDataEntry, ReferenceDatabase
from arkane.ess import ess_factory
from rdkit.Chem import GetPeriodicTable
from rmgpy.quantity import ScalarQuantity
from rmgpy.statmech import HarmonicOscillator
from rmgpy.thermo import ThermoData

from aec_bac_common import BAC_LABEL_PREFIX, arkane_level_of_theory, newest_job_output

REFERENCE_SET = "main"


def find_logs(species_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """(freq output, energy output); energy prefers sp, then freq, then opt."""
    freq = newest_job_output(species_dir, "freq")
    energy = newest_job_output(species_dir, "sp") or freq or newest_job_output(species_dir, "opt")
    return freq, energy


def log_to_enthalpy(lot, freq_log: Path, energy_log: Path, temp: float = 298.15) -> ScalarQuantity:
    """H(temp) in kcal/mol with scaled frequencies, scaled ZPE and AECs (no BACs)."""
    freq_ess = ess_factory(str(freq_log))
    energy_ess = ess_factory(str(energy_log))

    conformer, _ = freq_ess.load_conformer()
    coords, numbers, masses = energy_ess.load_geometry()
    conformer.coordinates = (coords, "angstroms")
    conformer.number = numbers
    conformer.mass = (masses, "amu")

    freq_scale = assign_frequency_scale_factor(lot)
    for mode in conformer.modes:
        if isinstance(mode, HarmonicOscillator):
            mode.frequencies = (mode.frequencies.value_si * freq_scale, "cm^-1")
    zpe_scale = freq_scale / 1.014

    e = energy_ess.load_energy(zpe_scale_factor=zpe_scale)  # J/mol; electronic only for non-composite methods
    e += freq_ess.load_zero_point_energy() * zpe_scale
    e += get_atom_correction(lot, Counter(symbol_by_number[int(n)] for n in numbers))
    conformer.E0 = (e / 4184.0, "kcal/mol")
    return ScalarQuantity((conformer.get_enthalpy(temp) + conformer.E0.value_si) / 4184.0, "kcal/mol")


def log_to_xyz_dict(log: Path) -> dict:
    coords, numbers, _ = ess_factory(str(log)).load_geometry()
    symbols = [symbol_by_number[int(n)] for n in numbers]
    table = GetPeriodicTable()
    return {"symbols": symbols, "isotopes": [table.GetMostCommonIsotope(s) for s in symbols], "coords": coords}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", type=Path, default=Path("."), help="ARC project directory")
    parser.add_argument("--dry-run", action="store_true", help="print H298 values, do not save the database")
    args = parser.parse_args()

    lot = arkane_level_of_theory()  # for a composite level, use CompositeLevelOfTheory(freq=..., energy=...)
    get_atom_correction(lot, {"H": 1})  # fails early if the AECs for this level are not in data.py

    db = ReferenceDatabase()
    db.load()
    species_root = args.project_dir / "calcs" / "Species"
    updated, missing = 0, []
    for spc in db.reference_sets[REFERENCE_SET]:
        freq_log, energy_log = find_logs(species_root / f"{BAC_LABEL_PREFIX}{spc.index}")
        if freq_log is None or energy_log is None:
            missing.append(spc.index)
            continue
        h298 = log_to_enthalpy(lot, freq_log, energy_log)
        if args.dry_run:
            print(f"{BAC_LABEL_PREFIX}{spc.index:<5} {spc.label:40s} H298 = {h298.value:12.4f} kcal/mol")
        spc.calculated_data[lot] = CalculatedDataEntry(thermo_data=ThermoData(H298=h298),
                                                       xyz_dict=log_to_xyz_dict(freq_log))
        updated += 1

    print(f"H298 for {updated} species; no freq output for {len(missing)}: {missing[:20]}")
    if args.dry_run:
        return
    db.save()
    print("Saved the reference database. Next: run_bac_fit.py")


if __name__ == "__main__":
    main()
