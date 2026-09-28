# Local documentation sources

Drop licensed manuals and exported cluster guides here (git-ignored), then `rag-drg ingest`:

| Folder | Content |
|---|---|
| `orca/5/`, `orca/6/` | ORCA manual PDFs (download from the ORCA forum) |
| `gaussian/09/`, `gaussian/16/` | Gaussian user reference PDFs/HTML, GPU/Linda notes, per version |
| `qchem/6.1/` | Q-Chem 6.1 user manual PDF |
| `molpro/2024/`, `molpro/2026/` | Molpro manual PDFs for each version |
| `hpc/<cluster>/` | Cluster user guides exported as HTML/PDF/Markdown |

PDF ingestion needs `pip install 'rag-drg[pdf]'`.

## How PDFs are split

Keep **one PDF per manual and version**; there is no need to split them by hand. The indexer
reads the PDF's bookmarks and makes one entry per section, titled with the full path and page
range, e.g. `orca_manual_6_0_1 > Geometry Optimization > Transition States (pp. 312-314)`.
Each section is labelled either

* `reference`: keywords, input blocks, options, examples, usage. This is what agents need to
  write inputs, and it is the default.
* `theory`: method background (sections titled theory/background/formalism, or mostly equations).

Agents filter with `doc_type="reference"` or `doc_type="theory"`. A PDF without bookmarks
falls back to one entry per page. Check what you got with `rag-drg ingest --source orca6-manual`
followed by `rag-drg search "..." --software orca --version 6`.
