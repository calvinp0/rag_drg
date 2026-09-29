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
| ESS | ORCA / Gaussian / Q-Chem / Molpro manuals as PDFs **or many HTML pages** (saved, wget-mirrored or crawled), split by bookmarks/headings and labelled `reference` (keywords/usage) or `theory`; books via `_meta.yaml` (e.g. one Gaussian book tagged for 09 and 16) | `sources/<code>/<version>/` (licensed, git-ignored), or built-in crawls |
| ARC | Input reference, examples, settings, source code, and the **`output.yml` JSON schema** (one chunk per field) | ARC GitHub (auto-fetched) + `knowledge/arc/` |
| HPC | Slurm/PBS essentials (submit, query, quota), submit templates for each ESS (programs called by absolute path, no `module load`), one card per cluster with the install paths | `knowledge/hpc/` |
| Projects | Per-project protocol/convention cards, paper notes, paper PDFs | `knowledge/projects/`, `papers/` |
| Lessons | Corrections recorded by agents/people, reviewed via PR | `knowledge/lessons/` |

## Quick start

### Install

rag-drg is a normal Python package (Python >= 3.10), so venv, uv or conda all work. Extras:
`mcp` (MCP server), `pdf` (PDF manuals), `chem` (basis-set checks, SMILES via RDKit),
`st` (local embeddings), `dev` (tests).

**venv**

```bash
git clone <this repo> rag_drg && cd rag_drg
python -m venv .venv && . .venv/bin/activate
pip install -e '.[mcp,pdf,chem]'
```

**uv**, also on a machine that has conda. uv makes the same `.venv`, so nothing else changes.

```bash
conda deactivate                     # repeat until no env is active (not even base): uv would
                                     # otherwise install into the active conda env
uv venv --python 3.12                # uv's own Python, independent of conda
uv pip install --python .venv/bin/python -e '.[mcp,pdf,chem]'
```

**conda**

```bash
conda create -n rag-drg python=3.12 && conda activate rag-drg
pip install -e '.[mcp,pdf,chem]'      # or: conda install -c conda-forge rdkit, then pip the rest
export RAG_DRG_BIN="$(which rag-drg)" # put this in ~/.bashrc, so tools find it without activating
```

**`bin/rag-drg`** is a small launcher that finds the install, trying in order:
1. `$RAG_DRG_BIN`;
2. `.venv/bin/rag-drg`;
3. the active conda env;
4. `rag-drg` on PATH.

It also points `RAG_DRG_CONFIG` at this repository's `rag_drg.yaml`. `.mcp.json`, the hooks,
`deploy/refresh.sh` and `deploy/rag-drg.service` all call it, so they work with any of the three
setups. In a service or cron job, set `RAG_DRG_BIN` there, because conda isn't activated.

For the MCP server, don't use `conda run -n rag-drg rag-drg serve`: `conda run` captures the
program's output by default, and MCP talks over that output. Use the launcher or the env's own
`rag-drg` path.

### First steps

```bash
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
claude mcp add --scope user rag-drg -- /path/to/rag_drg/bin/rag-drg serve
# if you run it from outside the repo:  -e RAG_DRG_CONFIG=/path/to/rag_drg/rag_drg.yaml
```

Or one shared server for the group (recommended once people start recording lessons, so
everyone sees them at once):

