"""Turn files into retrievable chunks.

Every chunker returns ``Chunk`` objects whose ``title`` is a heading
breadcrumb ("ORCA > Geometry optimization > TS searches"), which gives the
keyword index and the agent reading results useful context. Structured
formats are split on their own boundaries first (Markdown/RST headings,
Python functions/classes, PDF pages, HTML headings) and only long sections
are split further by paragraph with overlap.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import yaml


@dataclass
class Chunk:
    text: str
    title: str
    path: str  # path relative to the source root (or URL)
    source: str = ""
    domain: str | None = None
    software: str | None = None
    version: str | None = None
    doc_type: str = "reference"
    tags: list[str] = field(default_factory=list)
    url: str | None = None
    status: str | None = None  # e.g. "verified", "unreviewed" for curated cards / lessons
    ordinal: int = 0  # position of the chunk within its file
    kind: str | None = None  # chunker's own classification, e.g. "theory" for manual background sections

    @property
    def key(self) -> str:
        return f"{self.source}:{self.path}#{self.ordinal}"

    @property
    def content_hash(self) -> str:
        h = hashlib.sha1()
        for part in (
            self.text, self.title, self.domain, self.software, self.version,
            self.doc_type, ",".join(self.tags), self.url, self.status,
        ):
            h.update((part or "").encode())
            h.update(b"\0")
        return h.hexdigest()


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def split_front_matter(text: str) -> tuple[dict, str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, text[m.end():]


# --------------------------------------------------------------------------- #
# Generic size-based splitting
# --------------------------------------------------------------------------- #

def split_long(text: str, size: int, overlap: int) -> list[str]:
    """Split text into pieces of at most ~size chars, preferring paragraph breaks."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    paragraphs = re.split(r"\n\s*\n", text)
    pieces: list[str] = []
    buf = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            # Hard-split an oversized paragraph, preferring line then word boundaries.
            if buf:
                pieces.append(buf)
                buf = ""
            start = 0
            while start < len(para):
                end = min(len(para), start + size)
                if end < len(para):
                    cut = para.rfind("\n", start + size // 2, end)
                    if cut == -1:
                        cut = para.rfind(" ", start + size // 2, end)
                    if cut != -1:
                        end = cut
                pieces.append(para[start:end])
                if end >= len(para):
                    break
                start = max(end - overlap, start + 1)
            continue
        if len(buf) + len(para) + 2 > size and buf:
            pieces.append(buf)
            tail = buf[-overlap:] if overlap else ""
            # Start the overlap on a word boundary.
            if tail and " " in tail:
                tail = tail[tail.index(" ") + 1:]
            buf = f"{tail}\n\n{para}" if tail else para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        pieces.append(buf)
    return [p.strip() for p in pieces if p.strip()]


def _sections_to_chunks(
    sections: Iterable[tuple[list[str], str]],
    path: str,
    size: int,
    overlap: int,
    doc_title: str | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for crumbs, body in sections:
        crumbs = [c for c in crumbs if c]
        if doc_title and (not crumbs or crumbs[0] != doc_title):
            crumbs = [doc_title] + crumbs
        title = " > ".join(crumbs) if crumbs else Path(path).name
        for piece in split_long(body, size, overlap):
            chunks.append(Chunk(text=piece, title=title, path=path, ordinal=len(chunks)))
    return chunks


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def markdown_sections(text: str) -> list[tuple[list[str], str]]:
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append(([h for _, h in stack], body))
        buf.clear()

    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else _MD_HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            buf.append(line)
    flush()
    return sections


# --------------------------------------------------------------------------- #
# reStructuredText (Sphinx docs: Psi4, PySCF, ARC)
# --------------------------------------------------------------------------- #

_RST_ADORN = re.compile(r"^([=\-~^\"'`#*+_.:])\1{2,}\s*$")


def rst_sections(text: str) -> list[tuple[list[str], str]]:
    lines = text.splitlines()
    sections: list[tuple[list[str], str]] = []
    levels: list[str] = []  # adornment styles in order of first appearance
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    i = 0

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append(([h for _, h in stack], body))
        buf.clear()

    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        # Overline + title + underline
        if (
            _RST_ADORN.match(line)
            and i + 2 < len(lines)
            and _RST_ADORN.match(lines[i + 2])
            and lines[i + 1].strip()
            and not _RST_ADORN.match(lines[i + 1])
        ):
            style, title, skip = "o" + line.strip()[0], lines[i + 1].strip(), 3
        elif (
            line.strip()
            and not _RST_ADORN.match(line)
            and _RST_ADORN.match(nxt)
            and len(nxt.strip()) >= max(3, len(line.strip()) - 2)
        ):
            style, title, skip = "u" + nxt.strip()[0], line.strip(), 2
        else:
            buf.append(line)
            i += 1
            continue
        flush()
        if style not in levels:
            levels.append(style)
        level = levels.index(style)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        i += skip
    flush()
    return sections


# --------------------------------------------------------------------------- #
# Python source (ARC, PySCF examples): module docstring + one chunk per def/class
# --------------------------------------------------------------------------- #

def python_sections(text: str, module: str) -> list[tuple[list[str], str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [([module], text)]
    lines = text.splitlines()
    sections: list[tuple[list[str], str]] = []
    doc = ast.get_docstring(tree)
    # Module-level code that is not a def/class (constants, settings dicts, schemas).
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue  # docstring
        seg = "\n".join(lines[node.lineno - 1: node.end_lineno])
        top_level.append(seg)
    head = "\n\n".join(p for p in [doc or "", "\n".join(top_level)] if p.strip())
    if head.strip():
        sections.append(([module], head))

    def visit(node, crumbs):
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name
                if name.startswith("_") and not name.startswith("__init__"):
                    continue
                start = (child.decorator_list[0].lineno if child.decorator_list else child.lineno) - 1
                if isinstance(child, ast.ClassDef):
                    # Class header + docstring only; methods get their own chunks.
                    body_start = child.body[0].lineno - 1 if child.body else child.end_lineno
                    cdoc = ast.get_docstring(child) or ""
                    header = "\n".join(lines[start:body_start])
                    sections.append((crumbs + [f"class {name}"], f"{header}\n{cdoc}".strip()))
                    visit(child, crumbs + [f"class {name}"])
                else:
                    src = "\n".join(lines[start: child.end_lineno])
                    sections.append((crumbs + [f"def {name}"], src))

    visit(tree, [module])
    return sections


# --------------------------------------------------------------------------- #
# JSON (e.g. ARC's output.yml JSON Schema): one section per property / definition
# --------------------------------------------------------------------------- #

def json_sections(text: str, name: str) -> list[tuple[list[str], str]]:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return [([name], text)]
    if not isinstance(data, dict):
        return [([name], text)]
    dump = lambda v: json.dumps(v, indent=1, ensure_ascii=False)  # noqa: E731
    sections: list[tuple[list[str], str]] = []
    is_schema = "properties" in data or "$defs" in data or "definitions" in data
    if is_schema:
        head = {k: v for k, v in data.items() if k not in ("properties", "$defs", "definitions")}
        sections.append(([name, "schema header"], dump(head)))
        for key, sub in (data.get("properties") or {}).items():
            req = " (required)" if key in (data.get("required") or []) else ""
            sections.append(([name, f"property {key}{req}"], f"{key}{req}:\n{dump(sub)}"))
        for defs_key in ("$defs", "definitions"):
            for key, sub in (data.get(defs_key) or {}).items():
                sections.append(([name, f"{defs_key}/{key}"], f"{defs_key}/{key}:\n{dump(sub)}"))
    else:
        for key, sub in data.items():
            sections.append(([name, str(key)], f"{key}:\n{dump(sub)}"))
    return sections


# --------------------------------------------------------------------------- #
# Psi4 psi4/src/read_options.cc: the authoritative list of every module's keywords
# --------------------------------------------------------------------------- #

_PSI4_MODULE = re.compile(r'if\s*\(\s*name\s*==\s*"(\w+)"')
_PSI4_OPTION = re.compile(
    r"/\*-((?:(?!\*/).)*?)-\*/\s*options\.add(?:_(\w+))?\(\s*\"(\w+)\"\s*(?:,\s*(.*?))?\);",
    re.DOTALL,
)


_PSI4_SUBSECTION = re.compile(r"/\*-\s*SUBSECTION\s+(.*?)\s*-\*/", re.DOTALL)


def psi4_options_sections(text: str) -> list[tuple[list[str], str]]:
    modules = [(m.start(), m.group(1)) for m in _PSI4_MODULE.finditer(text)]
    subsections = [(m.start(), re.sub(r"\s+", " ", m.group(1))) for m in _PSI4_SUBSECTION.finditer(text)]

    def module_at(pos: int) -> tuple[str, str | None]:
        name, mod_start = "GLOBALS", -1
        for start, mod in modules:
            if start > pos:
                break
            name, mod_start = mod, start
        sub = None
        for start, s in subsections:
            if start > pos:
                break
            if start > mod_start:
                sub = s
        return name, sub

    per_module: dict[tuple[str, str | None], list[str]] = {}
    for m in _PSI4_OPTION.finditer(text):
        desc, kind, key, rest = m.group(1), m.group(2) or "array", m.group(3), (m.group(4) or "").strip()
        expert = "!expert" in desc
        desc = re.sub(r"\s+", " ", desc.replace("!expert", "")).strip()
        args = [a.strip() for a in re.findall(r'"[^"]*"|[^,]+', rest)]
        default = args[0] if args else ""
        allowed = args[1].strip('"') if len(args) > 1 and args[1].startswith('"') else ""
        line = f"{key} [{kind}] default={default}"
        if allowed:
            line += f" allowed: {allowed}"
        if expert:
            line += " (expert)"
        line += f" -- {desc}"
        per_module.setdefault(module_at(m.start()), []).append(line)
    return [
        (
            ["Psi4 options", mod] + ([sub] if sub else []),
            f"Psi4 module {mod} keywords{' - ' + sub if sub else ''} "
            f"(set with `set {mod.lower()} {{ ... }}` or globally):\n\n" + "\n\n".join(lines),
        )
        for (mod, sub), lines in per_module.items()
    ]


# --------------------------------------------------------------------------- #
# HTML (Gaussian keyword pages, Molpro manual pages)
# --------------------------------------------------------------------------- #

class _HTMLToText(HTMLParser):
    """HTML -> Markdown-ish text, keeping only the page's main content.

    Saved or mirrored manual pages (ORCA's Sphinx manual, gaussian.com keyword pages) carry
    navigation sidebars, headers, footers and a table of contents on every page; indexing
    those would repeat the same menu text thousands of times. When the page has a <main>,
    <article> or role="main" element, only its text is kept; elements whose class/id look
    like navigation are dropped everywhere.
    """

    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form", "button"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "area", "base", "col", "embed", "param", "track"}
    BLOCK = {"p", "div", "br", "li", "tr", "table", "section", "article", "dd", "dt", "pre", "ul", "ol"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
    NAV_ATTR = re.compile(
        r"(^|[\s_-])(sidebar|sphinxsidebar|toctree-wrapper|bd-sidebar|wy-nav|navbar|nav|menu|breadcrumbs?|"
        r"footer|header|related|prev-next|search|skip-link|headerlink|toc|localtoc|cookie|banner)($|[\s_-])",
        re.IGNORECASE,
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all: list[str] = []
        self.main: list[str] = []
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, skip, main)
        self.pre = 0
        self.title = ""
        self._in_title = False
        self.canonical: str | None = None
        self.saw_main = False

    @property
    def skipping(self) -> bool:
        return any(sk for _, sk, _ in self.stack)

    @property
    def in_main(self) -> bool:
        return any(m for _, _, m in self.stack)

    def emit(self, text: str):
        if self.skipping:
            return
        self.all.append(text)
        if self.in_main:
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and (a.get("rel") or "").lower() == "canonical" and a.get("href"):
            self.canonical = a["href"]
        if tag == "meta" and a.get("property") == "og:url" and a.get("content") and not self.canonical:
            self.canonical = a["content"]
        if tag == "title":
            self._in_title = True
        if tag not in self.VOID:
            ident = f"{a.get('class') or ''} {a.get('id') or ''}"
            skip = tag in self.SKIP_TAGS or (
                tag in ("div", "aside", "section", "ul") and bool(self.NAV_ATTR.search(ident))
            ) or a.get("role") in ("navigation", "search", "banner", "contentinfo") or tag == "aside"
            main = tag in ("main", "article") or a.get("role") == "main"
            if main:
                self.saw_main = True
            self.stack.append((tag, skip, main))
        if tag in self.HEADINGS:
            self.emit("\n\n" + "#" * self.HEADINGS[tag] + " ")
        elif tag == "pre":
            self.pre += 1
            self.emit("\n```\n")
        elif tag in ("td", "th"):
            self.emit(" | ")
        elif tag in self.BLOCK:
            self.emit("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.HEADINGS:
            self.emit("\n")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.emit("\n```\n")
        elif tag in self.BLOCK:
            self.emit("\n")
        if tag in self.VOID:
            return
        # Pop to the matching open tag (tolerates unclosed <p>, <li>, ...).
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        self.emit(data if self.pre else re.sub(r"\s+", " ", data))

    def handle_comment(self, data):
        # Browsers' "Save page as" records the source: <!-- saved from url=(0042)https://... -->
        m = re.search(r"saved from url=\(\d+\)(\S+)", data)
        if m and not self.canonical:
            self.canonical = m.group(1)


_TITLE_SUFFIX = re.compile(r"\s+[—–|·]\s+.*$|\s+-\s+(ORCA|Gaussian|Q-Chem|Molpro|Psi4|PySCF)\b.*$", re.IGNORECASE)


def html_to_markdown(html: str) -> tuple[str, str]:
    title, text, _ = parse_html(html)
    return title, text


def parse_html(html: str) -> tuple[str, str, str | None]:
    """Return (page title without site suffix, main text as Markdown-ish text, source URL)."""
    p = _HTMLToText()
    p.feed(html)
    main = "".join(p.main)
    text = main if p.saw_main and len(main.strip()) > 100 else "".join(p.all)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\u00b6", "")  # Sphinx permalink pilcrows
    title = _TITLE_SUFFIX.sub("", re.sub(r"\s+", " ", p.title).strip())
    return title, text.strip(), p.canonical


# --------------------------------------------------------------------------- #
# PDF (ORCA manual, Molpro manual, papers)
# --------------------------------------------------------------------------- #

def _pdf_reader(path: Path):
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - depends on optional dep
        raise RuntimeError("PDF ingestion needs `pip install rag-drg[pdf]` (pypdf)") from e
    return PdfReader(str(path))


_TOC_LINE = re.compile(r"(\.\s*){4,}\s*\d+\s*$")


def _page_texts(reader) -> list[str]:
    texts = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - malformed pages should not abort ingestion
            txt = ""
        txt = re.sub(r"-\n(\w)", r"\1", txt)  # re-join hyphenated line breaks
        # Drop table-of-contents style lines ("3.2 Geometry optimization ........ 45").
        txt = "\n".join(l for l in txt.splitlines() if not _TOC_LINE.search(l))
        texts.append(txt)
    return texts


def pdf_pages(path: Path) -> list[tuple[int, str]]:
    return [(i, t) for i, t in enumerate(_page_texts(_pdf_reader(path)), start=1) if t.strip()]


def pdf_outline(reader) -> list[tuple[int, int, str]]:
    """Flatten PDF bookmarks into (page_index, depth, title), in document order."""
    out: list[tuple[int, int, str]] = []

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:  # noqa: BLE001 - broken destinations are common
                continue
            title = re.sub(r"\s+", " ", str(getattr(it, "title", "") or "")).strip()
            if page is not None and page >= 0 and title:
                out.append((page, depth, title))

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001
        return []
    # Stable sort by page keeps sibling order for several headings on one page.
    return sorted(out, key=lambda e: e[0])


def _find_title(text: str, title: str, start: int) -> int:
    # Headings in extracted text often carry a section number and odd spacing.
    words = [re.escape(w) for w in re.findall(r"\w+", re.sub(r"^[\d.\s]+", "", title))]
    if not words:
        return -1
    m = re.compile(r"\W+".join(words), re.IGNORECASE).search(text, start)
    if not m:
        return -1
    pos = m.start()
    # Include a preceding section number ("2.1 ") on the same line.
    while pos > start and text[pos - 1] in "0123456789. \t":
        pos -= 1
    return pos


_SKIP_SECTIONS = re.compile(r"^(index|bibliography|references|contents|table of contents)$", re.IGNORECASE)


def pdf_sections(path: Path, doc_title: str | None = None) -> list[tuple[list[str], str, int, int]]:
    """Split a PDF along its bookmarks: (breadcrumbs, text, first_page, last_page) per section.

    Falls back to one section per page when the PDF has no bookmarks. Manuals such as the
    ORCA, Q-Chem, Gaussian and Molpro PDFs have them, which gives chunks titled like
    "ORCA manual > Geometry Optimization > Transition State Searches (pp. 312-314)".
    """
    reader = _pdf_reader(path)
    pages = _page_texts(reader)
    doc_title = doc_title or path.stem
    outline = pdf_outline(reader)
    if not outline:
        return [([doc_title], t, i, i) for i, t in enumerate(pages, start=1) if t.strip()]

    # Locate every heading as (page, offset) within the extracted text.
    starts: list[tuple[int, int]] = []
    cursor_page, cursor_off = 0, 0
    for page, _depth, title in outline:
        if page != cursor_page:
            cursor_page, cursor_off = page, 0
        off = _find_title(pages[page], title, cursor_off) if page < len(pages) else -1
        if off < 0:
            off = cursor_off
        starts.append((page, off))
        cursor_off = off

    def text_between(a: tuple[int, int], b: tuple[int, int] | None) -> str:
        (pa, oa) = a
        (pb, ob) = b if b else (len(pages) - 1, len(pages[-1]))
        if pa == pb:
            return pages[pa][oa:ob]
        parts = [pages[pa][oa:]] + pages[pa + 1: pb] + [pages[pb][:ob]]
        return "\n".join(parts)

    sections: list[tuple[list[str], str, int, int]] = []
    stack: list[tuple[int, str]] = []
    for i, (page, depth, title) in enumerate(outline):
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack.append((depth, title))
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        if _SKIP_SECTIONS.match(title):
            continue
        body = text_between(starts[i], nxt).strip()
        last_page = (nxt[0] if nxt and nxt[1] > 0 else (nxt[0] - 1 if nxt else len(pages) - 1))
        if body:
            sections.append(([doc_title] + [t for _, t in stack], body, page + 1, max(page, last_page) + 1))
    return sections


def _page_garbled(text: str) -> bool:
    visible = [ch for ch in text if not ch.isspace()]
    if len(visible) < 50:
        return False
    if text.count("(cid:") >= 3:
        return True
    letters = sum(ch.isalpha() for ch in visible)
    if letters / len(visible) < 0.55:
        return True
    words = re.findall(r"[A-Za-z]{4,}", text)
    if len(words) >= 20:
        no_vowel = sum(1 for w in words if not re.search(r"[aeiouyAEIOUY]", w))
        if no_vowel / len(words) > 0.3:
            return True
    return False


def pdf_quality(path: Path) -> dict:
    """Does this PDF have a usable text layer? Used by `rag-drg check-pdf` and ingest warnings.

    verdict:
      "ok"         - text on (nearly) every page
      "partial"    - some pages are images only (e.g. scanned figures/chapters)
      "scanned"    - little or no text: needs OCR (`ocrmypdf --skip-text in.pdf out.pdf`)
      "garbled"    - text exists but is unreadable (font encoding): `ocrmypdf --force-ocr`
    """
    reader = _pdf_reader(path)
    texts = _page_texts(reader)
    n = len(texts)
    empty = [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 30]
    garbled = [i + 1 for i, t in enumerate(texts) if _page_garbled(t)]
    outline = pdf_outline(reader)
    if n == 0:
        verdict = "scanned"
    elif len(empty) / n > 0.5:
        verdict = "scanned"
    elif len(garbled) / n > 0.2:
        verdict = "garbled"
    elif len(empty) / n > 0.05:
        verdict = "partial"
    else:
        verdict = "ok"
    sample_page = next((t for t in texts if len(t.strip()) > 200 and not _page_garbled(t)), "")
    return {
        "file": str(path),
        "pages": n,
        "pages_without_text": len(empty),
        "pages_garbled": len(garbled),
        "first_pages_without_text": empty[:15],
        "bookmarks": len(outline),
        "verdict": verdict,
        "sample": re.sub(r"\s+", " ", sample_page)[:300],
    }


# --------------------------------------------------------------------------- #
# Theory vs. parameters: manuals mix method background with keyword documentation.
# Agents asking "what is the keyword" should not get pages of equations, and vice versa.
# --------------------------------------------------------------------------- #

_THEORY_TITLE = re.compile(
    r"\b(theor\w*|background|formalism|derivation|equations?|mathematical|foundations?|"
    r"working equations|the \w+ (method|approximation|model)|introduction to)\b",
    re.IGNORECASE,
)
_PARAM_TITLE = re.compile(
    r"(\bkeywords?\b|\binput\b|\boptions?\b|\bparameters?\b|\bblock\b|\bsyntax\b|\busage\b|"
    r"\bexamples?\b|\blist of\b|\bsummary\b|\bvariables?\b|\bdirectives?\b|\$rem|%\w+|\bhow to\b|"
    r"\brunning\b|\bjob control\b)",
    re.IGNORECASE,
)
_CODE_LINE = re.compile(
    r"^\s*(!|%\w|\$\w|#[pPnNtT]?\s|end\b|\*\s*(xyz|int|gzmt|xyzfile)|@@@|\{|}|[A-Z][A-Z0-9_]{2,}\s+\S+\s*$)"
)
_MATH_CHARS = set("=∑∫∂αβγδεζηθλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ∆∇≤≥±×·⟨⟩†∈√∞")


def classify_section(crumbs: list[str], text: str) -> str:
    """Return "theory" or "reference" (keywords/usage) for a manual section."""
    leaf = crumbs[-1] if crumbs else ""
    if _PARAM_TITLE.search(leaf):
        return "reference"
    if _THEORY_TITLE.search(leaf):
        return "theory"
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return "reference"
    code_frac = sum(1 for l in lines if _CODE_LINE.match(l)) / len(lines)
    math_density = sum(1 for ch in text if ch in _MATH_CHARS) / max(1, len(text))
    if code_frac >= 0.12:
        return "reference"
    if math_density >= 0.008 and code_frac < 0.05:
        return "theory"
    # Inherit from an enclosing "Theory" chapter when the section itself is neutral.
    if any(_THEORY_TITLE.search(c) for c in crumbs[:-1]) and not any(_PARAM_TITLE.search(c) for c in crumbs[:-1]):
        return "theory"
    return "reference"


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

TEXT_SUFFIXES = {
    ".txt", ".inp", ".gjf", ".com", ".sh", ".slurm", ".pbs", ".sub", ".cfg", ".toml",
    ".json", ".yml", ".yaml", ".in", ".py", ".cc",
}
SUPPORTED_SUFFIXES = {".md", ".markdown", ".rst", ".html", ".htm", ".pdf"} | TEXT_SUFFIXES


def chunk_file(path: Path, rel_path: str, size: int = 1500, overlap: int = 200) -> tuple[dict, list[Chunk]]:
    """Chunk one file. Returns (front-matter metadata, chunks)."""
    suffix = path.suffix.lower()
    meta: dict = {}

    if suffix == ".pdf":
        chunks: list[Chunk] = []
        for crumbs, txt, first, last in pdf_sections(path):
            pages = f"p. {first}" if first == last else f"pp. {first}-{last}"
            title = f"{' > '.join(crumbs)} ({pages})"
            kind = classify_section(crumbs, txt)
            for piece in split_long(txt, size, overlap):
                chunks.append(Chunk(text=piece, title=title, path=rel_path, ordinal=len(chunks), kind=kind))
        return meta, chunks

    text = path.read_text(errors="replace")
    if suffix in (".md", ".markdown"):
        meta, body = split_front_matter(text)
        doc_title = meta.get("title")
        return meta, _sections_to_chunks(markdown_sections(body), rel_path, size, overlap, doc_title)
    if suffix == ".rst":
        return meta, _sections_to_chunks(rst_sections(text), rel_path, size, overlap)
    if suffix in (".html", ".htm"):
        title, md, url = parse_html(text)
        if url:
            meta["url"] = url
        chunks = _sections_to_chunks(markdown_sections(md), rel_path, size, overlap, title or None)
        for c in chunks:
            c.kind = classify_section(c.title.split(" > "), c.text)
        return meta, chunks
    if path.name == "read_options.cc":
        return meta, _sections_to_chunks(psi4_options_sections(text), rel_path, size, overlap)
    if suffix in (".yml", ".yaml") and "levels_of_theory" in text:
        from .levels import is_levels_file, levels_sections

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if is_levels_file(data):
            return dict(data.get("meta") or {}), _sections_to_chunks(levels_sections(data, path.name), rel_path, 4000, 0)
    if suffix in (".yml", ".yaml") and re.search(r"(?m)^errors:\s*$", text):
        # ESS error database (knowledge/ess/errors.yaml): one chunk per entry, "ESS errors > <software> > <id>".
        from .tools.diagnose import errors_sections, is_errors_file

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if is_errors_file(data):
            chunks = _sections_to_chunks(errors_sections(data), rel_path, 4000, 0)
            for c in chunks:  # "ESS errors > <software> > <id>": each entry belongs to one program
                parts = c.title.split(" > ")
                if len(parts) >= 2:
                    c.software = parts[1].lower()
            return dict(data.get("meta") or {}), chunks
    if suffix == ".json":
        return meta, _sections_to_chunks(json_sections(text, path.name), rel_path, size, overlap)
    if suffix == ".py":
        module = rel_path[:-3].replace("/", ".")
        # Code chunks can be a bit larger; splitting a function in half hurts more than it helps.
        return meta, _sections_to_chunks(python_sections(text, module), rel_path, int(size * 1.5), overlap)
    # Plain text / input templates / configs: keep whole when small.
    return meta, _sections_to_chunks([([path.name], text)], rel_path, size, overlap)


def chunk_html_string(html: str, url: str, size: int = 1500, overlap: int = 200) -> list[Chunk]:
    title, md, _ = parse_html(html)
    chunks = _sections_to_chunks(markdown_sections(md), url, size, overlap, title or None)
    for c in chunks:
        c.kind = classify_section(c.title.split(" > "), c.text)
    return chunks
