#!/bin/bash
# Nightly refresh on the shared server: pull reviewed cards, re-fetch upstream docs, re-index.
# crontab -e:   17 3 * * *  /path/to/rag_drg/deploy/refresh.sh >> /path/to/rag_drg/index/refresh.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== $(date -Is) refresh"
# Secrets for fetchers (e.g. ZOTERO_API_KEY) live outside the repo; see docs/zotero.md.
if [ -f /etc/rag-drg/secrets.env ]; then set -a; . /etc/rag-drg/secrets.env; set +a; fi
# Drop local lesson copies already merged upstream, so the pull can't conflict (best effort).
.venv/bin/rag-drg lessons tidy || echo "lessons tidy failed (see above); continuing"
git pull --ff-only
# Lint problems are reported, but they must not stop the index from refreshing.
.venv/bin/rag-drg lint || echo "lint problems (see above); continuing"
.venv/bin/rag-drg ingest --fetch
# ARC input schema for `rag-drg arc check` / check_arc_input, from the freshly fetched ARC clone.
.venv/bin/rag-drg arc schema || echo "arc schema failed (checker falls back to the committed snapshot); continuing"
# Retrieval quality on the full index, kept as a log for trend-watching (never fails the refresh).
.venv/bin/rag-drg eval --json > index/eval-latest.json || true
