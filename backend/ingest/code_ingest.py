"""Files, symbols, imports and components via tree-sitter (Python + JS/TS)."""
import json
import posixpath
import re
from pathlib import Path

from tree_sitter_language_pack import get_parser

from graph import run, uid, write_batch

CODE_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
            ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "venv", ".venv", "__pycache__", ".next",
             ".pytest_cache", ".mypy_cache", "coverage", ".tox"}
TEXT_EXT = {".md", ".txt", ".toml", ".json", ".yml", ".yaml", ".cfg", ".ini", ".html", ".css", ".sh"}
MAX_BYTES = 200_000
# Under these roots, components are second-level dirs (src/auth, app/orders) instead of top-level.
COMPONENT_ROOTS = {"src", "app", "lib", "packages", "apps", "services", "pkg"}
JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]

ROUTE_RE = re.compile(r"""@\w+\.(get|post|put|patch|delete)\(\s*["']([^"']*)["']""")
PREFIX_RE = re.compile(r"""APIRouter\([^)]*prefix\s*=\s*["']([^"']+)["']""")
JS_ROUTE_RE = re.compile(r"""\b(?:router|app)\.(get|post|put|patch|delete)\(\s*["'`]([^"'`]+)["'`]""")

_parsers = {}


def parser_for(lang):
    if lang not in _parsers:
        _parsers[lang] = get_parser(lang)
    return _parsers[lang]


def compute_roots(code_paths: list[str]) -> dict[str, str]:
    """Per top-level dir, the deepest single-chain directory all its code lives under.
    src/flask/app.py + src/flask/json/x.py -> {"src": "src/flask"}; components are then that root's children."""
    groups: dict[str, list[list[str]]] = {}
    for p in code_paths:
        parts = p.split("/")[:-1]
        if parts:
            groups.setdefault(parts[0], []).append(parts)
    roots = {}
    for top, dirs in groups.items():
        prefix = dirs[0]
        for d in dirs[1:]:
            i = 0
            while i < min(len(prefix), len(d)) and prefix[i] == d[i]:
                i += 1
            prefix = prefix[:i]
        if len(prefix) == 1 and top in COMPONENT_ROOTS:
            prefix = [top]  # app/auth, src/orders: children of the root are the components
        roots[top] = "/".join(prefix)
    return roots


def component_key(path: str, roots: dict[str, str] | None = None) -> str:
    parts = path.split("/")
    if len(parts) == 1:
        return ""
    if roots is None:  # fallback when a project's roots are unknown
        if parts[0] in COMPONENT_ROOTS and len(parts) > 2:
            return "/".join(parts[:2])
        return parts[0]
    root = roots.get(parts[0], parts[0])
    rest = path[len(root) + 1:].split("/") if path.startswith(root + "/") else []
    return f"{root}/{rest[0]}" if len(rest) > 1 else root


def load_roots(project: str) -> dict[str, str] | None:
    r = run("MATCH (p:Project {uid: $u}) RETURN p.component_roots AS r", u=uid("project", project, project))
    return json.loads(r[0]["r"]) if r and r[0]["r"] else None


