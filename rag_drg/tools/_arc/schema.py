"""ARC input schema, generated statically from ARC's source code (AST only; ARC is never imported).

What it reads from an ARC checkout (``sources_cache/arc`` by default):

* ``arc/main.py``            ``ARC.__init__``: every top-level input key, its type hint, default and the
                             description from the class docstring (``Args:``, falling back to ``Attributes:``).
* ``arc/species/species.py`` ``ARCSpecies.__init__`` + the keys ``from_dict``/``as_dict`` read and write:
                             the keys allowed in a ``species`` entry.
* ``arc/reaction/reaction.py`` the same for ``ARCReaction`` (``reactions`` entries).
* ``arc/level.py``           ``Level.__init__`` and the dict keys ``Level.build`` accepts (any other key raises).
* ``arc/settings/settings.py`` ``supported_ess``, ``ts_adapters``, ``default_job_types``, ``levels_ess`` ...
* ``arc/common.py``          the job types ``initialize_job_types`` accepts and its legacy aliases, and the
                             extra ``ess_settings`` keys ``check_ess_settings`` accepts.
* ``arc/job/**/*.py``        adapters registered with ``register_job_adapter`` (valid ``ts_adapters`` values).
* ``arc/statmech/adapter.py`` ``StatmechEnum`` (valid ``thermo_adapter``/``kinetics_adapter``).
* ``docs/source/input_reference.rst`` legacy job-type aliases (``fine_grid -> fine``).

The result is a plain dict (JSON/YAML-serialisable), see ``generate_schema``. ``load_schema`` returns
the freshest one available: generated from the clone (cached in ``index/arc_input_schema.json`` per
ARC commit), else the committed snapshot ``knowledge/arc/input_schema.snapshot.yaml``.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
ARC_REPO = "https://github.com/ReactionMechanismGenerator/ARC"
SNAPSHOT_REL = Path("knowledge") / "arc" / "input_schema.snapshot.yaml"
INDEX_JSON_NAME = "arc_input_schema.json"
SNAPSHOT_META = {
    "title": "ARC input schema (generated from ARC's source)",
    "domain": "arc",
    "software": "arc",
    "doc_type": "schema",
    "status": "draft",
    "tags": ["input.yml", "input schema", "ARC input keys", "species", "reactions", "level of theory",
             "job_types", "ess_settings", "defaults"],
}

FILES = {
    "main": "arc/main.py",
    "species": "arc/species/species.py",
    "reaction": "arc/reaction/reaction.py",
    "level": "arc/level.py",
    "settings": "arc/settings/settings.py",
    "common": "arc/common.py",
    "statmech": "arc/statmech/adapter.py",
    "input_reference": "docs/source/input_reference.rst",
}


class SchemaError(RuntimeError):
    pass


# ------------------------------------------------------------------ git


def git_commit(repo: Path) -> str | None:
    """HEAD commit of a checkout, read from .git without running git when possible."""
    repo = Path(repo)
    gitdir = repo / ".git"
    try:
        if gitdir.is_file():  # worktree / submodule: "gitdir: <path>"
            target = gitdir.read_text().strip().split(":", 1)[1].strip()
            gitdir = (repo / target).resolve()
        head = (gitdir / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
        ref = head[4:].strip()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
        for base in (gitdir, common):
            f = base / ref
            if f.is_file():
                return f.read_text().strip()
            packed = base / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split()[0]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10, check=False)
        sha = out.stdout.strip()
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ AST helpers


def _parse(path: Path) -> tuple[ast.Module, str]:
    text = path.read_text(errors="replace")
    return ast.parse(text, filename=str(path)), text


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise SchemaError(f"class {name} not found")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _jsonable(v):
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(_jsonable(x) for x in v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return v


def _literal(node: ast.AST) -> tuple[bool, object]:
    try:
        return True, _jsonable(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


_SECTION = re.compile(r"^(Args|Arguments|Attributes|Returns?|Raises|Examples?|Notes?|Yields)\s*:\s*$")
_ENTRY = re.compile(r"^\*{0,2}(\w+)\s*\((.*?)\)\s*:\s*(.*)$")
_ENTRY_NOTYPE = re.compile(r"^\*{0,2}(\w+)\s*:\s*(.*)$")


def doc_sections(doc: str | None) -> dict[str, dict[str, dict]]:
    """Google-style docstring -> {"Args": {name: {"type", "description"}}, "Attributes": {...}}."""
    out: dict[str, dict[str, dict]] = {}
    if not doc:
        return out
    section = None
    section_indent = 0
    entry_indent = None
    cur = None
    for ln in doc.splitlines():
        s = ln.strip()
        ind = len(ln) - len(ln.lstrip())
        m = _SECTION.match(s)
        if m and (section is None or ind <= section_indent):
            section, section_indent, entry_indent, cur = m.group(1), ind, None, None
            if section == "Arguments":
                section = "Args"
            out.setdefault(section, {})
            continue
        if section is None or not s:
            continue
        if ind <= section_indent:
            section, cur = None, None
            continue
        if entry_indent is None:
            entry_indent = ind
        if ind == entry_indent:
            m = _ENTRY.match(s) or _ENTRY_NOTYPE.match(s)
            if m:
                cur = m.group(1)
                typ, desc = (m.group(2), m.group(3)) if m.re is _ENTRY else (None, m.group(2))
                out[section][cur] = {"type": typ, "description": desc}
                continue
        if cur:
            d = out[section][cur]
            d["description"] = (d["description"] + " " + s).strip()
    for sec in out.values():
        for d in sec.values():
            d["description"] = re.sub(r"\s+", " ", d["description"]).strip()
    return out


def _params(fn: ast.FunctionDef, text: str, docs: dict[str, dict[str, dict]]) -> dict[str, dict]:
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
    doc_args, doc_attrs = docs.get("Args", {}), docs.get("Attributes", {})
    out: dict[str, dict] = {}
    for arg, default in pairs:
        if arg.arg in ("self", "cls"):
            continue
        doc = doc_args.get(arg.arg) or doc_attrs.get(arg.arg) or {}
        entry: dict = {
            "type": ast.unparse(arg.annotation) if arg.annotation is not None else None,
            "doc_type": doc.get("type"),
            "has_default": default is not None,
        }
        if default is not None:
            ok, value = _literal(default)
            entry["default"] = value if ok else None
            entry["default_is_literal"] = ok
            entry["default_repr"] = ast.get_source_segment(text, default) or ast.unparse(default)
        entry["description"] = doc.get("description") or ""
        if not doc_args.get(arg.arg) and doc_attrs.get(arg.arg):
            entry["description_from"] = "Attributes"
        out[arg.arg] = entry
    return out


def _required(fn: ast.FunctionDef, params: dict) -> list[str]:
    """Parameters for which ``if <param> is None: raise ...`` appears in the function."""
    req = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id in params
                and len(t.ops) == 1 and isinstance(t.ops[0], ast.Is)
                and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value is None
                and any(isinstance(b, ast.Raise) for b in node.body)):
            req.append(t.left.id)
    return sorted(set(req))


def _settings_fallbacks(fn: ast.FunctionDef, params: dict) -> dict[str, tuple[str, str]]:
    """`<param> or <settings_dict>.get('<key>', ...)` in __init__: the effective default comes from settings."""
    out = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
            a, b = node.values
            if (isinstance(a, ast.Name) and a.id in params and isinstance(b, ast.Call)
                    and isinstance(b.func, ast.Attribute) and b.func.attr == "get"
                    and isinstance(b.func.value, ast.Name) and b.args and isinstance(b.args[0], ast.Constant)):
                out[a.id] = (b.func.value.id, str(b.args[0].value))
    return out


def _dict_keys_read(fn: ast.FunctionDef | None, var: str) -> set[str]:
    """String keys looked up on the dict variable `var` inside `fn` (x['k'], x.get('k'), 'k' in x)."""
    keys: set[str] = set()
    if fn is None:
        return keys
    for node in ast.walk(fn):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == var
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == var
              and node.func.attr in ("get", "pop", "setdefault") and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
              and isinstance(node.left.value, str) and len(node.ops) == 1
              and isinstance(node.ops[0], (ast.In, ast.NotIn)) and isinstance(node.comparators[0], ast.Name)
              and node.comparators[0].id == var):
            keys.add(node.left.value)
    return keys


def _returned_dict_keys(fn: ast.FunctionDef | None) -> set[str]:
    """Keys written into the dict a function returns (``d['k'] = ...``; ``return d``)."""
    if fn is None:
        return set()
    names = {n.value.id for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)}
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in names
                        and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                    keys.add(t.slice.value)
    return keys


def _first_arg(fn: ast.FunctionDef | None) -> str | None:
    if fn is None:
        return None
    names = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
    return names[0] if names else None


def _module_literals(tree: ast.Module, names: set[str]) -> dict[str, object]:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in names:
                ok, value = _literal(node.value)
                if ok:
                    out[name] = value
    return out


def _local_list(fn: ast.FunctionDef | None, name: str) -> list | None:
    if fn is None:
        return None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            ok, value = _literal(node.value)
            if ok:
                return value
    return None


# ------------------------------------------------------------------ pieces


def _class_schema(path: Path, class_name: str, dict_var_default: str | None) -> dict:
    tree, text = _parse(path)
    cls = _class(tree, class_name)
    init = _method(cls, "__init__")
    if init is None:
        raise SchemaError(f"{class_name}.__init__ not found in {path}")
    docs = doc_sections(ast.get_docstring(cls))
    params = _params(init, text, docs)
    out: dict = {"class": class_name, "params": params}
    required = _required(init, params)
    if required:
        out["required"] = required
    for name, (var, key) in _settings_fallbacks(init, params).items():
        params[name]["default_from_settings"] = f"{var}['{key}']"
    if dict_var_default:
        from_dict = _method(cls, "from_dict")
        var = _first_arg(from_dict) or dict_var_default
        read = _dict_keys_read(from_dict, var) | _dict_keys_read(init, var)
        written = _returned_dict_keys(_method(cls, "as_dict"))
        out["dict_keys_read"] = sorted(read)
        out["dict_keys_written"] = sorted(written)
        out["input_keys"] = sorted(set(params) | read | written)
    return out


def _level_schema(path: Path) -> dict:
    tree, text = _parse(path)
    cls = _class(tree, "Level")
    out = _class_schema(path, "Level", None)
    build = _method(cls, "build")
    keys: list[str] = []
    if build is not None:
        for node in ast.walk(build):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "level_dict" and isinstance(node.value, ast.Dict)):
                keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
                break
    if not keys:
        raise SchemaError("Level.build: level_dict keys not found")
    out["dict_keys"] = keys
    return out


def _job_types(common: Path, settings_default: dict | None, input_ref: Path | None) -> dict:
    tree, _ = _parse(common)
    fn = _function(tree, "initialize_job_types")
    if fn is None:
        raise SchemaError("initialize_job_types not found in arc/common.py")
    true_ = _local_list(fn, "defaults_to_true") or []
    false_ = _local_list(fn, "defaults_to_false") or []
    aliases: dict[str, str] = {}
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.value, ast.Subscript)):
            t, v = node.targets[0], node.value
            if (isinstance(t.value, ast.Name) and isinstance(v.value, ast.Name) and t.value.id == v.value.id
                    and isinstance(t.slice, ast.Constant) and isinstance(v.slice, ast.Constant)
                    and isinstance(t.slice.value, str) and isinstance(v.slice.value, str)):
                aliases[v.slice.value] = t.slice.value
    if input_ref is not None and input_ref.is_file():
        for old, new in re.findall(r"^\*\s+``(\w+)``\s*->\s*``(\w+)``", input_ref.read_text(errors="replace"), re.M):
            aliases.setdefault(old, new)
    renamed = {}
    for node in ast.walk(fn):  # `if job_type == '1d_rotors': logging.error("... renamed to simply `rotors`")`
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.comparators[0], ast.Constant)):
            old = node.test.comparators[0].value
            src = ast.unparse(node)
            m = re.search(r"renamed to (?:simply )?`(\w+)`", src)
            if isinstance(old, str) and m:
                renamed[old] = m.group(1)
    if not true_ and not false_:
        raise SchemaError("job types not found in initialize_job_types")
    return {"keys": list(true_) + list(false_), "defaults_true": list(true_), "defaults_false": list(false_),
            "legacy_aliases": aliases, "renamed_error": renamed, "settings_default": settings_default or {}}


def _ess_settings_extra(common: Path) -> list[str]:
    tree, _ = _parse(common)
    fn = _function(tree, "check_ess_settings")
    if fn is None:
        return []
    for node in ast.walk(fn):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.left, ast.Name)
                and node.left.id == "supported_ess"):
            ok, value = _literal(node.right)
            if ok and isinstance(value, list):
                return [str(v) for v in value]
    return []


def _job_adapters(root: Path) -> list[str]:
    names = set()
    for f in sorted((root / "arc" / "job").rglob("*.py")):
        if f.name.endswith("_test.py"):
            continue
        for m in re.finditer(r"^register_job_adapter\(\s*['\"]([\w.-]+)['\"]", f.read_text(errors="replace"), re.M):
            names.add(m.group(1))
    return sorted(names)


def _statmech(path: Path) -> list[str]:
    if not path.is_file():
        return []
    tree, _ = _parse(path)
    try:
        cls = _class(tree, "StatmechEnum")
    except SchemaError:
        return []
    out = []
    for node in cls.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out.append(node.value.value)
    return out


# ------------------------------------------------------------------ generate / load


def generate_schema(arc_path: str | Path) -> dict:
    """Build the input schema from an ARC checkout. Raises SchemaError if the layout is not recognised."""
    root = Path(arc_path)
    missing = [rel for key, rel in FILES.items() if key not in ("statmech", "input_reference")
               and not (root / rel).is_file()]
    if missing:
        raise SchemaError(f"not an ARC checkout (missing {', '.join(missing)}): {root}")
    try:
        arc = _class_schema(root / FILES["main"], "ARC", None)
        species = _class_schema(root / FILES["species"], "ARCSpecies", "species_dict")
        reaction = _class_schema(root / FILES["reaction"], "ARCReaction", "reaction_dict")
        level = _level_schema(root / FILES["level"])
        settings_tree, _ = _parse(root / FILES["settings"])
        settings = _module_literals(settings_tree, {"supported_ess", "ts_adapters", "default_job_types", "levels_ess",
                                                    "default_levels_of_theory", "default_job_settings"})
        job_types = _job_types(root / FILES["common"], settings.get("default_job_types"), root / FILES["input_reference"])
        extra_ess = _ess_settings_extra(root / FILES["common"])
        valid_chars = _valid_chars((root / FILES["settings"]).read_text(errors="replace"))
    except SyntaxError as e:
        raise SchemaError(f"could not parse ARC source: {e}") from e
    supported = list(settings.get("supported_ess") or [])
    for p in arc["params"].values():  # e.g. job_memory -> default_job_settings['job_total_memory_gb'] = 14
        m = re.fullmatch(r"(\w+)\['(\w+)'\]", p.get("default_from_settings") or "")
        if m and isinstance(settings.get(m.group(1)), dict):
            p["settings_value"] = settings[m.group(1)].get(m.group(2))
    return {
        "schema_version": SCHEMA_VERSION,
        "arc_repo": ARC_REPO,
        "arc_commit": git_commit(root),
        "generator": "rag-drg arc schema (static AST analysis; ARC is not imported)",
        "source_files": [rel for rel in FILES.values() if (root / rel).is_file()] + ["arc/job/**/*.py"],
        "arc": arc,
        "species": species,
        "reaction": reaction,
        "level": level,
        "job_types": job_types,
        "supported_ess": supported,
        "ess_settings_keys": sorted({s.lower() for s in supported + extra_ess}),
        "job_adapters": _job_adapters(root),
        "ts_adapters_default": list(settings.get("ts_adapters") or []),
        "levels_ess": settings.get("levels_ess") or {},
        "statmech_adapters": _statmech(root / FILES["statmech"]),
        "default_levels_of_theory": settings.get("default_levels_of_theory") or {},
        "default_job_settings": settings.get("default_job_settings") or {},
        "project_valid_chars": valid_chars,
    }


def _valid_chars(settings_text: str) -> str | None:
    """settings.valid_chars, written as `"-_[]=.,%s%s" % (string.ascii_letters, string.digits)`."""
    import string

    m = re.search(r"^valid_chars\s*=\s*(['\"])(.*?)\1\s*(?:%\s*\(([^)]*)\))?\s*$", settings_text, re.M)
    if not m:
        return None
    fmt, args = m.group(2), [a.strip() for a in (m.group(3) or "").split(",") if a.strip()]
    values = []
    for a in args:
        if not a.startswith("string.") or not hasattr(string, a[7:]):
            return None
        values.append(getattr(string, a[7:]))
    try:
        return fmt % tuple(values) if values else fmt
    except (TypeError, ValueError):
        return None


def write_schema(schema: dict, out: str | Path) -> Path:
    """JSON for *.json, otherwise the YAML snapshot format ({meta, arc_input_schema})."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        out.write_text(json.dumps(schema, indent=1) + "\n")
    else:
        header = (
            "# GENERATED by `rag-drg arc schema --out knowledge/arc/input_schema.snapshot.yaml` from ARC's source\n"
            "# (AST only). Do not edit by hand; regenerate after updating sources_cache/arc.\n"
            "# Used by `rag-drg arc check` when no ARC clone is available, and indexed for search\n"
            "# (one chunk per input key: \"ARC input > <key>\").\n"
        )
        body = yaml.safe_dump({"meta": SNAPSHOT_META, "arc_input_schema": schema}, sort_keys=False,
                              allow_unicode=True, width=110)
        out.write_text(header + body)
    return out


