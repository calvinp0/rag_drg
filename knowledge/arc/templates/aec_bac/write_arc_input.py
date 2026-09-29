#!/usr/bin/env python3
"""AEC step 1: write an ARC input.yml for single points on the 16 Arkane AEC reference species.

Geometries are the experimental CCCBDB structures from the Arkane reference database. Do not
optimise them: AE.fit compares against CCCBDB atomization energies at those geometries.
Level of theory: LEVEL in aec_bac_common.py.

Run with rmg_env:   python write_arc_input.py
Then run ARC on input.yml (arc_env), then fit_aec.py.
"""

from pathlib import Path

from arkane.encorr.reference import ReferenceDatabase

from aec_bac_common import AEC_SPECIES, level_dict, project_name, write_arc_input, xyz_to_str

INPUT_PATH = Path("input.yml")


def main():
    db = ReferenceDatabase()
    db.load()

    species = []
    for label, ref_label in AEC_SPECIES.items():
        ref = db.get_species_from_label(ref_label)[0]
        species.append({
            "label": label,  # fit_aec.py looks for calcs/Species/<label>
            "smiles": ref.smiles,
            "charge": ref.charge,
            "multiplicity": ref.multiplicity,
            "xyz": xyz_to_str(ref.reference_data["CCCBDB"].xyz_dict),
        })

    write_arc_input(INPUT_PATH, {
        "project": project_name("aec"),
        "sp_level": level_dict(),
        "compute_thermo": False,
        # Only the single point. conf_opt/opt/fine/freq/rotors default to true in ARC when omitted.
        "job_types": {"conf_opt": False, "opt": False, "fine": False, "freq": False, "sp": True,
                      "rotors": False},
        "species": species,
    })
    print(f"Wrote {len(species)} species to {INPUT_PATH}. Next: run ARC on it, then fit_aec.py.")


if __name__ == "__main__":
    main()
