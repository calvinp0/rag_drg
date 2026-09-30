# Measuring and improving retrieval

Two tools tell us whether agents get the right document when they ask:

* **`rag-drg eval`** runs a fixed set of real questions (`eval/qa.yaml`) through search and
  reports how often the document that answers each question comes back.
* **The query log** (`conf.d/querylog.yaml`) records what agents actually ask the MCP server.
  `rag-drg queries report` shows what they ask, what returns nothing and what returns only raw
  manual text; `rag-drg queries to-qa` turns those queries into new eval questions.

The loop: the log shows gaps, the group writes cards for them, the questions go into
`eval/qa.yaml`, and `rag-drg eval` shows the fix worked and stays fixed (CI runs it).

## Running the evaluation

```bash
rag-drg ingest --fetch                  # full index: curated + ARC + Psi4 + PySCF
rag-drg eval                            # hit@1, hit@6, MRR overall and per tag; exit 1 if hit@6 < 0.8
rag-drg eval --show-failures            # plus the top 3 results of every miss
rag-drg eval --tags orca memory         # only items tagged orca or memory
rag-drg eval --ids arc-restart --show-failures
rag-drg eval --json > eval-$(date +%F).json
rag-drg eval --k 3 --min-hit-rate 0.9   # stricter
```

Items whose expected documents come from a source that is not in the index are **skipped**
and listed with the reason (e.g. `source(s) not indexed: psi4`), so the same file works on a
curated-only index. Items whose `expect` is still `TODO` are skipped too.

### Reading the numbers

| Metric | Meaning | What to look at |
|---|---|---|
| **hit@k** (k = 6, what the MCP tool returns by default) | Fraction of questions where an answering document is anywhere in the top k | The main number. Below ~0.9 agents regularly miss what we already know. |
| **hit@1** | Fraction where it is the very first result | Agents read the first result most carefully; low hit@1 with high hit@k means ranking, not coverage, is the problem. |
| **MRR** | Mean of 1/rank of the first answering result (0 if none) | One number combining both; 1.0 = always first, 0.5 = typically second. |

Per-tag rows show where it breaks: `paraphrase` low is expected with keyword search only;
`psi4` or `arc` low means those sources need better chunking or a card. A failure listing
shows what came back instead. Typical fixes, in order:

