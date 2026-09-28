# rag-drg: a shared knowledge base for the group's coding agents

Coding agents (Claude Code, local models) keep making the same mistakes with our tools:
ORCA `%maxcore` taken as total memory, PySCF `spin` taken as the multiplicity, invented Psi4
keywords, ARC input keys that don't exist, submit scripts that don't match the cluster.
Each person then corrects them again, and agents spend tokens re-reading the same manuals.

`rag-drg` fixes this with **one shared, searchable knowledge base** that every agent queries
before it writes ESS inputs, ARC code or HPC scripts. Corrections are written back into it,
so a mistake only has to be fixed once.

```
            sources                                   index                      agents
 ┌──────────────────────────────┐   rag-drg ingest   ┌─────────────────┐  MCP   ┌───────────────┐
 │ knowledge/  curated cards    │ ─────────────────▶ │ SQLite          │ ◀────▶ │ Claude Code   │
 │ knowledge/lessons/ (agents)  │                    │  FTS5 (BM25)    │ stdio/ │ local models  │
 │ ARC repo: docs, examples,    │                    │  + vectors      │  HTTP  │ (MCP clients) │
 │   source, output.yml schema  │                    │  (optional)     │        └───────────────┘
 │ Psi4 docs + read_options.cc  │                    └─────────────────┘  CLI   ┌───────────────┐
 │ PySCF examples               │                            ▲         ◀────▶ │ scripts, other│
 │ ORCA/Gaussian/Molpro manuals │                            │                 │ agents (JSON) │
 │ cluster guides, papers       │                  record_lesson ◀───────────── └───────────────┘
 └──────────────────────────────┘
```

## What's in it

| Domain | Content | Where it comes from |
|---|---|---|
| ESS | Essentials + gotchas for ORCA 5/6, Gaussian 09/16 (CPU/GPU), Q-Chem 6.1, Psi4, Molpro 2024/2026, PySCF; cross-code spin-convention and naming matrix | `knowledge/ess/` (curated) |
| ESS | **Level-of-theory support table**: which code supports each method/functional/dispersion/solvation model and how each one writes it | `knowledge/ess/levels_of_theory.yaml` (`lookup_level_of_theory`, `rag-drg level`) |
| ESS | Psi4 manual + **every Psi4 keyword with default/allowed values** (parsed from `read_options.cc`) | Psi4 GitHub (auto-fetched) |
| ESS | PySCF `examples/` (idiomatic usage for every module) | PySCF GitHub (auto-fetched) |
| ESS | ORCA / Gaussian / Q-Chem / Molpro manuals, split along the PDF bookmarks and labelled `reference` (keywords/usage) or `theory` | PDFs you drop in `sources/<code>/<version>/` (licensed), or optional web crawls |
| ARC | Input reference, examples, settings, source code, and the **`output.yml` JSON schema** (one chunk per field) | ARC GitHub (auto-fetched) + `knowledge/arc/` |
| HPC | Slurm/PBS essentials (submit, query, quota), submit templates for each ESS (programs called by absolute path, no `module load`), one card per cluster with the install paths | `knowledge/hpc/` |
| Projects | Per-project protocol/convention cards, paper notes, paper PDFs | `knowledge/projects/`, `papers/` |
| Lessons | Corrections recorded by agents/people, reviewed via PR | `knowledge/lessons/` |

## Quick start

```bash
git clone <this repo> rag_drg && cd rag_drg
python -m venv .venv && . .venv/bin/activate
pip install -e '.[mcp,pdf]'          # add ,st for local sentence-transformers embeddings

rag-drg ingest --fetch               # clones ARC/Psi4/PySCF docs (~200 MB) and builds index/ (~15 MB), ~15 s
rag-drg search "ORCA TS optimisation hessian" --software orca
rag-drg search "GEOM_MAXITER"
rag-drg search "output.yml transition_states" --software arc
rag-drg search "CASSCF orbital optimisation" --software orca --doc-type theory
rag-drg level "wb97xd/def2tzvp" --software orca   # is it supported, and how is it written?
```

### Connect Claude Code

Each person, local index (simplest):

```bash
claude mcp add --scope user rag-drg -- /path/to/rag_drg/.venv/bin/rag-drg serve
# if you run it from outside the repo:  -e RAG_DRG_CONFIG=/path/to/rag_drg/rag_drg.yaml
```

Or one shared server for the group (recommended once people start recording lessons, so
everyone sees them at once):

```bash
# on the server (see deploy/rag-drg.service and deploy/refresh.sh)
rag-drg serve --transport http --host 0.0.0.0 --port 8765
# each member
claude mcp add --scope user --transport http rag-drg http://<server>:8765/mcp
```

Then give agents the habit:

