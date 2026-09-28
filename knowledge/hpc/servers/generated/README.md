---
title: Generated cluster cards (how they are made)
domain: hpc
doc_type: scaffold
status: draft
tags: [cluster, server, servers.yaml, generated]
---
# Generated cluster cards

Every `<cluster>.md` in this folder is written by `rag-drg servers render-cards` from
`servers.yaml` at the repository root (format: `docs/servers-spec.md`, how-to: `docs/servers.md`).
Do not edit them by hand: change `servers.yaml`, regenerate, and commit both.
`rag-drg lint` reports cards that are out of date or belong to a cluster that no longer exists.
