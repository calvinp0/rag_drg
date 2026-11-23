---
title: 'ORCA 6: RIJK gradients are unavailable for range-separated functionals (wB97M-D3BJ);
  use RIJONX def2/J'
domain: ess
software: orca
version: 6.0.0
doc_type: lesson
status: unreviewed
tags:
- RIJK
- RIJONX
- range-separated
- wB97M-D3BJ
- gradient
author: someuser
date: '2026-09-29'
---

# ORCA 6: RIJK gradients are unavailable for range-separated functionals (wB97M-D3BJ); use RIJONX def2/J

## Mistake

Ran a RI-JK (`RIJK`) optimisation/gradient with the range-separated functional wB97M-D3BJ in ORCA 6.0.0. It stopped within a minute: `Error (SHARK/RIJK/Gradient/RangeSep): Sorry, the range sep RIJK gradient has an unresolved bug`. Reviews had only checked that RI-JK cannot do analytic Hessians, not gradients.

## Correct approach

Use `RIJONX def2/J` (RI for Coulomb, exact exchange, no COSX grid) for range-separated functionals when RIJK is not usable. Measured afterwards (D202): RIJONX energy+gradient and `Opt NumFreq VeryTightOpt` ran fine (residual force RMS 1.66e-5 Eh/bohr). RIJONX is far more expensive than RIJCOSX; whether it supports analytic Hessians was not tested.

## Evidence / source

molecular-discovery docs/DECISIONS.md D200 execution addendum (array JOBID[], 2026-09-26) and D202
