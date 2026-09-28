# Local documentation sources

Licensed manuals, saved web pages, books and exported cluster guides go here. Everything
except `_meta.yaml` files is git-ignored. Run `rag-drg ingest` afterwards; only new or
changed files are re-processed.

| Folder | Content |
|---|---|
| `orca/5/` | ORCA 5 manual (PDF) |
| `orca/6/` | ORCA 6 manual: **HTML pages** (saved or mirrored), and/or PDFs |
| `gaussian/09/`, `gaussian/16/` | Gaussian keyword pages (HTML) and/or PDFs, per version |
| `gaussian/book/` | Gaussian books (theory + worked examples), tagged for both 09 and 16 |
| `qchem/6.1/` | Q-Chem 6.1 manual (PDF or HTML) |
| `molpro/2024/`, `molpro/2026/` | Molpro manuals (PDF or HTML) |
| `hpc/<cluster>/` | Cluster user guides (HTML, PDF, Markdown) |

Any folder can mix PDFs, `.html` pages and Markdown. Sub-folders are fine.

## Manuals that are many HTML pages (ORCA 6, Gaussian keywords, Molpro)

Pick whichever is easiest; all three give one search entry per page section, titled with the
page title and headings, and linked back to the original URL.

**1. Let rag-drg download them.** Sources are already set up in `rag_drg.yaml`
(`orca6-manual-web`, `gaussian-keywords`, `qchem61-manual-web`, `molpro-manual-web`). They are
disabled by default. On a machine that can reach the site:

```bash
rag-drg fetch --source orca6-manual-web     # crawls only pages under the manual's URL
rag-drg ingest --source orca6-manual-web
```

Set `enabled: true` once it works, so the nightly refresh keeps it current. Check the start
URL first; it is the one thing to update when a new version comes out.

**2. Mirror the site with wget** into the version folder (the folder layout records each
page's URL, which rag-drg rebuilds for citations):

```bash
cd sources/orca/6
wget --recursive --level=inf --no-parent --adjust-extension --wait=1 \
     --reject-regex '/(_static|_images|_sources|_downloads)/|genindex|search\.html' \
     https://www.faccts.de/docs/orca/6.0/manual/      # use the current manual URL
```

**3. Save pages from the browser** ("Save page as", *Webpage, Complete* or *HTML only*) into
the folder. The `<page>_files/` asset folders the browser creates are ignored, and the
`saved from url=` note the browser writes lets rag-drg link each page back to its URL.

The indexer handles web-manual clutter for you:

* Only the page's main content is kept. Sidebars, the table of contents repeated on every page,
  headers, footers, "previous/next" links and permalink markers are dropped.
* Sphinx build artefacts (`_static/`, `_sources/`, `genindex.html`, `search.html`) are skipped.
* The same page saved twice (browser copy and wget copy, or two folders) is indexed once,
  preferring the copy whose URL is known.
* Each section is labelled `reference` (keywords, input, options, examples) or `theory`
  (method background), the same as for PDFs.

## PDFs

Keep one PDF per manual and version; no manual splitting is needed. The indexer follows the
PDF's bookmarks, giving entries like
`orca_manual_5_0_4 > Geometry Optimization > Transition States (pp. 312-314)`, each labelled
`reference` or `theory`. PDFs without bookmarks are indexed page by page.

## Books and other files that need their own metadata: `_meta.yaml`

A `_meta.yaml` in a folder applies to every file in it and in its sub-folders. A
`<file>.meta.yaml` next to one file applies to that file only. Keys: `title`, `version` (a
string or a list), `doc_type`, `tags`, `software`, `domain`. Example (`gaussian/book/_meta.yaml`):

```yaml
title: "Exploring Chemistry with Electronic Structure Methods (Foresman & Frisch)"
version: ["09", "16"]      # matches searches filtered to either version
tags: [book, tutorial]
```

For a PDF, the `title` replaces the file name in search results.

## Scanned or unreadable PDFs (OCR)

Older books are often scanned images, or have text that extracts as garbage. Check first:

```bash
rag-drg check-pdf sources/gaussian/book/
#   pages: 540, without text: 523, garbled: 0, bookmarks: 0
#   verdict: scanned - no usable text: run `ocrmypdf --skip-text in.pdf out.pdf` ...
```

Verdicts: `ok` (ingest as is), `partial` (some image-only pages), `scanned` (needs OCR),
`garbled` (text layer exists but is unreadable, e.g. broken font encoding). Ingest also warns
about PDFs that look like this.

Fix with [OCRmyPDF](https://ocrmypdf.readthedocs.io) on your own machine
(`apt install ocrmypdf`, `brew install ocrmypdf` or `conda install -c conda-forge ocrmypdf`):

```bash
ocrmypdf --skip-text --deskew --rotate-pages -l eng book.pdf book_ocr.pdf   # scanned / partial
ocrmypdf --force-ocr -l eng book.pdf book_ocr.pdf                           # garbled
```

Put only the OCR'd file in `sources/` (move the original elsewhere, or both get indexed).
OCR text is good for prose and route/keyword lines. Equations come out poorly, which is
acceptable for search but means agents should not quote formulas from OCR'd pages. OCRmyPDF
keeps the existing bookmarks; a book without bookmarks is indexed page by page.
