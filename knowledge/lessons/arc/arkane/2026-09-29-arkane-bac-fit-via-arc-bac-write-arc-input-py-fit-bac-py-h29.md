---
title: 'Arkane BAC fit via ARC: bac_write_arc_input.py + fit_bac.py (H298 into reference_sets)
  + BACJob p/m'
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
- zeus
author: calvin
date: '2026-09-29'
similar:
- knowledge/lessons/arc/arkane/2026-09-29-arkane-aec-bac-for-a-new-level-use-the-group-s-arc-driven-sc.md
---

# Arkane BAC fit via ARC: bac_write_arc_input.py + fit_bac.py (H298 into reference_sets) + BACJob p/m

## Mistake

Assumed BAC training data (calculated_data H298 in RMG-database reference_sets/main) had to be added per species by running Arkane thermo. The group scripts it around one ARC opt+freq+sp run (zeus:~/runs/ARC/BAC_wb97xd3).

## Correct approach

Prerequisite: the AECs for the level must already be in quantum_corrections/data.py, because the H298 values include them. Run the scripts in rmg_env and ARC in arc_env.
1. bac_write_arc_input.py writes one ARC species per entry in reference_sets['main'] (~420 species):
```python
db = ReferenceDatabase(); db.load()
for spc in db.reference_sets["main"]:
    # label: spcs{spc.index}; adjlist: spc.adjacency_list; charge/multiplicity from spc
    # xyz (starting guess only): spc.get_default_xyz(), falling back to spc.reference_data["CCCBDB"].xyz_dict
```
Set opt_level, freq_level and sp_level to the same LOT. Also `compute_thermo: false`, `compare_to_rmg: false`, `keep_logs: true`, and `job_types: {conf_opt: false, opt: true, freq: true, sp: true, rotors: false}`. The label spcs<index> is how fit_bac.py maps results back to the reference species.
2. Run ARC.
3. fit_bac.py: for each spcs<idx>, take the newest freq_* output and the newest sp_* output (fall back to freq, then opt). Compute H298 with AECs but no BACs:
```python
conformer, _ = ess_factory(freq_log).load_conformer()
coords, nums, masses = energy_ess.load_geometry()        # set conformer.coordinates/number/mass from these
freq_scale = assign_frequency_scale_factor(freq_lot)     # scale the HarmonicOscillator frequencies by this
zpe_scale = freq_scale / 1.014
e = energy_ess.load_energy(zpe_scale_factor=zpe_scale)
e += freq_ess.load_zero_point_energy() * zpe_scale
e += get_atom_correction(energy_lot, Counter(symbols))  # J/mol
conformer.E0 = (e / 4184.0, "kcal/mol")
h298 = ScalarQuantity((conformer.get_enthalpy(298.15) + conformer.E0.value_si) / 4184.0, "kcal/mol")
spc.calculated_data[energy_lot] = CalculatedDataEntry(thermo_data=ThermoData(H298=h298), xyz_dict=xyz_dict)
...
db.save()   # writes the YAMLs under RMG-database/input/reference_sets/main
```
Imports: arkane.ess.ess_factory; arkane.encorr.corr.get_atom_correction and assign_frequency_scale_factor; arkane.encorr.reference.ReferenceDatabase and CalculatedDataEntry; rmgpy.thermo.ThermoData. xyz_dict = {symbols, isotopes (most common), coords} from the freq log. For a composite level, use CompositeLevelOfTheory as the key.
4. run_bac_fit.py:
```python
kw = {"weighted": False, "write_to_database": True, "overwrite": True}
BACJob(level_of_theory=lot, bac_type="p", **kw).execute()
BACJob(level_of_theory=lot, bac_type="m", fit_mol_corr=True, global_opt=True, global_opt_iter=5, **kw).execute()
```
The results go into pbac and mbac in quantum_corrections/data.py. If the AECs change later, redo steps 3 and 4. The lot key must be the same in every step, and must match ARC's arkane_level_of_theory.

## Evidence / source

zeus:~/runs/ARC/BAC_wb97xd3/{bac_write_arc_input.py,fit_bac.py,run_bac_fit.py,bac_arkane.py}, read 2026-09-29. RMG-Py arkane/encorr/bac.py (BAC.fit reads calculated_data via extract_dataset; write_to_database edits pbac/mbac).
