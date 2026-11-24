---
title: orca_vpot in ORCA 6.0.0 needs KeepInts, and its PName is a density-container
  name not a file
domain: ess
software: orca
version: 6.0.0
doc_type: lesson
status: unreviewed
author: someuser
date: '2026-10-03'
similar:
- knowledge/lessons/ess/orca/2026-10-03-orca-6-0-0-orca-vpot-needs-input-k-tmp-which-only-keepints-k.md
---

# orca_vpot in ORCA 6.0.0 needs KeepInts, and its PName is a density-container name not a file

## Mistake

Tried to compute electrostatic-potential surface descriptors with `orca_vpot` after an ordinary ORCA 6.0.0 single point, following the manual's description of its arguments. It failed instantly with `[file gtovpot.cpp, line 1193]: Prescreening matrix could not be read!` and exit status 255. Default settings, `NoRI`, `RIJCOSX`, `SCFMode Direct` and plain HF all failed the same way (measured on Zeus, job JOBID, 12-atom CHNO molecule, wB97M-D3BJ/def2-TZVPP).

Two further mismatches with the documentation cost further debugging jobs:
- the manual's `PName` argument reads as though it names a file on disk; there is no `.scfp` file in 6.0.0;
- the manual says the output file has three columns; it actually has four.

## Correct approach

1. **`KeepInts` is REQUIRED on the single point that produces the wavefunction.** `orca_vpot` reads `<base>.K.tmp`, which ORCA deletes at normal termination. Without `! KeepInts` that file is gone before vpot runs, and the failure is the "Prescreening matrix could not be read!" message above, not anything that names the real cause. Note the disk cost of keeping integrals is unmeasured at larger sizes.

2. **`PName` is `<base>.scfp`, which is a density NAME inside `<base>.densities`, not a path to a file.** Pass the string `<base>.scfp` even though no such file exists on disk.

3. **Input points are in BOHR**, and the **output has FOUR columns (x, y, z, V)**, not the three the manual states. A reader that assumes three columns will mis-parse silently.

4. Working invocation: `orca_vpot <base>.gbw <base>.scfp points.xyz out.pot`, with the single point run using `! KeepInts`.

5. **Verified correct independently**, which is worth doing before trusting the numbers: ORCA 6.0.0 + `orca_vpot` at PBE0/def2-TZVPP was compared against PySCF's own density and potential integrals at 6,000 points on the same surface — maximum difference 2.0e-5 au, RMS 0.003 kcal/mol, r = 0.99999999. (PBE0 rather than wB97M-D3BJ because PySCF's wB97M-V is not the same functional.)

6. **ORCA 6.1's changelog mentions a vpot "contraction" fix whose scope we do not understand**, so correctness beyond PBE0/def2-TZVPP on C/H/N/O is unverified on 6.0.0.

Related, for anyone computing these descriptors: `orca_plot`'s own ESP option (type 43) is ORCA **6.1+** and is NOT available in 6.0.0 — but it is not needed, because `orca_vpot` gives the potential directly. A density cube from `orca_plot` is still useful for constructing the 0.001 e/bohr^3 isodensity surface. GFN2-xTB `--esp` is about 1000x cheaper but is NOT a substitute: on the same points it correlated at r = 0.963 yet stretched the potential by ~1.30x, giving sigma^2_tot 1335 against DFT's 713 (~1.9x). The surface definition also matters: the same DFT wavefunction gave sigma^2_tot 713 on xtb's van der Waals grid and 569 on the 0.001 isodensity surface, so fix and record the surface before comparing any numbers.
