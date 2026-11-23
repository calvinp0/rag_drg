---
title: ORCA 6 removed the analytic (RI-)MP2 Hessian; "MP2 Hessian is analytic" is
  stale for ORCA 6
domain: ess
software: orca
version: '6'
doc_type: lesson
status: unreviewed
tags:
- hessian
- frequencies
- mp2
- analytic
- numfreq
- tckdb
- hessian_method
author: someuser
date: '2026-10-31'
---

# ORCA 6 removed the analytic (RI-)MP2 Hessian; "MP2 Hessian is analytic" is stale for ORCA 6

## Mistake

The KB's ORCA 6.1 TS section (chunk 16786: "For the HF, DFT methods and MP2, the analytic Hessian evaluation is available") and gotcha chunk 272 ("analytic for HF, DFT and MP2") state MP2 frequencies are analytic, which is wrong for ORCA 6.

## Correct approach

In ORCA 6.x the analytic (RI-)MP2 Hessian no longer exists: MP2 frequencies are numerical. Treat MP2 `Freq` on ORCA 6 as numerical (finite difference), never analytic. HF and DFT (outside the documented exclusions: double hybrids, VV10/NL, RI-JK, X2C) remain analytic.

## Evidence / source

ORCA 6.0.0 detailed changelog (faccts.de): "Removed the slow and limited analytic (RI-)MP2 Hessian code". Found 2026-10-31 while verifying ARC PR #1059 freq_hessian_method.
