#!/usr/bin/env python3
"""AEC step 2: fit Arkane atom energy corrections from the ARC single points (AEJob).

Reads the newest sp_* output of each species under calcs/Species, converts the electronic energy
(no ZPE) to Hartree and runs AEJob for LEVEL (aec_bac_common.py). AEJob writes
AEC_<method>_<basis>.out with a 95% confidence interval per element and a dict ready to paste into
RMG-database/input/quantum_corrections/data.py under atom_energies (or use --write-to-database).

Run with rmg_env, in the ARC project directory:   python fit_aec.py [--write-to-database]
"""

import argparse
from pathlib import Path

import rmgpy.constants as constants
from arkane.encorr.ae import AEJob
from arkane.ess.factory import ess_factory

from aec_bac_common import AEC_SPECIES, arkane_level_of_theory, newest_job_output


def collect_energies(species_root: Path) -> dict:
    """{Arkane reference label: electronic energy in Hartree}."""
    energies = {}
    for label, ref_label in AEC_SPECIES.items():
        out = newest_job_output(species_root / label, "sp")
        if out is None:
            print(f"WARNING: no sp output for {label} ({ref_label}); its elements may be undetermined")
            continue
        e_h = ess_factory(str(out)).load_energy() / (constants.E_h * constants.Na)  # J/mol -> Hartree
        energies[ref_label] = e_h
        print(f"  {label:6s} {ref_label:18s} {e_h:18.8f} Ha  ({out.parent.name})")
    return energies


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", type=Path, default=Path("."), help="ARC project directory")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--write-to-database", action="store_true",
                        help="also write the AECs into your RMG-database quantum_corrections/data.py")
    args = parser.parse_args()

    lot = arkane_level_of_theory()
    # AEJob appends to this file. A leftover from an earlier fit (e.g. in a copied run folder) would
    # leave two results in one file, so refuse instead.
    out_file = args.output_dir / f"AEC_{lot.to_model_chem().replace('//', '__').replace('/', '_')}.out"
    if out_file.exists():
        raise SystemExit(f"{out_file} exists (AEJob appends to it). Move or delete it, then rerun.")

    energies = collect_energies(args.project_dir / "calcs" / "Species")
    print(f"Fitting AECs for {lot} from {len(energies)} of {len(AEC_SPECIES)} species")
    AEJob(species_energies=energies, level_of_theory=lot,
          write_to_database=args.write_to_database).execute(output_directory=str(args.output_dir))
    print(f"Done: {out_file}")


if __name__ == "__main__":
    main()
