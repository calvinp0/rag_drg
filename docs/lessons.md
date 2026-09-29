# Lessons: from a correction to a reviewed card

Agents call `record_lesson` when someone corrects them. This page covers what happens after
that: duplicate hints, the pull request the group reviews, the report of what is still open,
and a monthly clean-up that folds verified lessons into the cards.

Code: `rag_drg/lessons.py` (writing, duplicate detection) and
`rag_drg/tools/lessons_workflow.py` (PRs, report, lint). Config: `conf.d/lessons.yaml`.

## Lifecycle

```
 record_lesson / rag-drg lesson
        │  1. look for near-duplicates (same software: lessons + curated cards)
        │  2. write knowledge/lessons/<domain>/[<software>/]<date>-<slug>.md
        │     status: unreviewed, similar: [paths]  -> indexed at once, searchable
        │  3. lessons.pr.mode != none: commit it on lessons/<domain>-[<software>-]<date>-<slug> (or lessons/<date>)
        │     from <remote>/<base>, push, and (github mode) open or update the PR
        ▼
 pull request  ── reviewer checks it against the manual / a test calculation
        │
        ├── set status: verified          (the lesson stays; ranked above everything else)
        └── fold into the matching card   (edit knowledge/ess/<software>/..., delete the lesson)
        ▼
 merged into <base>  ->  shared server: `rag-drg lessons tidy && git pull && rag-drg ingest`
```

* The lesson file is always written and indexed first. A git or GitHub failure never fails
  `record_lesson`: the tool output says what went wrong and the file stays in place. Retry
  later with `rag-drg lessons pr --all-unreviewed`.
* Git work never touches the serving checkout's working tree, index or current branch. The
  lesson is committed with git plumbing (a throw-away index file and `git commit-tree`) on top
  of `<remote>/<base>` (or on top of the lesson branch if it already exists) and pushed
  straight to `refs/heads/lessons/...` on the remote. No local branch is created; only the
  remote-tracking refs are updated by `git fetch`.
* The PR/branch each lesson went to is recorded in `index/lessons_prs.json` (git-ignored).
  A reviewer may also add `pr: https://github.com/<owner>/<repo>/pull/<n>` to a lesson's
  front matter; `rag-drg lint` checks the format.

### Duplicate hints

Before writing, the new lesson's title + mistake + correction are searched (normal hybrid
search) against the existing lessons and curated cards of the same software (or the same
domain if no software is given). Each candidate chunk is scored as

    score = 0.4 * overlap + 0.3 * jaccard + 0.3 * identifiers

where *overlap* and *jaccard* compare the content words (stop words removed) and
*identifiers* is the share of the new lesson's exact keywords (`%maxcore`, `def2-TZVP`,
`ts_guess_level`, ...) that also appear in the chunk. Matches at or above
`lessons.similar.threshold` (0.35) are returned by the tool as
`possibly duplicates: <paths>` and stored in the lesson as `similar: [paths]`, so the
reviewer sees them in the PR. In the test set, duplicates score 0.57-0.89 and unrelated
lessons on the same program score at most 0.21. `rag-drg lint` fails when a `similar` path
no longer exists (for example after the lesson it pointed to was folded into a card):
remove the entry or point it at the card.

## Configuration (`conf.d/lessons.yaml`)

```yaml
lessons:
  pr:
    mode: none              # none | branch | github
    remote: origin          # git remote of the serving checkout
    base: main              # lesson branches start from here and PRs target it
    repo: owner/name        # github mode
    token_env: GITHUB_TOKEN # github mode: name of the env var holding the token
    batch: per-lesson       # per-lesson | daily
    labels: []              # optional, e.g. [lesson] (the label must exist in the repo)
    draft: false
    # api_url: https://api.github.com   # GitHub Enterprise: https://<host>/api/v3
  similar:
    threshold: 0.35
    max: 5
    sources: [curated]
```

| mode | what happens after a lesson is recorded |
|---|---|
| `none` | nothing; the tool asks the user to commit the file and open a PR |
| `branch` | commit on `lessons/<domain>-[<software>-]<date>-<slug>` (e.g. `lessons/ess-orca-2026-03-01-maxcore`) and push it; open the PR yourself (any git host) |
| `github` | as `branch`, then open a PR via the GitHub REST API (`POST /repos/{owner}/{repo}/pulls`) titled `Lesson: <title>`, with the lesson, its author, the similar entries and a reviewer checklist |

`batch: daily` puts all lessons of one day on `lessons/<YYYY-MM-DD>` and in one PR
(`Lessons: <date>`): the first lesson opens it, later ones are committed on the same branch
and appended to the PR description.

The author shown in the lesson and the PR is the authenticated user when the server runs with
token auth (`ctx.current_user()`), else `$RAG_DRG_AUTHOR`, else `$USER`.

## GitHub token

Use a **fine-grained personal access token** (or a GitHub App installation token) limited to
the knowledge-base repository, with:

* **Contents: Read and write**: to push the `lessons/...` branches when the remote is https.
* **Pull requests: Read and write**: to find, open and update the PRs.
* (Metadata: Read is added automatically.) Labels (optional) are set through the issues
  endpoint (`POST /repos/{owner}/{repo}/issues/{n}/labels`); if GitHub refuses that with the
  scopes above, add **Issues: Read and write**. A label failure never blocks the PR; the tool
  output notes it.