def read_schema_file(path: str | Path) -> dict | None:
    path = Path(path)
    try:
        text = path.read_text()
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if isinstance(data, dict) and isinstance(data.get("arc_input_schema"), dict):
        data = data["arc_input_schema"]
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or "arc" not in data:
        return None
    return data


def is_schema_file(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("arc_input_schema"), dict)


def default_arc_path(cfg) -> Path | None:
    if cfg is None:
        return None
    try:
        src = cfg.source("arc")
        if src.path is not None:
            return Path(src.path)
    except KeyError:
        pass
    return Path(cfg.cache_dir) / "arc"


def default_json_path(cfg) -> Path | None:
    return Path(cfg.index_path).parent / INDEX_JSON_NAME if cfg is not None else None


def snapshot_path(cfg=None) -> Path:
    if cfg is not None and (Path(cfg.root) / SNAPSHOT_REL).is_file():
        return Path(cfg.root) / SNAPSHOT_REL
    return Path(__file__).resolve().parents[3] / SNAPSHOT_REL


_CACHE: dict[tuple, dict] = {}


def load_schema(cfg=None, arc_path: str | Path | None = None, use_clone: bool = True) -> dict:
    """The freshest schema available.

    1. An ARC clone (``arc_path`` or the `arc` source's cache dir): the cached
       ``index/arc_input_schema.json`` if it was generated from the clone's current commit, else
       generated now (and the cache rewritten, best effort).
    2. The committed snapshot ``knowledge/arc/input_schema.snapshot.yaml``.

    The returned dict has an extra ``_origin`` key ("clone", "index-cache" or "snapshot").
    """
    clone = Path(arc_path) if arc_path else (default_arc_path(cfg) if use_clone else None)
    if clone is not None and (clone / FILES["main"]).is_file():
        commit = git_commit(clone)
        key = (str(clone.resolve()), commit)
        if key in _CACHE:
            return _CACHE[key]
        cache_file = default_json_path(cfg) if not arc_path else None
        if cache_file is not None and cache_file.is_file() and commit:
            cached = read_schema_file(cache_file)
            if cached and cached.get("arc_commit") == commit:
                cached["_origin"] = "index-cache"
                _CACHE[key] = cached
                return cached
        try:
            schema = generate_schema(clone)
        except (SchemaError, OSError):
            schema = None
        if schema is not None:
            if cache_file is not None:
                try:
                    write_schema(schema, cache_file)
                except OSError:
                    pass
            schema["_origin"] = "clone"
            _CACHE[key] = schema
            return schema
    snap = snapshot_path(cfg)
    key = ("snapshot", str(snap))
    if key not in _CACHE:
        data = read_schema_file(snap)
        if data is None:
            raise SchemaError(f"no ARC input schema: no ARC clone and no snapshot at {snap}")
        data["_origin"] = "snapshot"
        _CACHE[key] = data
    return _CACHE[key]


