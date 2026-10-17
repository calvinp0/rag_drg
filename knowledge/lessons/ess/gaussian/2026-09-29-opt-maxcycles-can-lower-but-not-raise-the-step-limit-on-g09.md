---
title: Opt MaxCycles can lower but not raise the step limit on G09 D.01; the 'maximum
  allowed number of steps' figure never shows it
domain: ess
software: gaussian
version: 09
doc_type: lesson
status: unreviewed
tags:
- berny
- maxcycles
- l9999
- optimization
author: calvin
date: '2026-09-29'
---

# Opt MaxCycles can lower but not raise the step limit on G09 D.01; the 'maximum allowed number of steps' figure never shows it

## Mistake

l9999 fix: restart with opt=(maxcycles=N) at 2-3x the previous count. And: in the line 'Number of steps in this run= A maximum allowed number of steps= B', read the SECOND number (B, 'maximum allowed number of steps') as the limit Gaussian applied.

## Correct approach

On G09 Revision D.01 the applied Berny limit is min(MaxCycles, max(100, 6*NAtoms)): MaxCycles= only LOWERS the limit; requesting more than the default has no effect (measured on 437 logs). MaxCycle= (singular) and IOp(1/6=N) were shown to LOWER it identically, but neither has been tested in the RAISING direction on this build, so do not assume either can raise it -- that is untested, not established. In the line 'Number of steps in this run= A maximum allowed number of steps= B', A is the applied limit; B is only the ceiling max(100, 6*NAtoms) that a requested MaxCycles is clipped to, and never reflects the keyword (step-cap tests: A=5, B=100; 39-atom logs with maxcycles=200: A=200, B=234). A equals 'Step number k out of a maximum of M' and '-- Number of steps exceeded, NStep= M', which also give the applied limit. So restarting an l9999 with a larger maxcycles does nothing when the default already binds. Restarting from the last geometry, or switching to RFO/GDIIS/cartesian, is general practice and UNTESTED on this build: none of these remedies was run in the evidence below, so treat them as options to try, not as established fixes.

## Evidence / source

471 of 471 returned opt logs (chemprop_cmpnn cohort DFT phase, 2026-09) obey min(requested, max(100,6N)); 437 requested 200 with a default below 200 and every one applied the default, never 200; 16 with NAtoms>=34 applied 200 below their default of 234 etc. Controlled test on propanal (10 atoms) from one displaced start: opt=(maxcycles=5), opt=(maxcycle=5) and IOp(1/6=5,2/9=2000) each stopped at NStep=5 ('Step number 5 out of a maximum of 5') while all three printed 'maximum allowed number of steps= 100'. Commit and data: paper/literature_cohort/dft_phase_decks_20260916/STEPCAP_RESULT.md, d083_results/stepcap_test/. 'Number of steps in this run' equals the applied limit in 4,681 of 4,681 distinct G09 D.01 logs (chemprop_cmpnn corpus, 2026-09-30). When the route carries no step token the default applied limit is not max(100,6N) in general: the only two such optimisation logs (plain opt on 8 atoms) applied 33.
