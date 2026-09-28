Extra config files merged into `rag_drg.yaml`: `sources` lists are appended, other keys are
merged recursively (nested mappings key by key; lists and scalars replace). One file per
feature (e.g. `zotero.yaml`).

Merge order: the shared files alphabetically, then the **per-machine overrides**
(`zz-*.yaml` and `*.local.yaml`), so an override always wins, whatever its name.
`zz-local.yaml` and `*.local.yaml` are git-ignored: put settings that only apply to one
machine (the shared server, your own stdio server) there, e.g.

```yaml
# conf.d/zz-local.yaml on the shared server
lessons:
  pr: {mode: github, repo: my-group/rag_drg}
```

Do not commit such changes in the serving clone: a local commit makes the nightly
`git pull --ff-only` in `deploy/refresh.sh` fail. Changes meant for everyone go through a PR.
