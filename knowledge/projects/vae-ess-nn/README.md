---
title: VAE + ESS + NN molecular property prediction - project card
domain: project
doc_type: card
status: draft
tags: [vae, variational autoencoder, neural network, property prediction, latent space, dataset, pipeline]
---
# VAE + ESS + NN property prediction pipeline

> Skeleton to be filled in by the project owner. Replace every `<...>`, then set `status: verified`.

## Goal and current status

* A VAE learns a molecular latent space, ESS calculations provide target properties, and a
  neural network predicts properties from the latent representation.
* Status: `<...>`

## Repository and layout

* Repo: `<url>`; training entry point: `<...>`; ESS driver: `<...>`; tests: `<...>`.

## Computational protocol (source of truth for ESS labels)

| Step | Software | Level of theory | Settings that matter |
|---|---|---|---|
| Conformer search | `<...>` | `<...>` | `<...>` |
| Optimisation + freq | `<...>` | `<...>` | `<...>` |
| Property calculation | `<...>` | `<...>` | `<...>` |

* All labels in one dataset must come from **one** code + functional definition (see
  "Functional names are not portable" in `ess/capabilities.md`).

## Data conventions

* Molecule representation: `<SMILES / SELFIES / graph>`; canonicalisation: `<RDKit canonical SMILES>`.
* Property units: `<Hartree / eV / kcal/mol>`; stored in `<file/db>`.
* Splits: `<random / scaffold>`, seed `<...>`.

## Key papers (PDFs in `papers/vae-ess-nn/`, notes in `papers/vae-ess-nn/notes.md`)

* `<Author Year>`: `<idea used>`.

## Decisions and recurring agent mistakes

* `<...>`
