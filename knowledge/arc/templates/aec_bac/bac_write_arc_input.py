#!/usr/bin/env python3
"""BAC step 1: write an ARC input.yml for opt + freq + sp of every species in reference_sets['main'].

About 420 species. The xyz is only a starting guess (the database default geometry, else CCCBDB);
ARC optimises it. Labels are spcs<index> so fit_bac.py can map results back to the reference species.
Level of theory: LEVEL in aec_bac_common.py (used for opt, freq and sp).

Run with rmg_env:   python bac_write_arc_input.py
Then run ARC on input.yml (arc_env), then fit_bac.py.
"""

from pathlib import Path

from arkane.encorr.reference import ReferenceDatabase

from aec_bac_common import BAC_LABEL_PREFIX, level_dict, project_name, write_arc_input, xyz_to_str

INPUT_PATH = Path("input.yml")
REFERENCE_SET = "main"


def starting_xyz(spc) -> str:
    try:
        xyz = spc.get_default_xyz()
    except Exception:  # no preferred source for this species
        xyz = spc.reference_data["CCCBDB"].xyz_dict
    return xyz if isinstance(xyz, str) else xyz_to_str(xyz)


def main():
    db = ReferenceDatabase()
    db.load()

    species, skipped = [], []
    for spc in db.reference_sets[REFERENCE_SET]:
        try:
            xyz = starting_xyz(spc)
        except Exception:
            skipped.append(spc.index)
            continue
        species.append({
            "label": f"{BAC_LABEL_PREFIX}{spc.index}",
            "adjlist": spc.adjacency_list.strip() + "\n",
            "charge": spc.charge,
            "multiplicity": spc.multiplicity,
            "xyz": xyz.strip() + "\n",
        })

    write_arc_input(INPUT_PATH, {
        "project": project_name("bac"),
        "opt_level": level_dict(),
        "freq_level": level_dict(),
        "sp_level": level_dict(),
        "compute_thermo": False,
        "compare_to_rmg": False,
        # Optimise a species even if its 3D starting geometry is not isomorphic to the adjlist
        # (ions and unusual valences in the reference set); otherwise ARC skips it and it has no H298.
        "allow_nonisomorphic_2d": True,
        "job_types": {"conf_opt": False, "opt": True, "fine": False, "freq": True, "sp": True,
                      "rotors": False},
        "species": species,
    })
    print(f"Wrote {len(species)} species to {INPUT_PATH}.")
    if skipped:
        print(f"Skipped (no default or CCCBDB geometry): index {skipped}")


if __name__ == "__main__":
    main()