```bash
# on the server (see deploy/rag-drg.service and deploy/refresh.sh)
rag-drg serve --transport http --host 0.0.0.0 --port 8765
# each member
rag-drg tokens add <member>            # on the server, once per person; see docs/auth.md
claude mcp add --scope user --transport http rag-drg http://<server>:8765/mcp \
    --header "Authorization: Bearer $RAG_DRG_TOKEN"
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
| `record_lesson(title, mistake, correction, domain, software, version, evidence, tags)` | Write + index a correction; flags near-duplicates and can open a GitHub PR (disabled with `serve --readonly`) |
| `check_input(content, filename, submit_script_content)` | Check an ESS input (+ submit script) for known mistakes: spin/electron parity, memory/cores vs allocation, section structure, missing aux basis, functional/basis support, cluster limits |
| `check_basis(basis, elements \| smiles \| xyz)` | Is this basis defined for these elements (Basis Set Exchange)? ECPs, auxiliary sets, spelling |
| `diagnose_output(content, filename)` | What went wrong in a failed ESS job, and the ordered fixes |
| `list_servers()`, `server_info(name)` | The group's clusters from `servers.yaml` |
| `render_submit_script(server, software, input_file, cores, mem_gb, walltime, ...)` | A ready-to-run Slurm/PBS script with the registered absolute paths, checked against partition limits |
| `check_resources(server, partition, cores, mem_gb, walltime, gpus)` | Does a request fit the partition? |
| `cluster_query(server, what)` | Read-only live queries (jobs, quota, partitions); off unless enabled |

**Tool profiles.** `rag-drg serve --profile minimal` exposes only `search_knowledge`,
`find_tool` and `run_tool` (about 650 tokens instead of 4,800) for clients that load every tool
schema up front, e.g. local models; see [integrations/local-models.md](integrations/local-models.md).

### Command line

| Command | Purpose | Docs |
|---|---|---|
| `rag-drg search "..." [--software X --version V --max-tokens N --json]` | Search from a terminal or a script | |
| `rag-drg compose SPEC.yaml` (or `--program orca --job sp --method ...`) | Compose a checked ESS input + submit script from an explicit spec or your project's protocol file | [docs/compose.md](docs/compose.md) |
| `rag-drg arc check input.yml`, `arc compose input.yml --server zeus`, `arc schema` | ARC: validate input.yml against a schema generated from ARC `main`; compose the runner job (e.g. alon_q on n170) and ARC's `settings.py` / `submit.py` snippets | [docs/arc-input.md](docs/arc-input.md), [docs/arc-run.md](docs/arc-run.md) |
| `rag-drg check-input FILE [--submit SCRIPT]`, `--hook` | Input checker; `--hook` is the Claude Code hook mode (also routes ARC input.yml) | [docs/input-checker.md](docs/input-checker.md) |
| `rag-drg basis NAME --elements C,H,I` | Basis coverage check | [docs/input-checker.md](docs/input-checker.md) |
| `rag-drg diagnose OUTPUT` | Diagnose a failed job | [docs/diagnose.md](docs/diagnose.md) |
| `rag-drg level NAME [--software X]` | Level-of-theory support table | |
| `rag-drg servers list\|show\|validate\|render-cards\|arc-settings\|submit\|check\|query` | Cluster registry | [docs/servers.md](docs/servers.md) |
| `rag-drg eval`, `rag-drg queries report\|to-qa` | Retrieval test set, query log reports | [docs/evaluation.md](docs/evaluation.md) |
| `rag-drg agent-eval validate\|check-graders\|run\|report` | Real group tasks run by an agent with and without rag-drg, graded pass/fail | [docs/agent-eval.md](docs/agent-eval.md) |
| `rag-drg lessons report\|pr\|similar\|tidy` | Lesson review workflow | [docs/lessons.md](docs/lessons.md) |
| `rag-drg zotero sync\|status` | Zotero library sync | [docs/zotero.md](docs/zotero.md) |
| `rag-drg tokens add\|list\|revoke` | Per-person tokens for the HTTP server | [docs/auth.md](docs/auth.md) |
| `rag-drg tools-schema --format openai\|ollama` | Tool definitions for local-model frameworks | [integrations/local-models.md](integrations/local-models.md) |
| `rag-drg check-pdf`, `ingest`, `fetch`, `lint`, `stats`, `sources` | Content management | [sources/README.md](sources/README.md) |

### Using the shared server without installing anything

Group members only need one file: [`integrations/rag-drg-remote`](integrations/rag-drg-remote)
(standard-library Python) plus their token. It checks inputs, diagnoses outputs and searches via
the server, and works as the Claude Code hook. What runs where (shared server vs. your machine
vs. a per-user install for live cluster queries) is explained in
[docs/remote-client.md](docs/remote-client.md).

### Automatic input checks in Claude Code

Add the hook from [`integrations/claude-code/hooks.remote.json`](integrations/claude-code/hooks.remote.json)
(thin client, no install) or [`integrations/claude-code/hooks.json`](integrations/claude-code/hooks.json)
(local install) to `~/.claude/settings.json`, using the absolute path of the command. Every time an agent
writes an input file or submit script, it is checked; errors are fed back to the agent, which
then fixes them before anything is submitted.

## Growing the knowledge base (the part that matters)

The tool is only as good as what's in it. In order of value:

1. **Describe your clusters in `servers.yaml`** (copy `servers.example.yaml`; see
   [docs/servers.md](docs/servers.md)): partitions and limits, **absolute install paths of each
   ESS**, scratch, storage and quota commands. `rag-drg servers render-cards` then generates the
   cluster cards, `rag-drg servers arc-settings` the ARC `servers` block, and submit scripts and
   input checks use the real limits and paths.
2. **Review the draft cards** in `knowledge/ess/`, `knowledge/arc/`, `knowledge/hpc/`. They
   were written from general knowledge and marked `status: draft`; check each against the
   manual/your experience, fix, and set `status: verified`.
3. **Add the licensed manuals**, one PDF per manual and version, no manual splitting needed
   (see [`sources/README.md`](sources/README.md)): ORCA in `sources/orca/{5,6}/`, Gaussian in
   `sources/gaussian/{09,16}/`, Q-Chem in `sources/qchem/6.1/`, Molpro in
   `sources/molpro/{2024,2026}/`. HTML manuals (ORCA 6, Gaussian keyword pages) can be saved,
   wget-mirrored or crawled; books go in e.g. `sources/gaussian/book/` with a `_meta.yaml`;
   run `rag-drg check-pdf` to see whether a PDF needs OCR first. Exported cluster
   docs in `sources/hpc/<cluster>/`. These are git-ignored, so they go on the shared server or
   each person's copy. Web crawls for the ORCA 6 / Gaussian / Molpro online docs are
   pre-configured but `enabled: false`: enable them in `rag_drg.yaml` if the site terms allow it.
   To get the Molpro online manual as one PDF instead, run `python scripts/molpro_manual_pdf.py
   --out sources/molpro/<version>/molpro_manual_web.pdf` (needs Chrome/Chromium; standard library only).
   Also fill the `unknown` cells in `knowledge/ess/levels_of_theory.yaml` as you check them.
4. **Only group-wide knowledge belongs here.** Project-specific protocols and notes stay in each
   project's own repository (the compose tools accept a protocol file by path);
   `knowledge/projects/_TEMPLATE.md` is there for anything a project wants to share group-wide.
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