* copy `integrations/claude-code/skills/group-knowledge/` to `~/.claude/skills/`, and/or
* append `integrations/claude-code/CLAUDE.md.snippet` to `~/.claude/CLAUDE.md` or to your project's `CLAUDE.md`.

Local models: see [`integrations/local-models.md`](integrations/local-models.md) (MCP, or the
`rag-drg search --json` CLI as a tool).

### MCP tools

| Tool | Purpose |
|---|---|
| `search_knowledge(query, software, version, domain, doc_type, k)` | Hybrid search; results show scope, doc type, review status, `chunk_id`, source path/URL |
| `get_context(chunk_id, neighbors)` | Surrounding chunks from the same file |
| `read_document(path)` | A whole card or template (e.g. `slurm_orca`) |
| `list_documents(domain, software, doc_type)` | Browse what exists (e.g. all HPC templates) |
| `lookup_level_of_theory(name, software)` | Support/keyword table for a method across Gaussian, ORCA, Q-Chem, Psi4, Molpro, PySCF |
| `list_knowledge_sources()` | Index statistics: sources, software, versions |
| `record_lesson(title, mistake, correction, domain, software, version, evidence, tags)` | Write + index a correction (disabled with `serve --readonly`) |

## Growing the knowledge base (the part that matters)

The tool is only as good as what's in it. In order of value:

1. **Fill in the cluster cards.** Copy `knowledge/hpc/servers/_TEMPLATE.md` to one card per
   cluster: partitions, limits, **absolute install paths of each ESS**, scratch paths, quota
   commands, ARC `servers` entry.
2. **Review the draft cards** in `knowledge/ess/`, `knowledge/arc/`, `knowledge/hpc/`. They
   were written from general knowledge and marked `status: draft`; check each against the
   manual/your experience, fix, and set `status: verified`.
3. **Add the licensed manuals**, one PDF per manual and version, no manual splitting needed
   (see [`sources/README.md`](sources/README.md)): ORCA in `sources/orca/{5,6}/`, Gaussian in
   `sources/gaussian/{09,16}/`, Q-Chem in `sources/qchem/6.1/`, Molpro in
   `sources/molpro/{2024,2026}/`, exported cluster
   docs in `sources/hpc/<cluster>/`. These are git-ignored, so they go on the shared server or
   each person's copy. Web crawls for the ORCA 6 / Gaussian / Molpro online docs are
   pre-configured but `enabled: false`: enable them in `rag_drg.yaml` if the site terms allow it.
   Also fill the `unknown` cells in `knowledge/ess/levels_of_theory.yaml` as you check them.
4. **Project cards and paper notes**: `knowledge/projects/<project>/` (see the
   `vae-ess-nn` skeleton) and `papers/<project>/` (PDFs git-ignored, `notes.md` committed).
5. **Review lessons** that agents record: they arrive as `status: unreviewed` files in
   `knowledge/lessons/`; merge them via PR (verify, or fold into a card).

`rag-drg lint` validates front matter (run in CI). See [`knowledge/README.md`](knowledge/README.md) for the card format.

## How retrieval works

* **Chunking follows document structure**: Markdown/RST headings, Python functions/classes
  (ARC source), JSON-schema properties (ARC `output.yml`), Psi4 option subsections, PDF
  bookmarks (with page ranges; theory vs. keyword sections labelled), HTML headings, and one
  chunk per method in the level-of-theory table. Every chunk carries a heading breadcrumb title and metadata
  (`domain`, `software`, `version`, `doc_type`, `status`, `url`).
* **Hybrid ranking**: SQLite FTS5 BM25 over the whole query, an exact-phrase pass for
  identifiers (`GEOM_MAXITER`, `opt=(ts,calcfc)`, `def2-TZVP`), and, if configured,
  dense-vector similarity, fused with reciprocal-rank fusion.
* **Curated knowledge first**: lessons/gotchas/cards get a ranking boost, `verified` > `draft` >
  `unreviewed`, and at most 2 chunks per file are returned so one manual can't flood the results.
* **Version-aware filters**: `version="6"` matches chunks tagged 6, 6.0, or `5|6`, plus every
  chunk without a version.
* **Incremental**: re-ingest only touches changed chunks; embeddings are only computed for new ones.

Embeddings are optional. Keyword search already handles most agent queries (they are mostly
exact keywords); enable `sentence-transformers` or an OpenAI-compatible endpoint (Ollama,
vLLM) in `rag_drg.yaml` for paraphrased questions.

## Repository layout

```
rag_drg/            python package (chunking, store, search, ingest, lessons, MCP server, CLI)
rag_drg.yaml        source configuration
knowledge/          curated cards, templates, lessons  (committed, reviewed via PR)
papers/  sources/   local PDFs/HTML (git-ignored)
integrations/       Claude Code skill + CLAUDE.md snippet, local-model notes
deploy/             systemd unit and nightly refresh script for a shared server
tests/              pytest suite
```