1. **Nothing in the index answers it** -> write a card (the most valuable fix).
2. **A card answers it but ranks low** -> add the words people use to the card (a heading, the
   `tags:` front matter, or a sentence using the agent's phrasing).
3. **Raw manual/code chunks crowd it out** -> pass a filter in the agent instructions, or file an
   issue about ranking (`rag_drg/search.py` boosts).
4. The expectation is wrong (another document answers it too) -> add that document to `expect`.

Do **not** reword a question until it passes: the questions are how people ask, and the point is
to change the knowledge base, not the test.

### Current results (keyword search only, 2026-09-28, 51 questions)

| Index | Evaluated | hit@1 | hit@6 | MRR |
|---|---|---|---|---|
| Full (curated + lessons + ARC + Psi4 + PySCF) | 51 | 0.726 | 0.941 | 0.797 |
| CI (curated + lessons only; 12 items skipped) | 39 | 0.769 | 0.974 | 0.830 |
| `paraphrase` tag only (full index) | 6 | 0.500 | 0.500 | 0.500 |

## Keyword vs. embeddings

The `paraphrase` items share few or no words with their answer ("the Python chemistry library
refuses my radical" -> PySCF `spin` is 2S). They measure what dense embeddings add. To compare:

```bash
rag-drg eval --json > eval-keyword.json         # embeddings: provider: none

# enable embeddings in rag_drg.yaml (or a conf.d file), e.g.
#   embeddings: {provider: sentence-transformers, model: BAAI/bge-small-en-v1.5}
pip install -e '.[st]'
rag-drg ingest                                  # computes vectors for every chunk once
rag-drg eval --json > eval-embeddings.json      # header says "keyword + embeddings (<model>)"

rag-drg eval --tags paraphrase                  # the number that should move most
```

**Nightly on GitHub.** `.github/workflows/nightly-eval.yml` runs every night (and on demand from the
Actions tab):
* It fetches every enabled remote source (ARC, Psi4, PySCF, DRGScripts) and runs the whole
  `eval/qa.yaml` twice: keyword-only, and with `sentence-transformers` (`BAAI/bge-small-en-v1.5`, CPU).
* Each run's summary shows hit@1/hit@6/MRR and every miss. The JSON is kept as an artifact.
* The keyword run fails below hit@6 0.95. The semantic run only reports, until embeddings are
  enabled for real.
* The PR workflow stays on curated + lessons, so upstream repos can't break unrelated PRs.

Compare overall hit@1/MRR and the `paraphrase` row. Embeddings are worth enabling if
`paraphrase` improves clearly **and** the keyword-style questions (exact tokens like
`%maxcore`, `GEOM_MAXITER`) do not get worse. Keep the JSON files: they contain the per-item
ranks, so you can diff which questions changed.

## The query log

Enabled by `conf.d/querylog.yaml` on whatever machine runs `rag-drg serve` (for the shared
HTTP server, that is the only place). Each MCP call to `search_knowledge`,
`lookup_level_of_theory` or `record_lesson` becomes one JSON line in `index/query_log.jsonl`:
timestamp (UTC), tool, arguments, user (when token auth is on, otherwise null), number of
results, and the top 5 results (source, path, title, doc type, score). The file rotates at
`max_mb` (default 20 MB, one old file kept).

**Privacy.** Queries can contain project details (molecules, unpublished results, user and
cluster names). The log stays on the server under the git-ignored `index/`; never commit it or
paste it raw into issues. Only curated questions (read and cleaned by a person) go into
`eval/qa.yaml`. Delete `index/query_log.jsonl*` whenever you like, or set `enabled: false`
(on the server, in the git-ignored `conf.d/zz-local.yaml` as `query_log: {enabled: false}`, not as
a local commit, which would break the nightly `git pull --ff-only`).

```bash
rag-drg queries report                  # last 7 days
rag-drg queries report --since 30d --json
rag-drg queries to-qa --since 30d --empty-only > /tmp/candidates.yaml
```

The report lists:

* totals per tool, per day, per user and per `software` filter (which codes agents work with),
* **top queries** (normalised, with who asked),
* **EMPTY** queries: nothing in the index matched. Each is a card waiting to be written (or a
  source to add),
* **low-confidence** queries: the top result's score is below 0.02 or it is not from a curated
  source (`curated`, `lessons`). Scores are reciprocal-rank-fusion sums; a top hit supported by a
  single loose keyword match on a raw manual/code chunk scores 0.015-0.02, while answers backed by
  a card, an exact identifier match or a second ranked list score above 0.02 (on this index,
  42 of the 51 eval questions score >= 0.02, and 7 of 8 off-topic/unanswerable probes scored
  0.0156-0.0197). The reason is
  printed with each query. Tune with `query_log.low_score` / `--low-score`, and re-check after
  enabling embeddings,
* **most returned documents** (the cards worth reviewing first, since everyone reads them),
* **`record_lesson` events**: lessons agents wrote, i.e. PRs to review.

### From the log to cards and questions

Once a week (or before a group meeting):

1. `rag-drg queries report --since 7d`. For each EMPTY or low-confidence query that matters:
   find the answer (manual, a person), write or extend a card in `knowledge/` (`status: draft`),
   or record a lesson.
2. `rag-drg queries to-qa --since 7d > candidates.yaml`. It prints one entry per distinct
   query not already in `eval/qa.yaml`, empty ones first, with `expect: [TODO]` and a comment
   saying how often it was asked and what came back. Keep the useful ones, remove anything
   private, replace `TODO` with the answering document (`"curated:ess/orca/orca-essentials.md"`),
   add topic tags, and paste them into `eval/qa.yaml`.
3. `rag-drg ingest && rag-drg eval --show-failures`, then open the PR with the card and the questions.

## Adding questions by hand

```yaml
- id: orca-maxcore                         # unique, short
  q: "Is ORCA %maxcore total memory or per core?"   # as a person or agent asked it
  filters: {software: orca}                # only if the asker would pass it
  expect: ["curated:ess/orca/orca-essentials.md"]   # "source:path prefix", any of them counts
  tags: [orca, memory]
```

* `expect` entries: `"source:path/prefix"` (e.g. `"arc:examples/"` for any ARC example), or a
  mapping `{source: psi4, path: psi4/src/read_options.cc, title_contains: "OPTKING"}` when only
  one section of a large file answers it. `title_contains: "..."` at the item level is a
  shortcut for one more expectation.
* List every document that really answers the question (a card *and* the manual section); open
  each one and check the answer is actually there before adding it.
* `requires:` defaults to the sources named in `expect`. Set it (e.g. `requires: [curated]`)
  when a curated alternative is enough for the item to be meaningful on a curated-only index.
* Tag with the software and topic, and `paraphrase` when the question avoids the answer's words.
* `rag-drg lint` validates the file (ids unique, expectations well-formed).

## What we need from the group

The current 51 questions were written from the cards and repos; they are realistic but not
**yours**. To make the numbers mean something, please provide:

1. **30-50 real questions** that you or your agents asked in the last months (from chat history,
   Slack, agent transcripts), covering the codes and clusters you use: exactly as asked, not
   cleaned up. For each one, **the document that answers it** (card, manual section, ARC file,
   or "nothing yet" - those are the most useful: they become cards).
2. For ~10 of them, **the wrong answer the agent gave** (these are also lessons).
3. Confirmation that running the query log on the shared server is acceptable, and who may read
   the reports (default: the maintainers only; see Privacy above).
4. After a month of logging: an hour with the maintainer to go through `queries report` and
   `queries to-qa` and turn the gaps into cards and questions.

## CI

CI builds an index of the curated cards and lessons only and runs
`rag-drg eval --min-hit-rate 0.9 --show-failures`. Items that need ARC/Psi4/PySCF are skipped
automatically (listed in the log). We chose this over fetching the git sources in CI because
those sources are unpinned: a new upstream commit could change the rankings and fail our CI for
reasons unrelated to the PR, and it adds a network dependency. The full evaluation runs where
the full index lives (the shared server's nightly refresh, or locally before changing ranking
code).
