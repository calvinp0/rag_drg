---
title: "Template: fit Arkane AEC and BAC for a new level of theory with ARC (aec_bac scripts)"
domain: arc
software: arkane
doc_type: template
status: draft
tags: [arkane, arc, aec, bac, atom energy corrections, bond additivity corrections, AEJob, BACJob, fit_aec,
       fit_bac, write_arc_input, bac_write_arc_input, run_bac_fit, reference_sets, calculated_data,
       quantum_corrections, data.py, CCCBDB, petersson, melius, template]
---
# Fit Arkane AEC/BAC for a new level of theory (template scripts)

This folder (`knowledge/arc/templates/aec_bac/` in the rag-drg repo) holds the group's scripts for
fitting Arkane energy corrections for a level that has none. ARC runs the reference species, and
Arkane's `AEJob` / `BACJob` do the fits. Background (what AEC/BAC are, what ARC does without them):
the card "Arkane energy corrections (AEC/BAC)".

| File | Step | Environment |
|---|---|---|
| `aec_bac_common.py` | **the only file you edit**: `LEVEL` (method, basis, software), optional `ESS_SETTINGS` | imported by all |
| `write_arc_input.py` | AEC 1: ARC `input.yml`, sp only, 16 species at CCCBDB geometries | rmg_env |
| `fit_aec.py` | AEC 2: newest `sp_*` energies → `AEJob` → `AEC_<method>_<basis>.out` | rmg_env |
| `bac_write_arc_input.py` | BAC 1: ARC `input.yml`, opt+freq+sp for `reference_sets['main']` (~420 species) | rmg_env |
| `fit_bac.py` | BAC 2: H298 (with AECs, no BACs) → `calculated_data` in the reference database | rmg_env |
| `run_bac_fit.py` | BAC 3: `BACJob` Petersson (`p`), then Melius (`m`) → `pbac` / `mbac` in `data.py` | rmg_env |

## Procedure

1. Copy the whole folder into a fresh directory for the AEC run, and again for the BAC run. The scripts import
   `aec_bac_common.py` from the same folder. A fresh directory also avoids an old run's `calcs/` or
   `AEC_*.out` being picked up.
2. Set `LEVEL` (method, basis, software) in `aec_bac_common.py`; the scripts stop until it is filled in.
   Spell `method` as the ESS expects it, because ARC passes it through; check with `lookup_level_of_theory`.
3. **AEC**: run `python write_arc_input.py` (writes `input.yml`), then run ARC on it (arc_env, as for any ARC run;
   `compose_arc_run` gives the runner job and settings for a cluster), then run `python fit_aec.py` in the ARC
   project directory.
   * Geometries are the experimental CCCBDB ones and must not be optimised: AE.fit compares against CCCBDB
     atomization energies. The input therefore sets `conf_opt`, `opt`, `fine`, `freq` and `rotors` to
     false. ARC defaults them to true, and it does not recognise an old key such as `conformers`.
   * Paste the dict from `AEC_<method>_<basis>.out` into `RMG-database/input/quantum_corrections/data.py`
     under `atom_energies`, or run `fit_aec.py --write-to-database`.
   * `AEJob` appends to `AEC_*.out`. `fit_aec.py` refuses to run if that file already exists, because a
     leftover file would end up holding two fits.
4. **BAC** (needs the AECs of step 3 in `data.py`, since H298 includes them): run `python bac_write_arc_input.py`,
   run ARC, then `python fit_bac.py --dry-run` (prints H298 per species), then `python fit_bac.py` and
   `python run_bac_fit.py`.
   * The input sets `allow_nonisomorphic_2d: true`. Otherwise ARC does not optimise reference species whose
     starting geometry is not isomorphic to the adjacency list.
   * When `sp_level` equals `opt_level`, ARC runs no separate sp job. `fit_bac.py` then takes the energy
     from the freq output.
   * `fit_bac.py` saves the reference database, which rewrites the YAMLs under
     `RMG-database/input/reference_sets/main`. `run_bac_fit.py` writes `pbac`/`mbac` into `data.py`, and also
     writes `output.py` and CSVs next to the run. A failed database write is only a warning
     ("Could not write BACs to database"), so check the log.
   * Frequencies are scaled with Arkane's factor for the level. The lookup tries the level, then the level
     without software, and falls back to 1. If the level has no factor, add one before trusting H298.
   * If the AECs change later, rerun `fit_bac.py` and `run_bac_fit.py`.
5. **Keep the results.** The fits write into your RMG-database checkout. Commit those changes (on a branch,
   or upstream), or a later `git pull`/reset of that checkout loses them.
6. **Use them.** ARC finds the corrections when its `sp_level` (or `arkane_level_of_theory`) normalises to
   the same `LevelOfTheory` key: same method, basis and **software**. Check the ARC log for
   "Arkane energy corrections matched for ...".

## Adapting the scripts

* **Composite level** (freq/geometry at one level, energy at another): return
  `CompositeLevelOfTheory(freq=..., energy=...)` from `arkane_level_of_theory()`, give
  `bac_write_arc_input.py` separate `opt_level`/`freq_level` and `sp_level`, and fit the AECs at the energy level.
* **ESS not in `ARC_OUTPUT_FILENAMES`**: add the file name ARC gives its output (ARC
  `arc/settings/settings.py`, `output_filenames`). Arkane's `ess_factory` must also be able to read that
  output.
* **Only some elements needed**: drop species from `AEC_SPECIES`. `AE.fit` fits one energy per element present
  and needs more species than elements, or the confidence intervals are undefined. The corrections then cover
  only those elements.
* **Check transferability** before writing BACs: give `BACJob` `crossval_n_folds` (-1 = leave-one-out); nothing
  is written to the database while cross-validating.
* The fitting itself is always `AEJob(species_energies={reference label: Hartree}, level_of_theory=...)` and
  `BACJob(level_of_theory=..., bac_type='p'|'m')`. The scripts only gather the inputs those need from the
  ARC project.
