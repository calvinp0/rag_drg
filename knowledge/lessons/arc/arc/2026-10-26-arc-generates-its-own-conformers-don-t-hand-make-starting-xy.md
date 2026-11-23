---
title: ARC generates its own conformers; starting XYZ geometries are optional
domain: arc
software: arc
doc_type: lesson
status: unreviewed
tags:
- conformers
- xyz
- input.yml
- conformer generation
author: someuser
date: '2026-10-26'
---

# ARC generates its own conformers; starting XYZ geometries are optional

## Mistake

An agent brief assumed a conformer search had to be run outside ARC to produce starting XYZ geometries for each molecule before ARC could be used.

## Correct approach

ARC performs conformer generation itself; giving it the species (SMILES) is enough. Supplying starting geometries is also fine, but not required. Choose whichever is simpler for the study. If the same conformer must be followed across several levels of theory, make sure the route chosen keeps that correspondence (reuse one conformer set for every level, or match minima afterwards and say so).

## Evidence / source

Corrected by a group member (ARC developer/user), 2026-10-26, then clarified the same day: starting geometries are acceptable, ARC's own generation is the default capability.