# ------------------------------------------------------------------ search chunks


def _fmt_default(p: dict) -> str:
    if not p.get("has_default"):
        return "(required positional)" if "default_repr" not in p else ""
    if p.get("default_is_literal"):
        return json.dumps(p.get("default"))
    return p.get("default_repr") or ""


def _param_text(scope: str, name: str, p: dict, extra: str = "") -> str:
    lines = [f"{scope} key `{name}`"]
    if p.get("type") or p.get("doc_type"):
        lines.append(f"Type: {p.get('type') or p.get('doc_type')}")
    d = _fmt_default(p)
    if d:
        lines.append(f"Default: {d}")
    if p.get("default_from_settings"):
        v = p.get("settings_value")
        lines.append(f"If omitted or null, ARC uses settings {p['default_from_settings']}"
                     + (f" = {json.dumps(v)}" if v is not None else "") + ".")
    if p.get("description"):
        lines.append(f"Description: {p['description']}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def schema_sections(data: dict) -> list[tuple[list[str], str]]:
    """One section per input key, e.g. (["ARC input", "job_memory"], "...")."""
    s = data["arc_input_schema"] if is_schema_file(data) else data
    commit = (s.get("arc_commit") or "unknown")[:12]
    foot = f"(ARC commit {commit}; generated from ARC's source by `rag-drg arc schema`.)"
    out: list[tuple[list[str], str]] = []
    arc = s.get("arc", {})
    required = set(arc.get("required") or [])
    jt = s.get("job_types") or {}
    for name, p in (arc.get("params") or {}).items():
        extra = []
        if name in required:
            extra.append("Required: yes (ARC raises an error if it is missing).")
        if name == "job_types" and jt:
            extra.append(f"Allowed keys: {', '.join(jt.get('keys', []))}. Default true: "
                         f"{', '.join(jt.get('defaults_true', []))}; default false: {', '.join(jt.get('defaults_false', []))}. "
                         f"Legacy aliases: {', '.join(f'{a} -> {b}' for a, b in (jt.get('legacy_aliases') or {}).items())}.")
        if name == "specific_job_type" and jt:
            extra.append(f"Values: one job type ({', '.join(jt.get('keys', []))}). It replaces job_types wholesale "
                         "(only that job type is true); `stability` therefore runs nothing.")
        if name == "ess_settings":
            extra.append(f"Allowed ESS keys: {', '.join(s.get('ess_settings_keys') or [])}. Values: a server name "
                         "or a list of server names defined in ARC's `servers` settings (e.g. 'local').")
        if name == "ts_adapters":
            extra.append(f"Default (settings.py): {s.get('ts_adapters_default')}. Registered adapters: "
                         f"{', '.join(s.get('job_adapters') or [])}.")
        if name in ("thermo_adapter", "kinetics_adapter") and s.get("statmech_adapters"):
            extra.append(f"Allowed (case-insensitive): {', '.join(s['statmech_adapters'])}.")
        if name.endswith("_level") or name in ("level_of_theory", "composite_method", "arkane_level_of_theory"):
            extra.append("A level: string 'method/basis' or a dict with keys "
                         f"{', '.join((s.get('level') or {}).get('dict_keys', []))} (see 'ARC input > level dict').")
        if name == "species":
            extra.append("Each entry is a dict; see 'ARC input > species > <key>'. Needs `label` and a structure "
                         "(smiles, inchi, adjlist, xyz or yml_path).")
        if name == "reactions":
            extra.append("Each entry is a dict; see 'ARC input > reactions > <key>'. Give `label: 'A + B <=> C + D'` "
                         "(spaces around <=> and +) or `reactants`/`products` lists of species labels.")
        out.append((["ARC input", name], _param_text("ARC input.yml top-level", name, p, "\n".join(extra)) + "\n" + foot))
    for scope, title in (("species", "species"), ("reaction", "reactions")):
        block = s.get(scope) or {}
        params = block.get("params") or {}
        read = set(block.get("dict_keys_read") or params)
        for name, p in params.items():
            if name in read:
                out.append((["ARC input", title, name],
                            _param_text(f"ARC input.yml `{title}` entry", name, p) + "\n" + foot))
        # Keys ARC only writes/reads for restarts, and arguments it ignores in input entries: one chunk,
        # so restart bookkeeping does not crowd out the keys people actually write.
        restart = [k for k in block.get("input_keys") or [] if k not in params]
        ignored = [k for k in params if k not in read]
        text = (f"Also accepted in `{title}` entries (restart.yml bookkeeping written by ARC; not needed in a "
                f"new input): {', '.join(restart) or 'none'}.\n")
        if ignored:
            text += (f"{block.get('class', scope)}() arguments that ARC does NOT read from input-file entries "
                     f"(no effect in input.yml): {', '.join(ignored)}.\n")
        out.append((["ARC input", title, "restart-only and ignored keys"], text + foot))
    level = s.get("level") or {}
    lp = level.get("params") or {}
    for name in level.get("dict_keys") or []:
        out.append((["ARC input", "level dict", name],
                    _param_text("ARC level-of-theory dict (opt_level, sp_level, ...)", name, lp.get(name) or {},
                                "Any key outside " + ", ".join(level.get("dict_keys") or []) + " makes ARC raise "
                                "'Got an illegal key'.") + "\n" + foot))
    out.append((["ARC input", "supported ESS and adapters"],
                f"supported_ess: {', '.join(s.get('supported_ess') or [])}\n"
                f"ess_settings keys: {', '.join(s.get('ess_settings_keys') or [])}\n"
                f"registered job adapters: {', '.join(s.get('job_adapters') or [])}\n"
                f"default ts_adapters: {s.get('ts_adapters_default')}\n"
                f"levels_ess (method/basis phrase -> ESS routing): {json.dumps(s.get('levels_ess') or {})}\n"
                f"default_levels_of_theory: {json.dumps(s.get('default_levels_of_theory') or {})}\n"
                f"default_job_settings: {json.dumps(s.get('default_job_settings') or {})}\n" + foot))
    return out
