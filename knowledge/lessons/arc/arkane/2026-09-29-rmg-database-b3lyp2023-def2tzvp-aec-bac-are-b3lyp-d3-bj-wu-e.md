---
title: "RMG-database b3lyp2023/def2tzvp AEC+BAC are B3LYP-D3(BJ) (Wu et al. 2024)\
  \ \u2014 do not use with plain B3LYP"
domain: arc
software: arkane
doc_type: lesson
status: unreviewed
tags:
- AEC
- BAC
- b3lyp2023
- D3BJ
- GD3BJ
- def2-TZVP
- RMG-database
- atom energy corrections
- bond additivity corrections
- formation enthalpy
author: someuser
date: '2026-09-29'
---

# RMG-database b3lyp2023/def2tzvp AEC+BAC are B3LYP-D3(BJ) (Wu et al. 2024) — do not use with plain B3LYP

## Mistake

Plain Gaussian B3LYP/def2-TZVP energies were corrected with RMG-database's LevelOfTheory(method='b3lyp2023', basis='def2tzvp', software='gaussian') AEC + PBAC (ARC arkane_level_of_theory {b3lyp, def2tzvp, year: 2023}; ARC also picks it by default when no year is given, because it falls back to the latest year). Benzene came out at H298 = 126.8 kJ/mol vs ATcT 83.07 — a size-extensive bias of roughly -E_disp (≈ +45 kJ/mol for benzene, ~+55-60 for C6 species).

## Correct approach

The b3lyp2023 entries (RMG-database PR #648) were fitted on B3LYP-D3(BJ) energies: Wu et al., J. Phys. Chem. A 2024, 128, 4335 (Table 'B: B3LYP-D3BJ/def2-TZVP'; 'For B3LYP-D3 and B97D3, the empirical D3BJ dispersion model in Gaussian is employed'). Use them only with EmpiricalDispersion=GD3BJ (ARC level dispersion: 'empiricaldispersion=gd3bj', with arkane_level_of_theory {b3lyp, def2tzvp, year: 2023} as a stand-in until the key is renamed). For plain B3LYP/def2-TZVP use a plain-B3LYP fit (the group's b3lyp2026 refit on Zeus, RMG-database worktree branch b3lyp2026_aec_bac). camb3lyp2023 is plain CAM-B3LYP per the paper.

## Evidence / source

Paper: rag_drg/papers/chemrxiv-2023-4xlj9-v3.pdf (methods, method table). Zeus 2026-09-29: benzene plain B3LYP AEC-only H298 194.19 kJ/mol; B3LYP-D3(BJ) + same AEC 144.62; RMG reference_sets Benzene.yml b3lyp2023 calculated H298 145.96 kJ/mol; Gaussian D3(BJ) dispersion energy -0.018953 Ha.
