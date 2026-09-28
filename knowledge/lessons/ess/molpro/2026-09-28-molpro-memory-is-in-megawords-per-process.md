---
title: 'Molpro: memory card is mega-words per process, not GB total'
domain: ess
software: molpro
doc_type: lesson
status: unreviewed
tags: [memory, words, molpro -n]
date: '2026-09-28'
---

# Molpro: memory card is mega-words per process, not GB total

## Mistake

The agent wrote `memory,8000,m` to give "8 GB" to a 16-process job (`molpro -n 16`),
which asks for 8000 MW x 8 B = 64 GB per process (1 TB total). The job was killed by the scheduler.

## Correct approach

`memory,N,m` is mega-words (8 bytes) **per process**. For 64 GB total over 16 processes:
4 GB per process = 500 MW, so `memory,500,m`, and request a little more than 64 GB from the scheduler.

## Evidence / source

Molpro manual, "Memory allocation" (the MEMORY card). Example lesson that shows the format.
