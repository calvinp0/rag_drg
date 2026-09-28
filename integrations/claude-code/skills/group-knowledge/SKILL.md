---
name: group-knowledge
description: Consult the research group's knowledge base (rag-drg MCP server) before writing or editing electronic-structure inputs (ORCA, Gaussian, Psi4, Molpro, PySCF), ARC input/settings/output parsing code, HPC submit scripts or cluster commands, and when a user corrects a mistake in any of those areas. Use it whenever the task involves quantum-chemistry software keywords, levels of theory, ARC, Slurm/PBS, or queue/quota questions.
---

# Group knowledge base

The `rag-drg` MCP server holds the group's curated cards, lessons learned, the ARC
repository (docs, examples, source, output.yml schema), Psi4 and PySCF docs/examples,
ESS manuals and cluster notes.

## Before writing ESS / ARC / HPC code

1. `search_knowledge(query, software=..., version=...)` with the exact keyword or task,
   e.g. `("TS optimisation numerical hessian", software="orca", version="6")`,
   `("output.yml transition_states", software="arc")`, `("GEOM_MAXITER", software="psi4")`.
2. Follow results marked `lesson` or `gotcha` over your own prior knowledge. `verified` beats
   `draft`/`unreviewed`; raw manuals (`reference`) and source code (`code`) are ground truth for syntax.
3. Need more around a hit? `get_context(chunk_id)`. Need a whole template/card? `read_document(path)`.
4. For cluster work, `list_documents(domain="hpc")` first: use the cluster card and the
   matching submit template instead of writing a script from scratch.

## When you are corrected

If the user corrects you on ESS syntax/capabilities, ARC usage, HPC usage or project
conventions (or the knowledge base turned out to be wrong), call `record_lesson` with a
one-line title, the wrong snippet, the right snippet, `domain`, `software` and `version`.
Then tell the user the lesson file path so they can commit it and open a PR.

## Do not

* Do not guess keyword names or defaults when a search can confirm them.
* Do not mix energies from different codes/functional definitions (see `ess/capabilities.md`).
