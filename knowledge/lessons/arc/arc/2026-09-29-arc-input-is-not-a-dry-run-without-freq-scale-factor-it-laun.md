---
title: 'ARC(**input) is not a dry run: without freq_scale_factor it launches the Truhlar
  scale-factor jobs on construction'
domain: arc
software: arc
version: Zeus ARC 82614434
doc_type: lesson
status: unreviewed
tags:
- freq_scale_factor
- dry-run
- ARC.__init__
- zeus
- side-effects
author: someuser
date: '2026-09-29'
---

# ARC(**input) is not a dry run: without freq_scale_factor it launches the Truhlar scale-factor jobs on construction

## Mistake

Validated an ARC input on the Zeus login node by constructing `ARC(**input)` directly, assuming construction only parses and checks. The input had no `freq_scale_factor`.

## Correct approach

Constructing the ARC object starts ARC's Truhlar frequency-scale-factor workflow when `freq_scale_factor` is null/absent. From the login node, where qsub works, that submitted real PBS jobs (JOBID-JOBID, incl. pipe array JOBID[]) plus a debris project folder. Set `freq_scale_factor: 1.0` (or the real factor) in the input.yml before any construction; verified that this skips the workflow. To validate an input without side effects use `check_arc_input` / schema validation, not `ARC(**input)`.

## Evidence / source

molecular-discovery docs/DECISIONS.md D217 ("An unrequested computation"), 2026-09-28
