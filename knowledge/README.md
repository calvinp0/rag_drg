# Curated knowledge cards

This folder is the core of the knowledge base: short, high-signal Markdown cards
written and reviewed by the group. Search ranks them above raw manuals, so
**a card should only contain things we are confident about.**

```
knowledge/
  ess/<software>/     ORCA, Gaussian, Psi4, Molpro, PySCF: essentials + gotchas
  ess/capabilities.md cross-code capability/naming matrix
  arc/                ARC input/output schema, settings, gotchas
  hpc/                scheduler essentials, submit templates, one card per cluster
  projects/<name>/    per-project conventions, protocols, key-paper notes
  lessons/            corrections recorded by agents/people (see below)
```

## Card format

```markdown
---
title: ORCA input essentials
domain: ess            # ess | arc | hpc | project | literature
software: orca         # orca | gaussian | psi4 | molpro | pyscf | arc | slurm | pbs | ...
version: ["5", "6"]    # optional; omit when it applies to all versions
doc_type: card         # card | gotcha | template | schema | lesson
status: draft          # draft -> verified (after review) ; outdated to demote
tags: [maxcore, pal]
---
# Title
## Short sections with headings (each section becomes a search chunk)
```

Rules of thumb:

* One topic per `##` section, 5-40 lines each. Headings are part of the search index.
* Put the **exact syntax** in code blocks, and say which version it applies to.
* Lead with what agents get wrong (the "gotcha"), then the correct form.
* Cite the manual section or a test calculation in the card when possible.
* `status: draft` means someone wrote it from memory and it still needs checking
  against the manual. Flip it to `verified` in a PR once someone has checked it.

## Lessons

`lessons/` is filled by the `record_lesson` MCP tool (or `rag-drg lesson ...`)
whenever an agent is corrected. New lessons have `status: unreviewed` and are
searchable at once. Review them in PRs: fix wording, set `status: verified`, or
fold them into the relevant card and delete the lesson.
