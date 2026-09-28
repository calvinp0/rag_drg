"""Level-of-theory support matrix: which ESS supports which method and how it is written.

The data lives in YAML files (``knowledge/ess/levels_of_theory.yaml``) with a top-level
``levels_of_theory`` list. It is indexed for search (one chunk per method) and served as
exact, structured answers by ``lookup_level_of_theory`` / ``rag-drg level``.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import yaml

from .config import Config

SUPPORT_LABEL = {
    "yes": "yes",
    "no": "no",
    "variant": "different method with a similar name",
    "partial": "partial / needs extras",
    "unknown": "unknown (check the manual)",
}


def _support(c: dict) -> str:
    # YAML 1.1 reads bare yes/no as booleans.
    v = c.get("support", "unknown")
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v).lower()


def is_levels_file(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("levels_of_theory"), list)


def _norm(s: str) -> str:
    s = s.lower().replace("ω", "w").replace("omega", "w")
    return re.sub(r"[^a-z0-9]", "", s)


def format_entry(entry: dict, codes: list[str], software: str | None = None) -> str:
    names = ", ".join(entry.get("aliases") or [])
    lines = [f"Level of theory: {entry['name']}" + (f" ({entry['category']})" if entry.get("category") else "")]
    if names:
        lines.append(f"Also written: {names}")
    if entry.get("notes"):
        lines.append(f"Notes: {str(entry['notes']).strip()}")
    lines += ["", "| Code | Supported | How to write it | Notes |", "|---|---|---|---|"]
    per_code = entry.get("codes") or {}
    shown = [software.lower()] if software else (codes or list(per_code))
    for code in shown:
        c = per_code.get(code) or {"support": "unknown"}
        support = SUPPORT_LABEL.get(_support(c), _support(c))
        lines.append(f"| {code} | {support} | {c.get('keyword', '')} | {c.get('notes', '')} |")
    if software:
        alts = [k for k, v in per_code.items() if k != software.lower() and _support(v) == "yes"]
        if alts:
            lines.append(f"\nSupported as-is in: {', '.join(alts)}")
    return "\n".join(lines)


def levels_sections(data: dict, name: str) -> list[tuple[list[str], str]]:
    codes = data.get("codes") or []
    return [
        (["Levels of theory", e["name"]], format_entry(e, codes))
        for e in data["levels_of_theory"]
        if isinstance(e, dict) and e.get("name")
    ]


def load_levels(cfg: Config) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    codes: list[str] = []
    roots = [s.path for s in cfg.sources if s.type == "local" and s.path and s.path.is_dir()]
    for root in roots:
        for f in sorted(list(Path(root).rglob("*.yaml")) + list(Path(root).rglob("*.yml"))):
            try:
                data = yaml.safe_load(f.read_text())
            except yaml.YAMLError:
                continue
            if is_levels_file(data):
                entries += [e for e in data["levels_of_theory"] if isinstance(e, dict) and e.get("name")]
                codes += [c for c in data.get("codes") or [] if c not in codes]
    return entries, codes


def lookup(cfg: Config, name: str | None = None, software: str | None = None) -> str:
    entries, codes = load_levels(cfg)
    if not entries:
        return "No levels-of-theory table found (expected knowledge/ess/levels_of_theory.yaml)."
    if not name or name.strip().lower() in ("list", "all", "*"):
        by_cat: dict[str, list[str]] = {}
        for e in entries:
            by_cat.setdefault(e.get("category", "other"), []).append(e["name"])
        return "Known levels of theory:\n" + "\n".join(f"- {c}: {', '.join(n)}" for c, n in by_cat.items())

    q = _norm(name)
    # A full level like "wb97xd/def2tzvp" or "DLPNO-CCSD(T)/cc-pVTZ": match the method part.
    method_q = _norm(re.split(r"[/\\]", name)[0])
    keys: dict[str, dict] = {}
    for e in entries:
        for k in [e["name"], *(e.get("aliases") or [])]:
            keys.setdefault(_norm(k), e)
    hit = keys.get(q) or keys.get(method_q)
    if hit is None:
        close = difflib.get_close_matches(method_q, list(keys), n=5, cutoff=0.6)
        names = sorted({keys[c]["name"] for c in close})
        if names:
            return f"No exact entry for '{name}'. Did you mean: {', '.join(names)}?"
        return (f"'{name}' is not in the levels-of-theory table. Search the manuals with "
                f"search_knowledge, and add an entry to knowledge/ess/levels_of_theory.yaml.")
    out = format_entry(hit, codes, software)
    if "/" in name:
        out += "\n\n(Basis set spelling differs too; see 'Basis set spelling' in ess/capabilities.md.)"
    return out
