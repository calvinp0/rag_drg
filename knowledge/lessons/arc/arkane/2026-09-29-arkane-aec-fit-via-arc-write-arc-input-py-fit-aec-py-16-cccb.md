---
title: 'Arkane AEC fit via ARC: write_arc_input.py + fit_aec.py (16 CCCBDB species, sp only)'
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- AEC
- atom energy corrections
- AEJob
- fit_aec
- write_arc_input
- CCCBDB
- quantum_corrections
- data.py
- arkane
- template
author: calvin
date: '2026-09-29'
similar:
- knowledge/lessons/arc/arkane/2026-09-29-arkane-aec-bac-for-a-new-level-use-the-group-s-arc-driven-sc.md
---

# Arkane AEC fit via ARC: write_arc_input.py + fit_aec.py (16 CCCBDB species, sp only)

## Mistake

Treated the AEC fit as a hand-written Arkane ae() input, with the 16 single-point energies collected by
hand. The group runs them as one ARC sp-only job and fits with a script: `write_arc_input.py` and
`fit_aec.py` in `knowledge/arc/templates/aec_bac/`.

## Correct approach

Copy the template folder into a fresh directory, set `LEVEL` in `aec_bac_common.py`, and run the scripts
in rmg_env and ARC in arc_env.
1. `write_arc_input.py` writes `input.yml` with the experimental CCCBDB geometry of each species:
```python
from arkane.encorr.reference import ReferenceDatabase
db = ReferenceDatabase(); db.load()
ref = db.get_species_from_label(ref_label)[0]          # e.g. "Methane"; labels = SPECIES_LABELS in arkane/encorr/ae.py
xyz_dict = ref.reference_data["CCCBDB"].xyz_dict       # plus ref.smiles, ref.charge, ref.multiplicity
```
   The geometries must not be optimised, so `job_types` sets `conf_opt`, `opt`, `fine`, `freq` and
   `rotors` to false, and `sp` to true. ARC defaults the omitted ones to true, and it does not recognise an
   old key such as `conformers: false` (that leaves `conf_opt` on). `compute_thermo: false`.
2. Run ARC.
3. `fit_aec.py` takes the ESS output of the newest `calcs/Species/<label>/sp_a<N>` folder and ignores
   attempts that left no output. It converts `ess_factory(path).load_energy()` (J/mol, electronic only)
   to Hartree and runs
   `AEJob(species_energies, level_of_theory=LevelOfTheory(**LEVEL)).execute(output_directory=".")`.
4. `AEJob` writes `AEC_<method>_<basis>.out` in append mode, with a 95% CI per element and a dict to paste
   into RMG-database `input/quantum_corrections/data.py` under `atom_energies` (or pass
   `write_to_database=True`). Because of append mode, a leftover file in a copied directory ends up holding
   two fits, or a file named after one level holds another level's numbers. Start in a fresh directory;
   the template refuses to run when the file already exists.

## Evidence / source

An earlier group AEC run (2026-01), in a directory copied from a run at another level: the
`AEC_<other level>.out` there held the new level's numbers. The `write_arc_input.py` there writes
`conformers: false`, while the `input.yml` that ran has `conf_opt: false`. The template reproduces that run's AECs exactly
(2026-09-29). RMG-Py arkane/encorr/ae.py (AEJob.execute opens the output with 'a'); ARC arc/common.py
(job_types defaults, legacy alias fine_grid -> fine).
