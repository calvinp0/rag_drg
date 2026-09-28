# Zotero library → knowledge base

The group's Zotero library can be synced into rag-drg so that agents find papers, abstracts
and **our own reading notes** with `search_knowledge(..., domain="literature")`. The sync is
incremental: after the first run only items that changed since the last library version
are downloaded, and deleted or trashed items are removed from the index.

```
Zotero group library ──(Web API v3, read-only key)──▶ sources_cache/zotero-group/ ──ingest──▶ index
  papers + PDFs                                         items/<KEY>/<file>.pdf + .meta.yaml
  child notes                                           items/<KEY>/note_<KEY>.md
  collections = projects                                tags: vae-ess-nn, datasets, ...
```

## 1. Use a group library

Put the project literature in a **Zotero group** (zotero.org → Groups → Create a new group;
"Private membership" is fine) and have everyone add papers there instead of in their personal
libraries. Organise it with one **collection per project**, e.g. `VAE ESS NN` with
sub-collections `Datasets`, `Descriptors`, `ESS benchmarks`.

**Store the PDFs in Zotero**, not as linked files. Zotero can only give the server files it
stores itself (the default when you drag a PDF in or use the browser connector). Linked files
(`Add Attachment → Link to File`) live on one person's disk and cannot be downloaded; they are
listed by `rag-drg zotero status` so they can be converted (right-click → *Convert Linked Files
to Stored Files*). The group needs enough Zotero file storage for the PDFs (the free tier is
300 MB, shared by the group owner).

## 2. Find the group ID

Open the group on zotero.org (Groups → your group → *Group Library*). The URL is
`https://www.zotero.org/groups/<GROUP_ID>/<group_name>/library`; the number is the group ID.
If the URL shows only the name, open *Group Settings*: its URL contains the number. With an
API key (step 3) you can also list your groups:

```bash
curl -s -H "Zotero-API-Key: $ZOTERO_API_KEY" \
  "https://api.zotero.org/users/<YOUR_USER_ID>/groups" | python -m json.tool | grep -E '"(id|name)"'
```

Your userID is shown at <https://www.zotero.org/settings/keys> ("Your userID for use in API
calls is …").

## 3. Create a read-only API key

One member (ideally a group admin) creates a key at
<https://www.zotero.org/settings/keys/new>:

* **Name**: `rag-drg server`.
* **Personal Library**: leave *Allow library access* **off** (the server does not need your
  personal library). Leave *Allow write access* off.
* **Default Group Permissions**: *None*; then under **Specific Groups** set our group to
  **Read Only**. (Read access to a group includes its notes.)
* Save and copy the key. It is shown once.

The key only allows reading that one group. If it leaks, delete it on the same page and create
a new one.

## 4. Put the key on the shared server

The key is read **only from an environment variable** (default `ZOTERO_API_KEY`); it is
never written to the config, the cache, the index or the logs. `rag-drg lint` fails if a
config contains `api_key:`.

```bash
sudo install -d -m 700 -o <service-user> /etc/rag-drg
echo 'ZOTERO_API_KEY=<the key>' | sudo tee /etc/rag-drg/secrets.env >/dev/null
sudo chown <service-user> /etc/rag-drg/secrets.env && sudo chmod 600 /etc/rag-drg/secrets.env
```

The nightly refresh (`deploy/refresh.sh`, run by cron) loads `/etc/rag-drg/secrets.env` itself:

```cron
17 3 * * *  /opt/rag_drg/deploy/refresh.sh >> /opt/rag_drg/index/refresh.log 2>&1
```

(The MCP server itself does not need the key: it only reads the index.)

## 5. Configure and run

Edit `conf.d/zotero.yaml`:

```yaml
sources:
  - name: zotero-group
    type: zotero
    enabled: true
    domain: literature
    doc_type: paper
    zotero:
      library_type: group
      library_id: "1234567"          # step 2, quoted
      api_key_env: ZOTERO_API_KEY    # the variable NAME
      collections: null              # or ["VAE ESS NN"] to sync only some projects
      collection_tags: true
      include_notes: true
```

```bash
rag-drg zotero sync --source zotero-group      # first run downloads everything
rag-drg ingest --source zotero-group           # or: rag-drg zotero sync --ingest
rag-drg zotero status                          # version, counts, linked files, failed downloads
rag-drg search "latent space property predictor" --domain literature
```

