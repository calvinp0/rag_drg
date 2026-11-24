# The private site repository (`site/`)

This repository is public. What describes one group's clusters is not: login hosts, node names,
queue access lists, install paths. That lives in a second, private repository, cloned into
`site/` at the root of this one (`site/` is git-ignored here).

```bash
git clone git@github.com:<your-org>/<your-site-repo>.git site
rag-drg ingest --source site        # index its cards
```

Without `site/` everything works; there are just no clusters (`list_servers()` is empty, submit
scripts are not rendered) unless you put a `servers.yaml` at the repository root.

## What goes in it

| Path in `site/` | What it is | How it is picked up |
|---|---|---|
| `servers.yaml` | the cluster registry ([servers.md](servers.md)) | used instead of `<repo>/servers.yaml` when it exists |
| `knowledge/` | cluster cards, site templates, lessons about a cluster | indexed as the source `site`, ranked and linted like `curated` |
| `knowledge/hpc/servers/generated/` | cards written by `rag-drg servers render-cards` | written here when the registry is `site/servers.yaml` |
| `conf.d/*.yaml` | site configuration | merged like the repository's `conf.d/` ([conf.d/README.md](../conf.d/README.md)) |
| `eval/tasks.yaml`, `eval/tasks/` | agent tasks that need the clusters | `rag-drg agent-eval --tasks site/eval/tasks.yaml ...` |

## Rules

* Never copy a host name, IP address, node list or queue access list from `site/` into this
  repository: not into cards, lessons, tests, fixtures, eval tasks or docs. Tests use
  `servers.example.yaml` and `*.example.org` hosts.
* A lesson that is about one cluster belongs in `site/knowledge/lessons/`. `record_lesson` writes
  to `knowledge/lessons/` in this repository, so move such a lesson before it is committed.
* Commit and push `site/` on its own; nothing in this repository tracks its revision.