def list_files(repo_dir: Path) -> list[str]:
    out = []
    for p in repo_dir.rglob("*"):
        rel = p.relative_to(repo_dir)
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        if p.suffix not in CODE_EXT and p.suffix not in TEXT_EXT and p.name not in ("Dockerfile", "Makefile"):
            continue
        if p.stat().st_size > MAX_BYTES:
            continue
        out.append(rel.as_posix())
    return sorted(out)


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _txt(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


# ---------- import resolution ----------

def _resolve_py(path: str, module: str, names: list[str], files: set[str]):
    """Returns (resolved_file_paths, external_top_module_or_None)."""
    dots = len(module) - len(module.lstrip("."))
    mod = module.lstrip(".")
    if dots:
        base = posixpath.dirname(path)
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        bases = [base]
    else:
        bases = ["", "src"]
    found = []
    for b in bases:
        stem = posixpath.join(b, *mod.split(".")) if mod else b
        # `from pkg import submodule` -> pkg/submodule.py
        for n in names:
            for cand in (f"{stem}/{n}.py", f"{stem}/{n}/__init__.py"):
                cand = cand.lstrip("/")
                if cand in files:
                    found.append(cand)
        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
            cand = cand.lstrip("/")
            if mod and cand in files and not found:
                found.append(cand)
        if found:
            return found, None
    if dots:
        return [], None
    return [], mod.split(".")[0] or None


def _resolve_js(path: str, spec: str, files: set[str]):
    if not spec.startswith("."):
        parts = spec.split("/")
        return [], ("/".join(parts[:2]) if spec.startswith("@") else parts[0])
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    cands = [target] + [target + e for e in JS_EXTS] + [f"{target}/index{e}" for e in JS_EXTS]
    for c in cands:
        if c in files:
            return [c], None
    return [], None


# ---------- per-file parse ----------

def parse_file(repo_dir: Path, path: str, files: set[str]) -> dict:
    lang = CODE_EXT.get(Path(path).suffix)
    src = (repo_dir / path).read_bytes()
    rec = {"path": path, "language": lang, "chars": len(src), "symbols": [], "imports": set(), "external": set()}
    if not lang:
        return rec
    tree = parser_for(lang).parse(src)
    text = src.decode("utf8", "replace")

    if lang == "python":
        for n in _walk(tree.root_node):
            if n.type in ("function_definition", "class_definition"):
                kind = "class" if n.type == "class_definition" else "function"
                rec["symbols"].append((_txt(n.child_by_field_name("name")), kind, n.start_point[0] + 1))
            elif n.type == "import_statement":
                for c in n.children_by_field_name("name"):
                    mod = _txt(c.child_by_field_name("name") if c.type == "aliased_import" else c)
                    found, ext = _resolve_py(path, mod, [], files)
                    rec["imports"].update(found)
                    if ext:
                        rec["external"].add(ext)
            elif n.type == "import_from_statement":
                mod = _txt(n.child_by_field_name("module_name"))
                names = [_txt(c.child_by_field_name("name") if c.type == "aliased_import" else c)
                         for c in n.children_by_field_name("name")]
                found, ext = _resolve_py(path, mod, names, files)
                rec["imports"].update(found)
                if ext:
                    rec["external"].add(ext)
        prefix = (PREFIX_RE.search(text) or [None, ""])[1]
        for m in ROUTE_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            rec["symbols"].append((f"{m[1].upper()} {prefix}{m[2]}", "route", line))
    else:
        for n in _walk(tree.root_node):
            t = n.type
            if t in ("function_declaration", "generator_function_declaration", "class_declaration",
                     "interface_declaration", "method_definition"):
                kind = {"class_declaration": "class", "interface_declaration": "interface",
                        "method_definition": "method"}.get(t, "function")
                rec["symbols"].append((_txt(n.child_by_field_name("name")), kind, n.start_point[0] + 1))
            elif t == "variable_declarator":
                v = n.child_by_field_name("value")
                if v is not None and v.type in ("arrow_function", "function_expression", "function"):
                    rec["symbols"].append((_txt(n.child_by_field_name("name")), "function", n.start_point[0] + 1))
            elif t in ("import_statement", "export_statement") and n.child_by_field_name("source") is not None:
                spec = _txt(n.child_by_field_name("source")).strip("'\"`")
                found, ext = _resolve_js(path, spec, files)
                rec["imports"].update(found)
                if ext:
                    rec["external"].add(ext)
            elif t == "call_expression" and _txt(n.child_by_field_name("function")) == "require":
                args = n.child_by_field_name("arguments")
                if args is not None and args.named_child_count and args.named_children[0].type == "string":
                    spec = _txt(args.named_children[0]).strip("'\"`")
                    found, ext = _resolve_js(path, spec, files)
                    rec["imports"].update(found)
                    if ext:
                        rec["external"].add(ext)
        for m in JS_ROUTE_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            rec["symbols"].append((f"{m[1].upper()} {m[2]}", "route", line))
    rec["imports"].discard(path)
    return rec


# ---------- writes ----------

def write_files(project: str, parsed: list[dict], roots: dict[str, str] | None = None):
    """Upsert File/Symbol/IMPORTS/Component for the given parsed files. Replaces their old symbols+imports."""
    pid = uid("project", project, project)
    roots = roots if roots is not None else load_roots(project)
    comps, file_rows, sym_rows, imp_rows = {}, [], [], []
    for r in parsed:
        ck = component_key(r["path"], roots) if r["language"] else None
        fid = uid("file", project, r["path"])
        if ck is not None:
            comps[ck] = {"uid": uid("comp", project, ck), "name": ck.split("/")[-1] if ck else "(root)", "path": ck}
        file_rows.append({"uid": fid, "path": r["path"], "language": r["language"], "chars": r["chars"],
                          "name": posixpath.basename(r["path"]), "ext": sorted(r["external"]),
                          "comp": comps[ck]["uid"] if ck is not None else None})
        for name, kind, line in r["symbols"]:
            sym_rows.append({"uid": uid("sym", project, f"{r['path']}:{name}:{line}"), "file": fid,
                             "name": name, "kind": kind, "file_path": r["path"], "line": line})
        for target in r["imports"]:
            imp_rows.append({"src": fid, "dst": uid("file", project, target), "dst_path": target})

    write_batch("""UNWIND $rows AS row
        MERGE (c:Component {uid: row.uid}) SET c.name = row.name, c.path = row.path, c.project = $p
        WITH c MATCH (pr:Project {uid: $pid}) MERGE (pr)-[:HAS_COMPONENT]->(c)""",
                list(comps.values()), p=project, pid=pid)
    write_batch("""UNWIND $rows AS row
        MERGE (f:File {uid: row.uid})
        SET f.path = row.path, f.name = row.name, f.language = row.language, f.chars = row.chars,
            f.ext_imports = row.ext, f.project = $p, f.deleted = false
        WITH f, row
        OPTIONAL MATCH (f)-[old:IMPORTS]->() DELETE old
        WITH DISTINCT f, row
        OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol) DETACH DELETE s
        WITH DISTINCT f, row WHERE row.comp IS NOT NULL
        MATCH (c:Component {uid: row.comp}) MERGE (c)-[:CONTAINS]->(f)""", file_rows, p=project)
    write_batch("""UNWIND $rows AS row
        MATCH (f:File {uid: row.file})
        MERGE (s:Symbol {uid: row.uid})
        SET s.name = row.name, s.kind = row.kind, s.file_path = row.file_path, s.line = row.line, s.project = $p
        MERGE (f)-[:DEFINES]->(s)""", sym_rows, p=project)
    write_batch("""UNWIND $rows AS row
        MATCH (a:File {uid: row.src})
        MERGE (b:File {uid: row.dst}) ON CREATE SET b.path = row.dst_path, b.project = $p
        MERGE (a)-[:IMPORTS]->(b)""", imp_rows, p=project)


def derive_component_deps(project: str):
    run("""MATCH (c:Component {project: $p})-[r:DEPENDS_ON]->() DELETE r""", p=project)
    run("""MATCH (c1:Component {project: $p})-[:CONTAINS]->(:File)-[:IMPORTS]->(:File)<-[:CONTAINS]-(c2:Component)
           WHERE c1 <> c2
           WITH c1, c2, count(*) AS n
           MERGE (c1)-[r:DEPENDS_ON]->(c2) SET r.count = n""", p=project)


def ingest_code(project: str, url: str, repo_dir: Path, log=print) -> dict:
    run("MERGE (p:Project {uid: $uid}) SET p.name = $name, p.url = $url, p.project = $name",
        uid=uid("project", project, project), name=project, url=url)
    paths = list_files(repo_dir)
    files = set(paths)
    log(f"code: parsing {len(paths)} files")
    parsed = []
    for i, p in enumerate(paths):
        try:
            parsed.append(parse_file(repo_dir, p, files))
        except Exception as e:  # one bad file shouldn't kill ingestion
            log(f"code: skip {p}: {e}")
        if i and i % 200 == 0:
            log(f"code: parsed {i}/{len(paths)}")
    roots = compute_roots([r["path"] for r in parsed if r["language"]])
    run("MATCH (p:Project {uid: $u}) SET p.component_roots = $r", u=uid("project", project, project), r=json.dumps(roots))
    write_files(project, parsed, roots)
    derive_component_deps(project)
    source_chars = sum(r["chars"] for r in parsed if r["language"])
    run("MATCH (p:Project {uid: $uid}) SET p.source_chars = $c", uid=uid("project", project, project), c=source_chars)
    n_sym = sum(len(r["symbols"]) for r in parsed)
    n_comp = len({component_key(r["path"], roots) for r in parsed if r["language"]})
    log(f"code: {len(parsed)} files, {n_sym} symbols, {n_comp} components")
    return {"files": len(parsed), "symbols": n_sym, "components": n_comp, "source_chars": source_chars}
