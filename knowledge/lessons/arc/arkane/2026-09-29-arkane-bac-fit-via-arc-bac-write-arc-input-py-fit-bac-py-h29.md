---
title: 'Arkane BAC fit via ARC: bac_write_arc_input.py + fit_bac.py (H298 into reference_sets) + BACJob p/m'
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- BAC
- bond additivity corrections
- BACJob
- Petersson
- Melius
- fit_bac
- reference_sets
- calculated_data
- H298
- arkane
- template
author: calvin
date: '2026-09-29'
similar:
- knowledge/lessons/arc/arkane/2026-09-29-arkane-aec-bac-for-a-new-level-use-the-group-s-arc-driven-sc.md
---

# Arkane BAC fit via ARC: bac_write_arc_input.py + fit_bac.py (H298 into reference_sets) + BACJob p/m

## Mistake

Assumed the BAC training data (H298 as `calculated_data` in RMG-database `reference_sets/main`) had to
be added species by species by running Arkane thermo. The group runs all reference species as one ARC
opt+freq+sp job and scripts the rest: `bac_write_arc_input.py`, `fit_bac.py` and `run_bac_fit.py` in
`knowledge/arc/templates/aec_bac/`.

## Correct approach

Prerequisite: the AECs for the level are already in `quantum_corrections/data.py`, because the H298 values
include them. Set `LEVEL` in `aec_bac_common.py`; run the scripts in rmg_env and ARC in arc_env.
1. `bac_write_arc_input.py` writes one ARC species per entry in `reference_sets['main']` (~420):
   label `spcs<index>`, which `fit_bac.py` uses to map results back. It also writes `adjlist`, charge and
   multiplicity, and a starting xyz (`spc.get_default_xyz()`, else the CCCBDB xyz; ARC optimises it).
   opt, freq and sp levels are all `LEVEL`. Also `compute_thermo: false`, `compare_to_rmg: false`,
   `allow_nonisomorphic_2d: true` (otherwise ARC does not optimise species whose starting geometry is not
   isomorphic to the adjlist), and `job_types: {conf_opt: false, opt: true, fine: false, freq: true,
   sp: true, rotors: false}`. Do not add `keep_logs`: it is not an ARC argument, and `ARC(**input)` raises
   TypeError.
2. Run ARC. When sp level = opt level, ARC runs no separate sp job.
3. `fit_bac.py` takes the newest `freq_*` output and the energy from the newest `sp_*` output (falling back
   to freq, then opt). It computes H298 with AECs but no BACs:
```python
conformer, _ = ess_factory(freq_log).load_conformer()     # set coordinates/number/mass from energy_ess.load_geometry()
freq_scale = assign_frequency_scale_factor(lot)           # scale the HarmonicOscillator frequencies by this
zpe_scale = freq_scale / 1.014
e = energy_ess.load_energy(zpe_scale_factor=zpe_scale)    # J/mol
e += freq_ess.load_zero_point_energy() * zpe_scale
e += get_atom_correction(lot, Counter(symbols))           # AECs, J/mol
conformer.E0 = (e / 4184.0, "kcal/mol")
h298 = ScalarQuantity((conformer.get_enthalpy(298.15) + conformer.E0.value_si) / 4184.0, "kcal/mol")
spc.calculated_data[lot] = CalculatedDataEntry(thermo_data=ThermoData(H298=h298), xyz_dict=xyz_dict)
db.save()   # rewrites the YAMLs under RMG-database/input/reference_sets/main
```
   Use `--dry-run` to print the values without saving. For a composite level, use `CompositeLevelOfTheory`
   as the key.
4. `run_bac_fit.py`: `BACJob(lot, bac_type="p", weighted=False, write_to_database=True, overwrite=True)`,
   then `bac_type="m"` with `fit_mol_corr=True, global_opt=True, global_opt_iter=5`. The results go into
   `pbac` and `mbac` in `data.py`. A failed database write is only logged as a warning.
If the AECs change later, redo steps 3 and 4. The `lot` key must be the same in every step and match the
level ARC uses for thermo. Commit the RMG-database changes, or a later pull/reset of that checkout loses
them.

## Evidence / source

An earlier group BAC run (2026-01): 421 species, freq outputs for all, no sp jobs (sp level = opt level). The `input.yml` it ran has no `keep_logs` (the script there wrote it) and has
`allow_nonisomorphic_2d: True` (the script did not write it). The
fitted BACs are no longer in the RMG-database checkout the fit wrote to (checked 2026-09-29). The
template's H298 equals the original script's for the species compared. RMG-Py arkane/encorr/bac.py
(BAC.fit reads calculated_data; BACJob.execute only warns if the database write fails); ARC input schema
(`keep_logs` unknown).