After `enabled: true`, `rag-drg ingest --fetch` (the nightly refresh) and `rag-drg fetch`
sync it too. Changing `collections` takes effect on the next sync. `rag-drg zotero sync
--full` re-reads all metadata from version 0 (if something looks out of date); unchanged PDFs
are not downloaded again.

| Option | Default | Meaning |
|---|---|---|
| `mode` | `web` | `web` (Zotero Web API) or `local` (see below) |
| `library_type` | `group` | `group` or `user` |
| `library_id` | — | group ID, or your userID for a personal library via the API |
| `api_key_env` | `ZOTERO_API_KEY` | env var with the key; `null` for a public library |
| `collections` | `null` | names or keys; sub-collections are included |
| `collection_tags` | `true` | add collection names (and parent names) as tags |
| `include_notes` | `true` | index child and standalone notes |
| `file_types` | `[application/pdf, text/html]` | attachment types to download |
| `max_file_mb` | `200` | skip larger files (reported as failed) |

## How collections map to projects

Every file gets the Zotero tags of its paper plus the names of the paper's collections and
their parent collections, lower-cased with spaces turned into dashes (`VAE ESS NN` →
`vae-ess-nn`, `Datasets` → `datasets`). Tags are searchable, so an agent working on the
VAE + ESS + NN pipeline can search `"vae-ess-nn property prediction"`, and the project card
in `knowledge/projects/vae-ess-nn/` can tell agents to do so. Use `collections:` to restrict
the sync to some projects, e.g. to keep a large personal-interest collection out.

## How notes are used

Zotero notes are where we write what a paper means *for us* (settings that worked, pitfalls,
which dataset split was used). Each note becomes its own Markdown document titled
`Note: <first line> (on: <paper title>)`, tagged `zotero-note` plus the paper's tags, with the
paper's authors, year and DOI link in its metadata. Standalone notes (not attached to a paper)
are indexed too, tagged with their collections.

Tips: start a note with a heading that says what it is ("Takeaways for the NN
featurisation"); keep one topic per note. Notes are not reviewed like `knowledge/` cards;
anything that becomes a group convention belongs in a project card via PR.

Papers without a stored file still get a small document with title, authors, venue, DOI and
abstract, so they are findable. PDF annotations (highlights) are not indexed yet.

## What is written where

```
sources_cache/zotero-group/          git-ignored; delete it to start over
  _zotero_state.json                 library version, item metadata, last report (no key)
  items/<ITEM>/<ATT>_<file>.pdf      stored PDF / HTML snapshot
  items/<ITEM>/<ATT>_<file>.pdf.meta.yaml   title, authors, year, doi, url, tags, collections
  items/<ITEM>/note_<NOTE>.md        child notes
  items/<ITEM>/abstract.md           only for papers without a stored file
  notes/<NOTE>.md                    standalone notes
```

Search results link to `https://doi.org/<DOI>` when the paper has a DOI, otherwise to its URL
or its page in the Zotero web library.

## Personal libraries without API access (local mode)

To index your own library on your machine, read Zotero's data directory directly (Zotero →
Settings → Advanced → Files and Folders → *Data Directory Location*):

```yaml
  - name: zotero-mine
    type: zotero
    zotero: {mode: local, library_type: user, data_dir: ~/Zotero, linked_base_dir: ~/Papers}
```

`zotero.sqlite` is copied to a temporary file before reading, so Zotero can stay open. Files
come from `storage/<KEY>/`; linked files are copied too when their path exists on this machine
(`linked_base_dir` is Zotero's *Linked Attachment Base Directory*, if you use one). A group
library synced into your Zotero desktop can be read the same way with `library_type: group`
and its `library_id`.

## Privacy and copyright

* PDFs and snapshots stay in the git-ignored `sources_cache/`; they are never committed.
  The index stores extracted text chunks, so it is as private as the PDFs: keep the index and
  the MCP server on group machines only (the HTTP server should not be reachable from outside).
* Notes are indexed as written; don't put anything in a group-library note you would not
  show to the whole group.
* The API key is read-only, limited to the group, and only ever read from the environment.
