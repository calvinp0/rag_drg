"""Static checks for ARC ``input.yml`` files, driven by the generated input schema (see schema.py).

Severity policy (same as the ESS input checker): "error" only when ARC raises, or silently does
something other than what was written; "warning" for very likely mistakes; "info" otherwise.
The table of checks lives in docs/arc-input.md; keep both in sync.
"""

from __future__ import annotations

import difflib
import re

import yaml

from .._inputcheck.model import Finding

REF_CARD = "arc/arc-essentials.md"
REF_SCHEMA = "arc/input_schema.snapshot.yaml"

# Keys that only appear in restart files (ARC writes them into restart.yml).
RESTART_KEYS = {"running_jobs", "output", "output_multi_spc"}
# A species entry needs one of these to have a structure (a TS may also get guesses from a reaction).
STRUCTURE_KEYS = ("smiles", "inchi", "adjlist", "xyz", "yml_path", "mol", "initial_xyz", "final_xyz",
                  "cheap_conformer", "most_stable_conformer", "conformers", "species_dict")
LEVEL_NAME_EXTRA = ("level_of_theory",)
ARKANE_LEVEL = "arkane_level_of_theory"
# A method name ending in a 4-digit year (b97d32023): years belong in the Level's `year` field.
YEAR_SUFFIX = re.compile(r"^(?P<base>.*[a-z].*?)[-_]?(?P<year>(19|20)\d\d)$")


# ------------------------------------------------------------------ YAML (ARC's loader semantics)


class ArcYamlLoader(yaml.SafeLoader):
    """Like ARC's ARCYAMLLoader (arc/common.py): only true/false are booleans; yes/no/on/off stay
    strings (so a bare `NO` label is the string "NO"). Safe: no python/* object tags except tuples.

    Aliases (``*name``) are refused: ARC inputs don't need them, and nested aliases expand
    exponentially (a few hundred bytes can take minutes and gigabytes to walk)."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, f"YAML aliases (*{event.anchor}) are not supported in ARC input files; "
                "write the value out", event.start_mark)
        return super().compose_node(parent, index)


ArcYamlLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ArcYamlLoader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
ArcYamlLoader.add_constructor("tag:yaml.org,2002:python/tuple",
                              lambda loader, node: tuple(loader.construct_sequence(node)))


def _line_map(text: str) -> tuple[dict[tuple, int], list[tuple[tuple, str, int]]]:
    """{path: 1-based line} for every mapping key / sequence item, and duplicate keys [(path, key, line)]."""
    lines: dict[tuple, int] = {}
    dups: list[tuple[tuple, str, int]] = []
    try:
        root = yaml.compose(text, Loader=ArcYamlLoader)
    except yaml.YAMLError:
        return lines, dups

    def walk(node, path):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for k, v in node.value:
                key = k.value if isinstance(k, yaml.ScalarNode) else str(k.start_mark)
                p = path + (key,)
                if key in seen:
                    dups.append((path, key, k.start_mark.line + 1))
                seen.add(key)
                lines.setdefault(p, k.start_mark.line + 1)
                walk(v, p)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                p = path + (i,)
                lines.setdefault(p, item.start_mark.line + 1)
                walk(item, p)

    if root is not None:
        walk(root, ())
    return lines, dups


def load_arc_yaml(text: str):
    return yaml.load(text, Loader=ArcYamlLoader)  # noqa: S506 - SafeLoader subclass


# ------------------------------------------------------------------ helpers


def _suggest(name: str, choices) -> str | None:
    choices = [str(c) for c in choices]
    low = {c.lower(): c for c in choices}
    if str(name).lower() in low and low[str(name).lower()] != name:
        return low[str(name).lower()]
    m = difflib.get_close_matches(str(name), choices, n=1, cutoff=0.7) or \
        difflib.get_close_matches(str(name).lower(), list(low), n=1, cutoff=0.7)
    if not m:
        return None
    return low.get(m[0], m[0])


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TYPE_WORDS = {"int": "int", "float": "float", "str": "str", "bool": "bool", "dict": "dict", "list": "list",
               "tuple": "list", "None": "none"}


def _hint_kinds(hint: str | None) -> set[str] | None:
    """'int | None' -> {'int', 'none'}; None when the hint has anything we don't model (Level, Molecule...)."""
    if not hint:
        return None
    kinds = set()
    for part in re.split(r"\s*\|\s*", re.sub(r"\[.*\]", "", hint)):
        part = part.strip().strip("'\"")
        if part not in _TYPE_WORDS:
            return None
        kinds.add(_TYPE_WORDS[part])
    return kinds


