#!/bin/bash
# Nightly refresh on the shared server: pull reviewed cards, re-fetch upstream docs, re-index.
# crontab -e:   17 3 * * *  /path/to/rag_drg/deploy/refresh.sh >> /path/to/rag_drg/index/refresh.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== $(date -Is) refresh"
git pull --ff-only
.venv/bin/rag-drg lint
.venv/bin/rag-drg ingest --fetch
