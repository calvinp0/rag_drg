---
title: 'Arkane AEC/BAC for a new level: use the group''s ARC-driven scripts on Zeus
  (~/runs/ARC/AE_Corr*, BAC_wb97xd3)'
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- AEC
- BAC
- atom energy corrections
- bond additivity corrections
- arkane
- encorr
- reference_sets
- fit_aec
- fit_bac
- zeus
author: calvin
date: '2026-09-29'
---

# Arkane AEC/BAC for a new level: use the group's ARC-driven scripts on Zeus (~/runs/ARC/AE_Corr*, BAC_wb97xd3)

## Mistake

Explained fitting new Arkane AEC/BAC from the RMG-Py source only (hand-built ae()/bac() Arkane input files), without knowing the group already has a scripted, ARC-driven workflow.

## Correct approach

Copy the templates on Zeus (calvin.p) and change the level of theory in each script.
AEC (~/runs/ARC/AE_Corr_wb97xd3; AE_Corr = the DLPNO-CCSD(T)/cc-pVTZ version):
1. write_arc_input.py (rmg_env): reads ReferenceDatabase, writes input.yml for the 16 AEC species (Br2, HBr, CH3, CH3Cl, CH4, Cl2, HCl, F2, HF, H2, H2O, H2S, NH3, N2, O2, S2) using the CCCBDB xyz, sp only (opt/freq/conf_opt false, compute_thermo false).
2. Run ARC on it (arc_env, submit.sh on alon_q).
3. fit_aec.py (rmg_env): takes input.log from the newest sp_* folder of each species in calcs/Species, reads energies with ess_factory(...).load_energy() and converts them to Hartree, then runs AEJob(species_energies, level_of_theory=LevelOfTheory(...)).execute(output_directory='.'). Paste the block from AEC_<lot>.out into RMG-database/input/quantum_corrections/data.py under atom_energies.
BAC (~/runs/ARC/BAC_wb97xd3), which needs the AECs already in data.py:
1. bac_write_arc_input.py: writes an ARC input.yml with opt+freq+sp for every species in reference_sets['main'] (labels spcs<index>, adjlist + CCCBDB/default xyz as a starting guess, compute_thermo false, keep_logs true).
2. Run ARC.
3. fit_bac.py: for each spcs<idx>, finds the newest freq_* and sp_* outputs and computes H298 (scaled freqs, ZPE scale = freq_scale/1.014, + get_atom_correction). It stores CalculatedDataEntry(ThermoData(H298), xyz_dict) in spc.calculated_data[lot], then db.save().
4. run_bac_fit.py / bac_arkane.py: BACJob(lot, bac_type='p', write_to_database=True, overwrite=True).execute(), then bac_type='m' with fit_mol_corr=True, global_opt=True, global_opt_iter=5.
The LevelOfTheory used in the fit must match the one ARC later uses (arkane_level_of_theory). ORCA needs the keyword wb97x-d3; 'wb97xd-3' gives an ORCA keyword syntax error (both normalise to Arkane method 'wb97xd3').

## Evidence / source

User pointed to zeus:~/runs/ARC/AE_Br, AE_Corr, AE_Corr_wb97xd3 (2026-09-29); scripts read there and in BAC_wb97xd3. ORCA error from AE_Corr_wb97xd3/out.txt: 'keywords WB97XD-3 can either be duplicated or illegal'.
