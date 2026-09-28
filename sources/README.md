# Local documentation sources

Drop licensed manuals and exported cluster guides here (git-ignored), then `rag-drg ingest`:

| Folder | Content |
|---|---|
| `orca/5/`, `orca/6/` | ORCA manual PDFs (download from the ORCA forum) |
| `gaussian/` | Gaussian 09/16 user reference PDFs/HTML, GPU/Linda notes |
| `molpro/2024/`, `molpro/2026/` | Molpro manual PDFs for each version |
| `hpc/<cluster>/` | Cluster user guides exported as HTML/PDF/Markdown |

PDF ingestion needs `pip install 'rag-drg[pdf]'`.
