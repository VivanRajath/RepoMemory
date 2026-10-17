"""Deterministic query parsing. No LLM: regexes + known graph vocabulary."""
import re

from graph import run

PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
JS_FRAME = re.compile(r"\(?((?:[A-Za-z]:)?[\w./\\@-]+\.(?:js|jsx|ts|tsx|mjs|cjs)):(\d+)(?::\d+)?\)?")
GENERIC_FRAME = re.compile(r"((?:[\w.-]+/)*[\w.-]+\.(?:py|js|jsx|ts|tsx)):(\d+)")
EXC = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b")
STATUS = re.compile(r"\b([1-5]\d\d)\b(?![./\d])")
ENDPOINT = re.compile(r"(/api(?:/[\w{}:.-]+)*)")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}")

STOPWORDS = set("""the and for are but not you all any can had her was one our out has him his how its may new now
old see two way who did get let put say she too use that with this from they will would there their what about
which when make like time just know take into year your some could them than then look only come over also back
after work first well even want because these give most why does doing done have been were could should might
cause causing caused happen happening happened anything something thing things here where tell show explain please
we us me my it is in on of to a an be do so if or as at by repo repository code codebase project contents content""".split())

INTENTS = {
    "decision": re.compile(r"\b(why|reason|decid\w*|chang\w*|chose|choice|design|switch\w*)\b", re.I),
    "bug": re.compile(r"\b(error|fail\w*|bug\w*|broken|break\w*|crash\w*|exception|[45]\d\d|regress\w*|wrong)\b", re.I),
    "dependency": re.compile(r"\b(depend\w*|version\w*|upgrad\w*|bump\w*|librar\w*|package\w*)\b", re.I),
}


def _known(project):
    files = [r["path"] for r in run("MATCH (f:File {project: $p}) RETURN f.path AS path", p=project)]
    deps = run("MATCH (d:Dependency {project: $p}) RETURN d.name AS name, d.import_name AS imp, d.uid AS uid", p=project)
    return files, deps


def _match_path(raw: str, files: list[str]) -> str | None:
    raw = raw.replace("\\", "/")
    best = None
    for f in files:
        if raw == f or raw.endswith("/" + f):
            if best is None or len(f) > len(best):
                best = f
    return best


def lucene_escape(term: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", term)


def parse(project: str, question: str, stack_trace: str = "") -> dict:
    text = f"{question}\n{stack_trace or ''}"
    files, deps = _known(project)

    frames = []
    for rx in (PY_FRAME, JS_FRAME, GENERIC_FRAME):
        for m in rx.finditer(text):
            p = _match_path(m[1], files)
            if p and (p, int(m[2])) not in frames:
                frames.append((p, int(m[2])))
    # bare paths mentioned without a line number
    for tok in re.findall(r"[\w./-]+\.(?:py|js|jsx|ts|tsx)\b", text):
        p = _match_path(tok, files)
        if p and p not in [f for f, _ in frames]:
            frames.append((p, 0))

    low = text.lower()
    packages = [d for d in deps if re.search(rf"\b{re.escape(d['name'].lower())}\b", low)
                or (d["imp"] and re.search(rf"\b{re.escape(d['imp'].lower())}\b", low))]

    endpoints = sorted(set(m.rstrip("/.,") for m in ENDPOINT.findall(text)))
    keywords = []
    for w in WORD.findall(question):  # keywords from the question only; stack traces are mostly noise
        lw = w.lower()
        if lw not in STOPWORDS and lw not in keywords and not INTENTS["decision"].fullmatch(lw):
            keywords.append(lw)

    intent = {k: bool(rx.search(text)) for k, rx in INTENTS.items()}
    return {
        "frames": [{"path": p, "line": ln} for p, ln in frames],
        "exceptions": sorted(set(EXC.findall(text))),
        "status_codes": sorted(set(STATUS.findall(text))),
        "endpoints": endpoints,
        "packages": [{"name": d["name"], "uid": d["uid"]} for d in packages],
        "keywords": keywords[:12],
        "fulltext": " OR ".join(lucene_escape(k) for k in keywords[:12]),
        "intent": intent,
    }
