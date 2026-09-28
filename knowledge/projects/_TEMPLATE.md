---
title: "<Project name> - project card (TEMPLATE)"
domain: project
doc_type: scaffold
status: draft
tags: [project]
---
# <Project name>

Copy to `knowledge/projects/<project>/README.md` (or `<project>.md`) and fill in. Keep it
short. It is what an agent needs to write code for the project without asking you the same
questions again.

## Goal and current status

* `<one paragraph>`

## Repository and layout

* Repo: `<url>`; entry points: `<...>`; how to run tests: `<...>`.

## Computational protocol (the source of truth)

| Step | Software | Level of theory | Settings that matter |
|---|---|---|---|
| Conformers | `<...>` | `<...>` | `<...>` |
| Optimisation / freq | `<...>` | `<...>` | `<...>` |
| Single points / properties | `<...>` | `<...>` | `<...>` |

## Data conventions

* Units, file formats, dataset splits, naming, where data lives on the cluster.

## Key papers (put PDFs in `papers/<project>/`, takeaways here)

* `<Author Year>`: `<the one idea we use from it>`.

## Decisions and things agents keep getting wrong

* `<decision / gotcha>`
