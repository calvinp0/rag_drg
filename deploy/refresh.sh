#!/bin/bash
# Nightly refresh on the shared server: pull reviewed cards, re-fetch upstream docs, re-index.
# crontab -e:   17 3 * * *  /path/to/rag_drg/deploy/refresh.sh >> /path/to/rag_drg/index/refresh.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
# bin/rag-drg finds the install (.venv, $RAG_DRG_BIN, conda env, PATH); for a conda install set
# RAG_DRG_BIN in the crontab line, e.g.  RAG_DRG_BIN=/opt/miniforge3/envs/rag-drg/bin/rag-drg
RAG=bin/rag-drg
echo "== $(date -Is) refresh"
# Secrets for fetchers (e.g. ZOTERO_API_KEY) live outside the repo; see docs/zotero.md.
if [ -f /etc/rag-drg/secrets.env ]; then set -a; . /etc/rag-drg/secrets.env; set +a; fi
# Drop local lesson copies already merged upstream, so the pull can't conflict (best effort).
"$RAG" lessons tidy || echo "lessons tidy failed (see above); continuing"
git pull --ff-only
# Lint problems are reported, but they must not stop the index from refreshing.
"$RAG" lint || echo "lint problems (see above); continuing"
"$RAG" ingest --fetch
# ARC input schema for `rag-drg arc check` / check_arc_input, from the freshly fetched ARC clone.
"$RAG" arc schema || echo "arc schema failed (checker falls back to the committed snapshot); continuing"
# Retrieval quality on the full index, kept as a log for trend-watching (never fails the refresh).
"$RAG" eval --json > index/eval-latest.json || true
