#!/bin/bash
# Nightly refresh on the shared server: pull reviewed cards, re-fetch upstream docs, re-index.
# crontab -e:   17 3 * * *  /path/to/rag_drg/deploy/refresh.sh >> /path/to/rag_drg/index/refresh.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== $(date -Is) refresh"
# Secrets for fetchers (e.g. ZOTERO_API_KEY) live outside the repo; see docs/zotero.md.
if [ -f /etc/rag-drg/secrets.env ]; then set -a; . /etc/rag-drg/secrets.env; set +a; fi
.venv/bin/rag-drg lessons tidy   # drop local lesson copies already merged upstream, so the pull can't conflict
git pull --ff-only
.venv/bin/rag-drg lint
.venv/bin/rag-drg ingest --fetch
# Retrieval quality on the full index, kept as a log for trend-watching (never fails the refresh).
.venv/bin/rag-drg eval --json > index/eval-latest.json || true