The token is read only from the environment variable named in `token_env`. It is sent as an
HTTP header to the GitHub API and, for https remotes, passed to `git push`/`git fetch`
through `GIT_CONFIG_*` environment variables, so it never appears in command lines, in the
config, in the tool output or in the logs (error messages are scrubbed). With an ssh remote,
`git push` uses the server's ssh key instead (a repository deploy key with write access) and
the token is only used for the API.

## Running it on the shared server

1. The server runs from a clone of this repository whose `origin` is the GitHub repo:
   `git clone https://github.com/<owner>/<repo>.git rag_drg` (or the ssh URL plus a deploy
   key with write access).
2. In that clone, set `mode: github`, `repo: <owner>/<repo>`, and `base:` to the branch the
   group merges into, in a per-machine override, `conf.d/zz-local.yaml` (or
   `conf.d/<name>.local.yaml`), which is git-ignored and merged after `conf.d/lessons.yaml`:

   ```yaml
   lessons:
     pr: {mode: github, repo: <owner>/<repo>, base: main}
   ```

   Nested keys are merged, so the rest of `lessons.pr` keeps its defaults. Do **not** edit and
   commit `conf.d/lessons.yaml` in the serving clone: a local commit makes the nightly
   `git pull --ff-only` fail.
3. Put the token in the service environment, not in the repo, e.g. a root-only
   `/etc/rag-drg/secrets.env` with `GITHUB_TOKEN=github_pat_...` and
   `EnvironmentFile=/etc/rag-drg/secrets.env` in `deploy/rag-drg.service`. Set
   `RAG_DRG_AUTHOR` there too if the server does not run with per-user token auth.
4. Keep the serving checkout on `base` and clean. Recorded lessons stay there as untracked
   files until their PR is merged. Because `git pull` refuses to overwrite untracked files,
   the nightly refresh must run **`rag-drg lessons tidy` before `git pull --ff-only`**: it
   fetches `<remote>/<base>` (with the same token as the push) and deletes the local copies of
   lessons that are now on it, or whose pushed commit (recorded in `index/lessons_prs.json`)
   was merged into it even though the reviewer folded the lesson into a card and deleted the
   file, so the pull brings in the reviewed version (`deploy/refresh.sh` does this; tidy and
   lint problems are reported there but do not stop the re-index):

   ```bash
   bin/rag-drg lessons tidy || true
   git pull --ff-only
   bin/rag-drg lint || true
   bin/rag-drg ingest --fetch
   ```

   Lessons whose PR was closed without merging stay as local files; delete them by hand
   (they are listed in `rag-drg lessons report`). The same goes for lessons folded into a card
   by a **squash or rebase merge**, which rewrites the commit: tidy cannot tell those were merged.
5. Test it once from the server: `rag-drg lesson --title "test" ...` followed by closing the
   PR, or run `rag-drg lessons pr PATH` on an existing lesson.

## Commands

```bash
rag-drg lessons similar knowledge/lessons/ess/orca/2026-09-28-x.md [--json]
rag-drg lessons pr PATH ...            # push/open PRs for these lessons
rag-drg lessons pr --all-unreviewed    # every unreviewed lesson without a PR yet  [--dry-run]
rag-drg lessons report [--json] [--stale-days 14]
rag-drg lessons tidy [--dry-run]       # before `git pull` on the serving checkout
```

`lessons report` lists: unreviewed lessons oldest first (`!` = older than 14 days) with their
PR, verified lessons grouped by software with the card to fold them into (the card they were
flagged similar to, else `knowledge/ess/<software>/`), lessons with a non-empty `similar`
list, and the number of lessons per author.

## Monthly consolidation routine

Verified lessons are meant to be temporary: once a month their content should move into the
cards. The prompt below is written for a Claude Code scheduled task (a routine) that runs in a
checkout of this repository with push access. Whether to schedule it is up to the group;
paste it as the task prompt, for example on the first Monday of the month.

```text
You maintain the group's rag-drg knowledge base (this repository). Monthly consolidation:

1. Setup: `python -m venv .venv && .venv/bin/pip install -q -e '.[mcp,pdf,dev]'`, then
   `git fetch origin && git switch -c consolidate/$(date +%Y-%m) origin/main`.
2. Run `bin/rag-drg lessons report --json` and read it.
3. For every lesson under "verified_by_software": read the lesson and the card named in
   "fold_into" (if it is a folder, pick the card whose topic matches, or create a new card
   from knowledge/README.md's format with `status: draft`). Fold the lesson in:
   - put the correction in the section it belongs to (one topic per `##` section), leading
     with what agents get wrong and then the exact correct syntax in a code block, with the
     version it applies to and the lesson's evidence/source;
   - do not add anything the lesson or the manual does not support; do not change a card's
     `status` (only humans set `verified`);
   - then delete the lesson file, and remove its path from any other lesson's `similar:` list.
4. For lessons under "with_similar" that are verified and clearly say the same thing as
   another lesson, merge them into one (keep the better-evidenced one) and delete the other.
   Leave unreviewed lessons alone, but list those marked stale in the PR description so a
   human can review them.
5. Run `bin/rag-drg lint` and `.venv/bin/python -m pytest -q`; fix any problem you caused.
6. Commit ("Consolidate verified lessons into cards (<month>)"), push the branch, and open a
   pull request against main whose description has: one bullet per folded lesson
   (lesson -> card/section), the merged duplicates, and the list of stale unreviewed lessons.
   If there were no verified lessons, do not open a PR; just report the stale list.
```
