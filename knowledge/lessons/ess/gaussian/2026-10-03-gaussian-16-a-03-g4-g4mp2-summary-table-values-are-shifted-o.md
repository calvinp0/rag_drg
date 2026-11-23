---
title: 'Gaussian 16 A.03 G4/G4MP2: summary-table values are shifted one label pair;
  "G4(0 K)" is NOT E0'
domain: ess
software: gaussian
version: '16'
doc_type: lesson
status: unreviewed
tags:
- G4
- G4MP2
- composite
- CBS-QB3
- summary table
- E0
- archive
- Arkane
- thermochemistry
- parser
author: someuser
date: '2026-10-03'
---

# Gaussian 16 A.03 G4/G4MP2: summary-table values are shifted one label pair; "G4(0 K)" is NOT E0

## Mistake

In Gaussian 16 A.03 G4 and G4MP2 output logs, the number printed after "G4(0 K)=" / "G4MP2(0 K)=" was read as the 0 K energy, E0 = Eelec + ZPE. Example from a methanol G4 log (G16 A.03):
  E(QCISD(T))= -115.374482  E(Empiric)= -0.048629
  E(CCSD(T))=  -0.048629    E(Empiric)= -0.012034
  ...                       DE(HF)= -115.651764
  G4(0 K)= -115.648433      G4 Energy= -115.647489
Every value in the table sits one label pair too late. The value labelled "G4(0 K)" is really the 298 K thermal energy (G4); for G4MP2 it is the 298 K enthalpy. These are 8.75 and 11.2 kJ/mol too high. Any parser that greps "G4(0 K)=" (TCKDB's first draft did; Arkane reads the same lines, so check it) stores the wrong E0.

## Correct approach

Take E0 from the archive block entry "\G4=" or "\G4MP2=". In the methanol example, \G4=-115.6517642, which is the value printed under the "DE(HF)=" label. Verify any summary table with the manual's identities before trusting it:
  (Energy - E0) == (E(Thermal) - E(ZPE)) within about 2e-6 Eh
  (Enthalpy - Energy) == RT (0.000944 Eh at 298.15 K)
Correctly labelled blocks (CBS-QB3, ROCBS-QB3, CBS-4M, G3) pass both identities to about 1e-6. The shifted G4 and G4MP2 blocks fail by 0.0024 and 0.030 Eh. TCKDB (PR #656) now refuses G4/G4MP2 summary blocks, applies this identity check to every composite block, and cross-checks E0 against the archive value.

## Evidence / source

TCKDB PR #656 review (2026-10-02). Fixtures: backend/tests/fixtures/gaussian_composite/g4_methanol_g16.out and the G4MP2 log, both G16 A.03, from ARC/RMG-Py test data. Archive values: \G4=-115.6517642, \G4MP2=-115.571053; parsed "(0 K)" values were -115.648433 and -115.566778. Identities from the Gaussian 09 manual (k_g1.htm): G2 (0 K) = Eelec + ZPE; Energy = E0 + thermal; Enthalpy = E + RT. Not yet checked on other Gaussian 16 revisions or on Gaussian 09.
