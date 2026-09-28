# Using the shared server without installing rag-drg

Most group members only need **one file**: `integrations/rag-drg-remote` (standard-library
Python, no dependencies). It talks to the shared rag-drg server on the main PC over the REST
API with your personal token.

## Where things run

| Runs on | What | Why |
|---|---|---|
| **Main PC** (one install, `rag-drg serve --transport http`) | knowledge index and search, levels table, lessons, input checking, failure diagnosis, basis checks | Same for everyone; needs no personal files or credentials: clients send file *contents* |
| **Your machine / the cluster** (`rag-drg-remote`, a single file) | reading your files, the Claude Code hook, sending your Unix user name and groups | Your files and identity live here |
| **Per-user rag-drg install** (optional, e.g. a shared venv in a group directory on zeus) | live cluster queries (`qstat -u $USER`, quota, queue access) | Must run *as you*, with your SSH keys and PBS permissions; the shared server is a service account |

ARC is never used by the server: it indexes ARC's source from GitHub. Your own ARC checkout and
`~/.arc/settings.py` stay yours.

## Setup (once per person)

1. Ask the server admin for a token (`rag-drg tokens add <you>` on the main PC).
2. Install the client:
   ```bash
   mkdir -p ~/bin ~/.config/rag-drg
   cp rag_drg/integrations/rag-drg-remote ~/bin/ && chmod +x ~/bin/rag-drg-remote
   echo '<your token>' > ~/.config/rag-drg/token && chmod 600 ~/.config/rag-drg/token
   echo 'export RAG_DRG_URL=http://<main-pc>:8765' >> ~/.bashrc
   ```
3. Check: `rag-drg-remote ping`.
4. Claude Code: add the MCP server (search, lessons and the other tools):
   ```bash
   claude mcp add --scope user --transport http rag-drg http://<main-pc>:8765/mcp \
       --header "Authorization: Bearer $(cat ~/.config/rag-drg/token)"
   ```
   and the automatic input check hook from `integrations/claude-code/hooks.remote.json` in
   `~/.claude/settings.json` (use the absolute path of `rag-drg-remote`).

## Commands

```bash
rag-drg-remote check job.gjf [--submit run.sh]   # finds a submit script next to the input that names it
rag-drg-remote diagnose job.log                  # sends only the head and tail of big outputs
rag-drg-remote search "ORCA NEB-TS" --software orca --version 6 --max-tokens 800
rag-drg-remote level "wb97xd/def2tzvp" --software orca
```

Exit codes: `check` 1 on errors; `diagnose` 0 success, 1 failed, 2 incomplete/unknown; 3 when the
server can't be reached or rejects the token. The hook never blocks the agent on network problems:
it prints "input check skipped" and lets the write through.

## What is sent to the server

The file you check (and its submit script), or the head/tail of an output you diagnose, your Unix
user name and group names (used only for queue-access rules), and your token. Requests are
recorded in the server's query log (see `conf.d/querylog.yaml`).