def _value_ok(value, kinds: set[str]) -> bool:
    if value is None:
        return True  # None is how YAML spells "use the default"; ARC treats it as not given
    if isinstance(value, bool):
        return "bool" in kinds
    if isinstance(value, int):
        return bool(kinds & {"int", "float"})
    if isinstance(value, float):
        # A float where ARC hints int (e.g. job_memory: 7.5) works in practice; not worth an error.
        return bool(kinds & {"int", "float"})
    if isinstance(value, str):
        return "str" in kinds
    if isinstance(value, dict):
        return "dict" in kinds
    if isinstance(value, (list, tuple)):
        return "list" in kinds
    return True


def _kind_name(value) -> str:
    return {bool: "a boolean", int: "an integer", float: "a number", str: "a string", dict: "a mapping",
            list: "a list", tuple: "a list"}.get(type(value), type(value).__name__)


def _levels_support(e: dict, program: str) -> tuple[str, dict] | None:
    c = (e.get("codes") or {}).get(program)
    if not isinstance(c, dict):
        return None
    v = c.get("support", "unknown")
    v = "yes" if v is True else "no" if v is False else str(v).lower()
    return v, c


# ------------------------------------------------------------------ checker


class _Checker:
    def __init__(self, schema: dict, text: str, filename: str, cfg, servers):
        self.s = schema
        self.text = text
        self.filename = filename
        self.cfg = cfg
        self.servers = servers  # None = no servers.yaml; else set of names (lower-case)
        self.lines, self.dups = _line_map(text)
        self.out: list[Finding] = []
        self.arc_params = (schema.get("arc") or {}).get("params") or {}
        self.species_keys = set((schema.get("species") or {}).get("input_keys") or [])
        self.species_params = (schema.get("species") or {}).get("params") or {}
        self.species_read = set((schema.get("species") or {}).get("dict_keys_read") or [])
        self.reaction_keys = set((schema.get("reaction") or {}).get("input_keys") or [])
        self.reaction_params = (schema.get("reaction") or {}).get("params") or {}
        self.reaction_read = set((schema.get("reaction") or {}).get("dict_keys_read") or [])
        self.level_keys = list((schema.get("level") or {}).get("dict_keys") or [])
        self.level_params = (schema.get("level") or {}).get("params") or {}
        jt = schema.get("job_types") or {}
        self.job_types = list(jt.get("keys") or [])
        self.job_aliases = dict(jt.get("legacy_aliases") or {})
        self.job_renamed = dict(jt.get("renamed_error") or {})
        self._levels_keys = None

    # -------------------------------------------------------------- utils
    def line(self, *path) -> int | None:
        while path:
            if path in self.lines:
                return self.lines[path]
            path = path[:-1]
        return None

    def add(self, sev, code, msg, path=(), fix=None, ref=REF_CARD):
        self.out.append(Finding(sev, code, msg, self.line(*path) if path else None, fix=fix, ref=ref))

    def levels_index(self):
        if self._levels_keys is None:
            from .._inputcheck.common import levels_index

            self._levels_keys = levels_index(self.cfg) if self.cfg is not None else {}
        return self._levels_keys

    # -------------------------------------------------------------- top level
    def run(self, data) -> list[Finding]:
        for path, key, ln in self.dups:
            where = ".".join(str(p) for p in path) or "top level"
            self.out.append(Finding("warning", "arc-duplicate-key",
                                    f"Key '{key}' appears twice ({where}); YAML keeps only the last one.", ln,
                                    fix="remove one of them"))
        if data is None:
            self.add("error", "arc-empty", "The input file is empty.")
            return self.out
        if not isinstance(data, dict):
            self.add("error", "arc-not-mapping", f"An ARC input must be a YAML mapping of ARC arguments, got "
                     f"{_kind_name(data)}.")
            return self.out
        self.top_keys(data)
        self.project(data)
        self.types(data)
        species = self.species(data)
        self.reactions(data, species)
        self.job_types_check(data)
        self.levels(data)
        self.ess_settings(data)
        self.adapters(data)
        self.misc(data, species)
        return self.out

    def top_keys(self, data: dict):
        known = list(self.arc_params)
        for key in data:
            if key in self.arc_params:
                continue
            sug = _suggest(key, known)
            if key in self.species_keys and key not in self.arc_params:
                hint = f" '{key}' is a species key; put it inside a `species` entry."
            else:
                hint = ""
            self.add("error", "arc-unknown-key",
                     f"'{key}' is not an ARC argument; ARC(**input) raises TypeError."
                     + (f" Did you mean '{sug}'?" if sug else "") + hint,
                     (key,), fix=f"rename it to '{sug}'" if sug else "remove it (see the ARC input schema)",
                     ref=REF_SCHEMA)

    def project(self, data: dict):
        if "project" not in data or data.get("project") in (None, ""):
            if RESTART_KEYS & set(data):
                return
            self.add("error", "arc-project-missing", "`project` is required: ARC raises 'A project name must be "
                     "provided for a new project'.", fix="add `project: <name>` (letters, digits, - _ . [ ] = ,)")
            return
        proj = data["project"]
        if not isinstance(proj, str):
            self.add("error", "arc-type", f"`project` must be a string, got {_kind_name(proj)} ({proj!r}).",
                     ("project",), fix="quote it")
            return
        valid = self.s.get("project_valid_chars")
        if valid:
            bad = sorted({c for c in proj if c not in valid})
            if bad:
                self.add("error", "arc-project-name", f"Project name {proj!r} contains {''.join(bad)!r}; ARC only "
                         "allows letters, digits and - _ [ ] = . , (it names folders after the project).",
                         ("project",), fix="e.g. " + re.sub(r"[^\w\-\[\]=.,]", "_", proj))

    def types(self, data: dict):
        for key, value in data.items():
            p = self.arc_params.get(key)
            if not p or key in ("species", "reactions", "job_types", "ess_settings", "ts_adapters",
                                "adaptive_levels") or self._is_level_param(key):
                continue
            kinds = _hint_kinds(p.get("type"))
            if kinds is None or _value_ok(value, kinds):
                continue
            numeric = kinds & {"int", "float"} and not kinds & {"str", "dict", "list"}
            sev = "error" if numeric or kinds <= {"dict", "list", "none"} else "warning"
            want = " or ".join(sorted(k for k in kinds if k != "none"))
            self.add(sev, "arc-type", f"`{key}` should be {want} (ARC: `{p.get('type')}`), got {_kind_name(value)} "
                     f"({value!r}).", (key,),
                     fix="write a plain number without units or quotes" if numeric else None, ref=REF_SCHEMA)

    # -------------------------------------------------------------- species
    def species(self, data: dict) -> dict[str, dict]:
        labels: dict[str, dict] = {}
        self.label_idx: dict[str, int] = {}
        spcs = data.get("species")
        if spcs is None:
            return labels
        if not isinstance(spcs, list):
            self.add("error", "arc-species-type", f"`species` must be a list of species entries (`- label: ...`), got "
                     f"{_kind_name(spcs)}.", ("species",), fix="start each species with '- label: ...'")
            return labels
        for i, spc in enumerate(spcs):
            path = ("species", i)
            if not isinstance(spc, dict):
                self.add("error", "arc-species-type", f"species[{i}] must be a mapping (label, smiles, ...), got "
                         f"{_kind_name(spc)} ({spc!r}).", path)
                continue
            for key in spc:
                if key not in self.species_keys:
                    sug = _suggest(key, sorted(self.species_keys))
                    self.add("error", "arc-species-unknown-key",
                             f"species[{i}]: '{key}' is not an ARCSpecies key; ARC ignores it silently."
                             + (f" Did you mean '{sug}'?" if sug else ""), path + (key,),
                             fix=f"rename it to '{sug}'" if sug else "remove it", ref=REF_SCHEMA)
                elif self.species_read and key not in self.species_read and key != "species_dict":
                    self.add("info", "arc-species-key-ignored",
                             f"species[{i}]: '{key}' is an ARCSpecies() argument, but ARC does not read it from "
                             "input-file entries (ARCSpecies.from_dict), so it has no effect here.", path + (key,),
                             ref=REF_SCHEMA)
                else:
                    self._typed(spc[key], self.species_params.get(key), f"species[{i}].{key}", path + (key,))
            label = spc.get("label")
            is_ts = spc.get("is_ts") is True
            if "label" not in spc or label in (None, ""):
                self.add("error", "arc-species-label", f"species[{i}] has no `label`; ARC raises 'All species must "
                         "have a label'.", path, fix="add `label: <name>`")
            elif not isinstance(label, str):
                self.add("error", "arc-species-label", f"species[{i}]: label {label!r} is {_kind_name(label)}, not a "
                         "string; ARC raises TypeError.", path + ("label",), fix=f"quote it: label: '{label}'")
            else:
                if label in labels:
                    self.add("error", "arc-species-duplicate", f"Species label '{label}' is used twice; ARC raises "
                             "'Species label ... is not unique'.", path + ("label",))
                else:
                    labels[label] = spc
                    self.label_idx[label] = i
                if re.fullmatch(r"TS\d*", label) and not is_ts:
                    self.add("error", "arc-species-label", f"'{label}': a non-TS species cannot be named TS<number> "
                             "(ARC raises SpeciesError).", path + ("label",), fix="rename it, or set is_ts: true")
            if "is_ts" in spc and not isinstance(spc["is_ts"], bool):
                self.add("error", "arc-type", f"species[{i}].is_ts must be true/false, got {spc['is_ts']!r} "
                         "(ARC reads yes/no as strings).", path + ("is_ts",))
            if not is_ts and not any(spc.get(k) not in (None, "", [], {}) for k in STRUCTURE_KEYS):
                self.add("error", "arc-species-no-structure",
                         f"species '{label or i}' has no structure: give one of smiles, inchi, adjlist, xyz or "
                         "yml_path.", path)
            self._spin(spc, i, path)
        return labels

    def _typed(self, value, p, name, path):
        if not p:
            return
        kinds = _hint_kinds(p.get("type"))
        if kinds is None or _value_ok(value, kinds):
            return
        numeric = kinds & {"int", "float"} and not kinds & {"str", "dict", "list"}
        if numeric:
            self.add("error", "arc-type", f"{name} should be a number (ARC: `{p.get('type')}`), got {_kind_name(value)} "
                     f"({value!r}).", path, ref=REF_SCHEMA)

    def _spin(self, spc: dict, i: int, path):
        smiles = spc.get("smiles")
        if not isinstance(smiles, str) or not smiles or spc.get("is_ts") is True:
            return
        try:
            from rdkit import Chem, RDLogger
        except ImportError:
            return
        RDLogger.DisableLog("rdApp.*")
        mol = Chem.MolFromSmiles(smiles)
        label = spc.get("label", i)
        if mol is None:
            self.add("warning", "arc-smiles", f"species '{label}': RDKit cannot parse SMILES {smiles!r}.",
                     path + ("smiles",))
            return
        molh = Chem.AddHs(mol)
        formal = Chem.GetFormalCharge(mol)
        charge = spc.get("charge")
        if _is_num(charge) and int(charge) != formal:
            self.add("warning", "arc-charge", f"species '{label}': charge {charge} but SMILES {smiles!r} has net "
                     f"formal charge {formal}.", path + ("charge",))
        q = int(charge) if _is_num(charge) else formal
        electrons = sum(a.GetAtomicNum() for a in molh.GetAtoms()) - q
        mult = spc.get("multiplicity")
        radicals = sum(a.GetNumRadicalElectrons() for a in mol.GetAtoms())
        if _is_num(mult):
            if int(mult) < 1:
                self.add("error", "arc-multiplicity", f"species '{label}': multiplicity must be >= 1, got {mult}.",
                         path + ("multiplicity",))
            elif (electrons % 2 == 0) == (int(mult) % 2 == 0):
                self.add("warning", "arc-parity", f"species '{label}': {electrons} electrons (SMILES {smiles!r}, "
                         f"charge {q}) cannot have multiplicity {mult} ({'even' if electrons % 2 == 0 else 'odd'} "
                         f"electron count needs an {'odd' if electrons % 2 == 0 else 'even'} multiplicity).",
                         path + ("multiplicity",), fix="check the SMILES radicals, charge and multiplicity")
            elif radicals >= 2 and int(mult) == 1:
                self.add("info", "arc-singlet-diradical", f"species '{label}': SMILES has {radicals} unpaired "
                         "electrons but multiplicity 1 (open-shell singlet?). Consider `number_of_radicals`.",
                         path + ("multiplicity",))

    # -------------------------------------------------------------- reactions
    def reactions(self, data: dict, labels: dict[str, dict]):
        rxns = data.get("reactions")
        if rxns is None:
            return
        if not isinstance(rxns, list):
            self.add("error", "arc-reaction-type", f"`reactions` must be a list of reaction entries, got "
                     f"{_kind_name(rxns)}.", ("reactions",))
            return
        species_ok = isinstance(data.get("species"), list)
        for i, rxn in enumerate(rxns):
            path = ("reactions", i)
            if not isinstance(rxn, dict):
                self.add("error", "arc-reaction-type", f"reactions[{i}] must be a mapping (label: 'A + B <=> C'), "
                         f"got {_kind_name(rxn)}.", path)
                continue
            for key in rxn:
                if key not in self.reaction_keys:
                    sug = _suggest(key, sorted(self.reaction_keys))
                    self.add("error", "arc-reaction-unknown-key",
                             f"reactions[{i}]: '{key}' is not an ARCReaction key; ARC ignores it silently."
                             + (f" Did you mean '{sug}'?" if sug else ""), path + (key,),
                             fix=f"rename it to '{sug}'" if sug else "remove it", ref=REF_SCHEMA)
                elif self.reaction_read and key not in self.reaction_read:
                    self.add("info", "arc-reaction-key-ignored",
                             f"reactions[{i}]: '{key}' is an ARCReaction() argument that ARC does not read from "
                             "input-file entries (ARCReaction.from_dict); it has no effect here.", path + (key,),
                             ref=REF_SCHEMA)
                else:
                    self._typed(rxn[key], self.reaction_params.get(key), f"reactions[{i}].{key}", path + (key,))
            reactants, products = rxn.get("reactants") or [], rxn.get("products") or []
            label = rxn.get("label")
            where = ("reactants",)
            if not (reactants and products):
                if not label:
                    self.add("error", "arc-reaction-label", f"reactions[{i}] needs `label: 'A + B <=> C + D'` or "
                             "both `reactants` and `products`.", path)
                    continue
                if not isinstance(label, str) or " <=> " not in label:
                    self.add("error", "arc-reaction-label", f"reactions[{i}]: label {label!r} must contain ' <=> ' "
                             "(with spaces); ARC splits on ' <=> ' and ' + '.", path + ("label",),
                             fix="e.g. label: 'CH4 + OH <=> CH3 + H2O'")
                    continue
                left, right = label.split(" <=> ", 1)
                reactants = [x.strip() for x in left.split(" + ")]
                products = [x.strip() for x in right.split(" + ")]
                where = ("label",)
                if any("+" in x for x in reactants + products):
                    self.add("warning", "arc-reaction-label", f"reactions[{i}]: '+' without spaces around it in "
                             f"{label!r}; ARC only splits on ' + '.", path + ("label",))
            if not isinstance(reactants, list) or not isinstance(products, list):
                self.add("error", "arc-reaction-type", f"reactions[{i}]: reactants/products must be lists of "
                         "species labels.", path)
                continue
            if species_ok:
                for lab in reactants + products:
                    if str(lab) not in labels:
                        sug = _suggest(str(lab), list(labels))
                        self.add("error", "arc-reaction-species",
                                 f"reactions[{i}]: species '{lab}' is not defined under `species`; ARC raises "
                                 "ValueError." + (f" Did you mean '{sug}'?" if sug else ""), path + where,
                                 fix="add the species, or fix the label (labels are case-sensitive)")
            guesses = rxn.get("ts_xyz_guess") or rxn.get("xyz")
            if (len(reactants) > 3 or len(products) > 3) and not guesses:
                self.add("error", "arc-reaction-size", f"reactions[{i}]: ARC handles up to three reactants/products "
                         "without a TS guess (ReactionError).", path)
            ts_label = rxn.get("ts_label")
            if ts_label is not None and species_ok:
                ts = labels.get(str(ts_label))
                if ts is None:
                    self.add("warning", "arc-reaction-ts", f"reactions[{i}]: ts_label '{ts_label}' is not a species "
                             "label; ARC will create its own TS species.", path + ("ts_label",))
                elif ts.get("is_ts") is not True:
                    self.add("warning", "arc-reaction-ts", f"reactions[{i}]: ts_label '{ts_label}' names a species "
                             "without `is_ts: true`.", path + ("ts_label",))

    # -------------------------------------------------------------- job types
    def job_types_check(self, data: dict):
        jt = data.get("job_types")
        sjt = data.get("specific_job_type")
        if jt is not None:
            if not isinstance(jt, dict):
                self.add("error", "arc-job-types-type", f"`job_types` must be a mapping of job type -> true/false, "
                         f"got {_kind_name(jt)}.", ("job_types",))
            else:
                for key, val in jt.items():
                    if key in self.job_aliases:
                        self.add("info", "arc-job-type-legacy", f"job_types: '{key}' is a legacy alias of "
                                 f"'{self.job_aliases[key]}' (ARC converts it).", ("job_types", key),
                                 fix=f"write '{self.job_aliases[key]}'")
                    elif key not in self.job_types:
                        sug = self.job_renamed.get(key) or _suggest(key, self.job_types)
                        self.add("error", "arc-job-type-unknown", f"job_types: '{key}' is not a job type; ARC raises "
                                 "InputError." + (f" Did you mean '{sug}'?" if sug else "")
                                 + f" Valid: {', '.join(self.job_types)}.", ("job_types", key),
                                 fix=f"rename it to '{sug}'" if sug else None)
                    if not isinstance(val, bool):
                        sev = "error" if isinstance(val, str) else "warning"
                        self.add(sev, "arc-job-type-value", f"job_types.{key} should be true or false, got {val!r}"
                                 + (" (ARC reads yes/no/on/off as strings, and any non-empty string counts as "
                                    "true)" if isinstance(val, str) else "") + ".", ("job_types", key))
        if sjt not in (None, ""):
            valid = set(self.job_types) | set(self.job_aliases)
            if not isinstance(sjt, str) or sjt not in valid:
                sug = _suggest(str(sjt), sorted(valid))
                self.add("error", "arc-specific-job-type", f"specific_job_type {sjt!r} is not a job type; ARC raises "
                         "InputError." + (f" Did you mean '{sug}'?" if sug else ""), ("specific_job_type",))
            elif sjt == "stability":
                self.add("error", "arc-specific-stability", "`specific_job_type: stability` runs nothing: it replaces "
                         "job_types with only `stability`, which switches off the opt/freq/sp jobs the analysis is "
                         "spawned from.", ("specific_job_type",),
                         fix="remove specific_job_type and set `job_types: {stability: true}` (Gaussian/ORCA only)")
            else:
                settings_default = (self.s.get("job_types") or {}).get("settings_default") or {}
                legacy_of = {v: k for k, v in self.job_aliases.items()}
                if sjt in legacy_of and legacy_of[sjt] in settings_default:
                    self.add("warning", "arc-specific-job-type-alias",
                             f"`specific_job_type: {sjt}`: ARC's default_job_types still uses the legacy key "
                             f"'{legacy_of[sjt]}', whose False value overwrites '{sjt}' during normalisation "
                             "(arc/common.py initialize_job_types), so nothing may run.", ("specific_job_type",),
                             fix=f"use `specific_job_type: {legacy_of[sjt]}` or job_types instead")
            if isinstance(jt, dict) and jt:
                self.add("warning", "arc-specific-with-job-types", "Both `specific_job_type` and `job_types` are "
                         "given: specific_job_type replaces job_types wholesale, so the job_types entries are "
                         "ignored.", ("specific_job_type",), fix="keep only one of them")

    # -------------------------------------------------------------- levels
    def _is_level_param(self, key: str) -> bool:
        p = self.arc_params.get(key) or {}
        return key in LEVEL_NAME_EXTRA or "Level" in str(p.get("type") or "")

    def levels(self, data: dict):
        lot = data.get("level_of_theory")
        if lot not in (None, ""):
            for other in ("opt_level", "sp_level", "composite_method"):
                if data.get(other):
                    self.add("error", "arc-level-conflict", f"Both `level_of_theory` and `{other}` are given; ARC "
                             "raises InputError.", (other,), fix=f"drop one; level_of_theory is 'sp//opt' shorthand")
            if not isinstance(lot, str):
                self.add("error", "arc-level-type", "`level_of_theory` must be a string ('sp//opt' or 'method/basis');"
                         " use opt_level/sp_level for dict levels.", ("level_of_theory",))
            elif lot.count("//") > 1:
                self.add("error", "arc-level-format", f"level_of_theory {lot!r} has more than one '//'.",
                         ("level_of_theory",))
            else:
                for part in lot.split("//"):
                    self.level(part, "level_of_theory", ("level_of_theory",))
        for key, value in data.items():
            if key == "level_of_theory" or not self._is_level_param(key) or value in (None, ""):
                continue
            self.level(value, key, (key,))
        ad = data.get("adaptive_levels")
        if ad is not None:
            if not isinstance(ad, list):
                self.add("error", "arc-adaptive-levels", "`adaptive_levels` must be a list of {atom_range, levels} "
                         "entries.", ("adaptive_levels",))
            else:
                for i, entry in enumerate(ad):
                    p = ("adaptive_levels", i)
                    if not isinstance(entry, dict) or "atom_range" not in entry or "levels" not in entry:
                        self.add("error", "arc-adaptive-levels", f"adaptive_levels[{i}] needs `atom_range` and "
                                 "`levels` keys.", p)
                        continue
                    if isinstance(entry["levels"], dict):
                        for jt, lvl in entry["levels"].items():
                            self.level(lvl, f"adaptive_levels[{i}].levels.{jt}", p + ("levels", jt))

    def level(self, value, name: str, path: tuple):
        if isinstance(value, str):
            if " " in value.strip():
                self.add("error", "arc-level-format", f"{name}: {value!r} contains spaces; ARC raises ValueError. Use "
                         "a dict (method, basis, auxiliary_basis, dispersion ...).", path)
                return
            if value.count("/") >= 2:
                self.add("error", "arc-level-format", f"{name}: {value!r} has more than one '/'; ARC raises "
                         "ValueError. Use a dict to give auxiliary basis sets etc.", path)
                return
            method, _, basis = value.partition("/")
            self._method_checks(method, basis or None, None, name, path, {})
        elif isinstance(value, dict):
            for key in value:
                if key not in self.level_keys:
                    sug = _suggest(key, self.level_keys)
                    self.add("error", "arc-level-unknown-key", f"{name}: '{key}' is not a level key; ARC raises "
                             "'Got an illegal key'." + (f" Did you mean '{sug}'?" if sug else "")
                             + f" Allowed: {', '.join(self.level_keys)}.", path + (key,),
                             fix=f"rename it to '{sug}'" if sug else None, ref=REF_SCHEMA)
            if "method" not in value or not value.get("method"):
                self.add("error", "arc-level-method", f"{name}: a level dict needs a `method` key.", path)
                return
            if bool(value.get("solvation_method")) != bool(value.get("solvent")):
                self.add("error", "arc-level-solvation", f"{name}: `solvation_method` and `solvent` must be given "
                         "together; ARC raises ValueError.", path)
            year = value.get("year")
            if year is not None:
                if not _is_num(year) or not 1000 <= int(year) <= 9999:
                    self.add("error", "arc-level-year", f"{name}: year must be a 4-digit integer, got {year!r}.",
                             path + ("year",))
                elif name != ARKANE_LEVEL:
                    self.add("warning", "arc-level-year", f"{name}: `year` has no effect here; it is only used for "
                             "Arkane correction matching via arkane_level_of_theory.", path + ("year",))
            self._method_checks(str(value["method"]), value.get("basis"), value.get("software"), name, path, value)
        elif value is not None:
            self.add("error", "arc-level-type", f"{name}: a level must be a string 'method/basis' or a dict, got "
                     f"{_kind_name(value)}.", path)

    def _method_checks(self, method: str, basis, software, name: str, path: tuple, raw: dict):
        m = method.strip().lower()
        ym = YEAR_SUFFIX.match(m)
        if ym and not m.startswith("uma"):
            self.add("warning", "arc-level-year-in-method", f"{name}: method '{method}' ends in a year; ESS "
                     "keywords don't carry years. Write the plain method and, for Arkane corrections, "
                     f"`arkane_level_of_theory: {{method: {ym.group('base')}, year: {ym.group('year')}}}`.",
                     path + (("method",) if raw else ()))
        if name == ARKANE_LEVEL:
            return  # never run by an ESS; only used to look up Arkane corrections
        supported = [e.lower() for e in self.s.get("supported_ess") or []]
        adapters = [a.lower() for a in self.s.get("job_adapters") or []]
        routed_by = None
        if software:
            sw = str(software).lower()
            if sw not in supported and sw not in adapters:
                sug = _suggest(sw, supported)
                self.add("error", "arc-level-software", f"{name}: software '{software}' is not an ESS ARC supports"
                         + (f"; did you mean '{sug}'?" if sug else ".") + f" Supported: {', '.join(supported)}.",
                         path + ("software",))
                return
            ess, routed_by = sw, "explicit"
        elif "dlpno" in m:
            ess, routed_by = "orca", "dlpno"
        else:
            ess = None
            for code, phrases in (self.s.get("levels_ess") or {}).items():
                for ph in phrases or []:
                    if ph in m or (basis and ph in str(basis).lower()):
                        ess, routed_by = str(code).lower(), f"levels_ess phrase '{ph}'"
                        break
                if ess:
                    break
        if not ess:
            return
        keys = self.levels_index()
        if not keys:
            return
        from .._inputcheck.common import lookup_method

        e = lookup_method(keys, method, ess)
        if e is None:
            return
        got = _levels_support(e, ess)
        if got is None:
            return
        sup, c = got
        how = f" In {ess} write it as: {c['keyword']}." if c.get("keyword") else ""
        notes = f" ({c['notes']})" if c.get("notes") else ""
        alts = [k for k, v in (e.get("codes") or {}).items() if _levels_support(e, k) and _levels_support(e, k)[0] == "yes"]
        via = "" if routed_by == "explicit" else f" (ARC routes it to {ess} via {routed_by}; set `software` to choose)"
        ref = f"ess/levels_of_theory.yaml#{e['name']}"
        certain = routed_by in ("explicit", "dlpno")
        if sup == "no":
            self.add("error" if certain else "warning", "arc-level-unsupported",
                     f"{name}: {e['name']} ('{method}') is not available in {ess}{via}{notes}. "
                     f"Supported as-is in: {', '.join(alts) or 'none listed'}.", path,
                     fix="choose another method or `software` (lookup_level_of_theory)", ref=ref)
        elif sup == "variant":
            self.add("warning", "arc-level-variant",
                     f"{name}: '{method}' ({e['name']}) is not the same method in {ess}{via}: the similar {ess} "
                     f"keyword is a different parametrisation.{how}{notes}", path,
                     fix=f"run it in {', '.join(alts) or 'another code'} (set `software`)", ref=ref)

    # -------------------------------------------------------------- ess_settings / adapters
    def ess_settings(self, data: dict):
        ess = data.get("ess_settings")
        if ess is None:
            return
        if not isinstance(ess, dict):
            self.add("error", "arc-ess-settings-type", "`ess_settings` must map ESS names to a server name or a list "
                     "of server names.", ("ess_settings",))
            return
        allowed = [k.lower() for k in self.s.get("ess_settings_keys") or []]
        for code, servers in ess.items():
            p = ("ess_settings", code)
            if allowed and str(code).lower() not in allowed:
                sug = _suggest(str(code).lower(), allowed)
                self.add("error", "arc-ess-unknown", f"ess_settings: '{code}' is not an ESS ARC recognises; ARC raises "
                         "SettingsError." + (f" Did you mean '{sug}'?" if sug else "")
                         + f" Recognised: {', '.join(allowed)}.", p)
            names = [servers] if isinstance(servers, str) else servers
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                self.add("error", "arc-ess-settings-type", f"ess_settings.{code}: servers must be a name or a list of "
                         f"names, got {servers!r}.", p)
                continue
            if self.servers is None:
                continue
            for n in names:
                if n.lower() != "local" and n.lower() not in self.servers:
                    sug = _suggest(n, sorted(self.servers) + ["local"])
                    self.add("warning", "arc-ess-server", f"ess_settings.{code}: server '{n}' is not in servers.yaml "
                             "(and not 'local'); ARC raises SettingsError unless your ~/.arc/settings.py defines it."
                             + (f" Did you mean '{sug}'?" if sug else ""), p,
                             fix="use a server from `rag-drg servers list`, or 'local'")

    def adapters(self, data: dict):
        tsa = data.get("ts_adapters")
        known = self.s.get("job_adapters") or []
        if tsa is not None:
            if not isinstance(tsa, list):
                self.add("error", "arc-ts-adapters-type", f"`ts_adapters` must be a list, got {_kind_name(tsa)}.",
                         ("ts_adapters",), fix=f"ts_adapters: [{tsa}]" if isinstance(tsa, str) else None)
            elif known:
                for a in tsa:
                    if str(a).lower() not in [k.lower() for k in known]:
                        sug = _suggest(str(a).lower(), known)
                        self.add("error", "arc-ts-adapter-unknown", f"ts_adapters: '{a}' is not a registered ARC "
                                 "adapter; ARC raises InputError." + (f" Did you mean '{sug}'?" if sug else ""),
                                 ("ts_adapters",))
        stat = self.s.get("statmech_adapters") or []
        for key in ("thermo_adapter", "kinetics_adapter"):
            v = data.get(key)
            if stat and isinstance(v, str) and v.lower() not in stat:
                self.add("error", "arc-statmech-adapter", f"{key}: '{v}' is not a statmech adapter ARC knows "
                         f"({', '.join(stat)}); ARC raises ValueError.", (key,))

    def ts_sources(self, data: dict, labels: dict[str, dict]):
        rxns = data.get("reactions") if isinstance(data.get("reactions"), list) else []
        referenced = {str(r.get("ts_label")) for r in rxns if isinstance(r, dict) and r.get("ts_label")}
        rxn_labels = {str(r.get("label")) for r in rxns if isinstance(r, dict) and r.get("label")}
        for lab, spc in labels.items():
            if spc.get("is_ts") is True and not any(spc.get(k) for k in STRUCTURE_KEYS) \
                    and lab not in referenced and str(spc.get("rxn_label")) not in rxn_labels:
                self.add("warning", "arc-ts-no-source", f"TS species '{lab}' has no xyz and no reaction points "
                         "to it (ts_label / rxn_label); ARC has nothing to build it from.",
                         ("species", self.label_idx.get(lab, 0)))

    def misc(self, data: dict, labels: dict):
        self.ts_sources(data, labels)
        dg = data.get("dont_gen_confs")
        if isinstance(dg, list) and isinstance(data.get("species"), list):
            for lab in dg:
                if str(lab) not in labels:
                    self.add("warning", "arc-dont-gen-confs", f"dont_gen_confs: '{lab}' is not a species label.",
                             ("dont_gen_confs",))


def run_checks(schema: dict, content: str, filename: str, cfg=None, servers=None) -> list[Finding]:
    try:
        data = load_arc_yaml(content)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        ctx = getattr(e, "context", None)
        return [Finding("error", "arc-yaml-error", f"YAML parse error: {problem}" + (f" ({ctx})" if ctx else ""),
                        line, fix="check indentation, colons and quoting around this line")]
    return _Checker(schema, content, filename, cfg, servers).run(data)
