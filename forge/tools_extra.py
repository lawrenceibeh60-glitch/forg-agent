"""60+ extra tools. Same @tool decorator; auto-registered. No extra dependencies."""
import os, re, json, ast, math, hashlib, base64, uuid, difflib, sqlite3, socket, subprocess, zipfile, shutil, operator, urllib.parse as up
from datetime import datetime, timezone, timedelta
import httpx
from tools import tool, safe, WS, sh, gh

SKIP = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
def T(s, n=6000): s = str(s); return s if len(s) <= n else s[:n] + "\n…[truncated]"
def files(g="*"): return [p for p in WS.rglob(g) if p.is_file() and not SKIP & set(p.relative_to(WS).parts)]
def rel(p): return str(p.relative_to(WS))
def run(cmd, cwd=".", t=600):
    try: return sh(cmd, safe(cwd), t)
    except subprocess.TimeoutExpired: return f"timed out after {t}s"
def get(url, **k): return httpx.get(url, timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Forge"}, **k)
strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
O = ("string", "")


@tool("project_map", "Build a bounded architecture map of the active workspace: entry points, manifests, tests, important directories, source files, and Python symbols. Use before substantial edits or when unfamiliar with a project.", {"path?": ("string", "workspace subdirectory, default root"), "max_files?": ("integer", "maximum files to inspect, default 180")})
def project_map(path=".", max_files=180):
    base = safe(path)
    if not base.is_dir():
        return "Not a directory: " + path
    limit = max(20, min(int(max_files), 500))
    entries, manifests, tests, symbols, dirs = [], [], [], [], set()
    count = 0
    for p in sorted(base.rglob("*")):
        try:
            relpath = p.relative_to(base)
            if p.is_symlink() or SKIP & set(relpath.parts) or any(x.startswith(".") and x not in {".github"} for x in relpath.parts):
                continue
            if p.is_dir():
                if len(relpath.parts) <= 2:
                    dirs.add(relpath.as_posix() + "/")
                continue
            if not p.is_file() or p.name in {".env", ".netrc", "credentials.json", "secrets.json"} or p.name.startswith(".env.") or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
                continue
            count += 1
            name = relpath.as_posix()
            entries.append(name)
            if p.name in {"README.md", "pyproject.toml", "package.json", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml", "Gemfile", "Makefile", "render.yaml", "Dockerfile"}:
                manifests.append(name)
            if re.search(r"(^|/)(test|tests|spec|specs)(/|_|\.|$)|(^|/)(test_|.*_test\.py$)", name, re.I):
                tests.append(name)
            if p.suffix == ".py" and len(symbols) < 240:
                try:
                    tree_ast = ast.parse(p.read_text(encoding="utf-8", errors="replace")[:250000])
                    for node in ast.walk(tree_ast):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            symbols.append(f"{name}:{node.lineno} {type(node).__name__.replace('Def','')} {node.name}")
                except (SyntaxError, OSError, ValueError):
                    pass
            if count >= limit:
                break
        except OSError:
            continue
    return json.dumps({
        "root": str(base), "files_seen": len(entries), "truncated": count >= limit,
        "directories": sorted(dirs)[:80], "manifests_and_entry_docs": manifests[:40],
        "test_files": tests[:80], "python_symbols": symbols[:180],
        "file_tree": entries[:limit],
        "next_action": "Read the entry point and the directly related implementation/tests before editing; this is a map, not a substitute for source inspection."
    }, ensure_ascii=False, indent=2)[:16000]


@tool("find_symbol", "Find definitions of a named function, class, method, or exported symbol across the workspace. Use for navigation and impact analysis before editing.", {"name": ("string", "symbol name"), "path?": ("string", "optional glob, e.g. **/*.py")})
def find_symbol(name, path="**/*"):
    target = str(name).strip()
    if not target or len(target) > 180:
        return "Provide a symbol name of 1-180 characters."
    out = []
    rx = re.compile(r"^\s*(?:async\s+)?(?:def|class|function|interface|type|enum|struct|trait|func|fn|export\s+(?:default\s+)?)\s+" + re.escape(target) + r"\b")
    for p in WS.glob(path):
        if not p.is_file() or SKIP & set(p.relative_to(WS).parts) or p.is_symlink():
            continue
        try:
            if p.stat().st_size > 800000:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if p.suffix == ".py":
            try:
                tree_ast = ast.parse("\n".join(lines))
                for node in ast.walk(tree_ast):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == target:
                        out.append(f"{rel(p)}:{node.lineno}: {type(node).__name__.replace('Def','')} {target}")
            except SyntaxError:
                pass
        else:
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    out.append(f"{rel(p)}:{i}: {line.strip()[:240]}")
        if len(out) >= 100:
            break
    return "\n".join(out) if out else f"No definition for {target!r} found. Broaden the glob or use search_code."


@tool("find_references", "Find usages/imports/call sites of a symbol or literal across source files, excluding generated/vendor directories. Use before renaming or changing an API.", {"name": ("string", "symbol or literal text"), "glob?": ("string", "file glob, default source files")})
def find_references(name, glob="*"):
    target = str(name).strip()
    if not target or len(target) > 180:
        return "Provide a search term of 1-180 characters."
    rx = re.compile(r"\b" + re.escape(target) + r"\b")
    allowed = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".go", ".rs", ".java", ".c", ".h", ".sql", ".yaml", ".yml"}
    out = []
    for p in WS.rglob(glob):
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in allowed or SKIP & set(p.relative_to(WS).parts):
            continue
        if p.name in {".env", ".netrc", "credentials.json", "secrets.json"} or p.name.startswith(".env.") or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            continue
        try:
            if p.stat().st_size > 800000:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel(p)}:{i}: {line.strip()[:220]}")
                    if len(out) >= 160:
                        return "\n".join(out) + "\n[results truncated]"
        except OSError:
            continue
    return "\n".join(out) if out else f"No references found for {target!r}."


@tool("dependency_graph", "Inspect local Python and JavaScript/TypeScript imports and report likely internal module dependencies. Heuristic analysis only; use to trace impact before refactoring.", {"path?": ("string", "workspace subdirectory, default root"), "max_files?": ("integer", "maximum source files, default 160")})
def dependency_graph(path=".", max_files=160):
    base = safe(path)
    if not base.is_dir():
        return "Not a directory: " + path
    limit = max(20, min(int(max_files), 400))
    source_exts = {".py", ".js", ".ts", ".tsx", ".jsx"}
    sources = []
    for p in base.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in source_exts or SKIP & set(p.relative_to(base).parts):
            continue
        sources.append(p)
        if len(sources) >= limit:
            break
    known = {}
    for p in sources:
        relpath = p.relative_to(base).as_posix()
        stem = p.with_suffix("").name
        known.setdefault(stem, relpath)
        known.setdefault(p.relative_to(base).with_suffix("").as_posix().replace("/", "."), relpath)
    edges, unresolved = [], []
    for p in sources:
        relpath = p.relative_to(base).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="replace")[:350000]
        except OSError:
            continue
        imports = []
        if p.suffix == ".py":
            try:
                tree_ast = ast.parse(content)
                for node in ast.walk(tree_ast):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.append(node.module)
            except SyntaxError:
                imports.extend(re.findall(r"(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", content))
                imports = [x for pair in imports for x in pair if x]
        else:
            imports.extend(re.findall(r"""(?:from\s+|import\s*)['"]([^'"]+)['"]""", content))
        for mod in sorted(set(imports)):
            if mod.startswith((".", "/")):
                candidate = mod.lstrip("./").split("/")[0].split(".")[0]
                resolved = known.get(candidate) or known.get(mod.replace("/", "."))
                if resolved:
                    edges.append({"from": relpath, "import": mod, "to": resolved})
                else:
                    unresolved.append({"from": relpath, "import": mod})
            elif mod.split(".")[0] in known:
                edges.append({"from": relpath, "import": mod, "to": known[mod.split(".")[0]]})
    return json.dumps({"files_analyzed": len(sources), "truncated": len(sources) >= limit,
                       "internal_edges": edges[:300], "unresolved_relative_imports": unresolved[:80],
                       "note": "Best-effort static heuristic; dynamic imports, aliases, package resolution and generated code may be missed."},
                      ensure_ascii=False, indent=2)[:16000]

@tool("tool_catalog", "Discover available Forge tools by capability when the current tool selection does not expose what you need. Returns matching tool names and descriptions; use the exact returned name in the next tool call.", {"query": ("string", "capability or task to search for"), "limit?": ("integer", "maximum matches, default 20")})
def tool_catalog(query, limit=20):
    import tools as registry
    terms = [x for x in re.findall(r"[a-zA-Z0-9_]{2,}", query.lower()) if x not in {"the", "and", "for", "with", "from", "tool", "tools"}]
    matches = []
    for schema in registry.SCHEMAS:
        fn = schema.get("function", {})
        name, desc = fn.get("name", ""), fn.get("description", "")
        haystack = (name + " " + desc).lower()
        score = sum(4 if term in name.lower() else 1 for term in terms if term in haystack)
        if score:
            matches.append((score, name, desc))
    matches.sort(key=lambda item: (-item[0], item[1]))
    maximum = max(1, min(int(limit), 40))
    return json.dumps([{"name": n, "description": d[:300]} for _, n, d in matches[:maximum]], ensure_ascii=False, indent=2) if matches else "No matching tools. Try a broader capability query."

# ================= files =================
@tool("glob_files", "Find files by glob pattern, e.g. **/*.py", {"pattern": ("string", "glob")})
def glob_files(pattern): return "\n".join(sorted(rel(p) for p in WS.glob(pattern) if p.is_file())[:300]) or "none"

@tool("tree", "Directory tree view.", {"path?": ("string", "dir"), "depth?": ("integer", "max depth, default 2")})
def tree(path=".", depth=2):
    out, base = [], safe(path)
    def w(d, lvl):
        for p in sorted(d.iterdir()):
            if p.name in SKIP: continue
            out.append("  " * lvl + p.name + ("/" if p.is_dir() else ""))
            if p.is_dir() and lvl < depth - 1: w(p, lvl + 1)
    w(base, 0); return "\n".join(out[:400]) or "(empty)"

@tool("read_many", "Read several files at once (comma-separated paths).", {"paths": ("string", "a.py,b.py")})
def read_many(paths): return "\n\n".join(f"=== {p} ===\n{T(safe(p.strip()).read_text(errors='replace'), 4000)}" for p in paths.split(",")[:8])

@tool("append_file", "Append text to a file.", {"path": ("string", ""), "content": ("string", "")})
def append_file(path, content):
    with safe(path).open("a") as f: f.write(content)
    return "appended"

@tool("delete_file", "Delete a file or folder.", {"path": ("string", "")})
def delete_file(path):
    p = safe(path); shutil.rmtree(p) if p.is_dir() else p.unlink(); return "deleted"

@tool("move_file", "Move or rename a file/folder.", {"src": ("string", ""), "dst": ("string", "")})
def move_file(src, dst): d = safe(dst); d.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(safe(src)), str(d)); return "moved"

@tool("make_dir", "Create a directory.", {"path": ("string", "")})
def make_dir(path): safe(path).mkdir(parents=True, exist_ok=True); return "created"

@tool("apply_patch", "Apply a unified diff to the workspace (git apply).", {"patch": ("string", "unified diff"), "cwd?": ("string", "dir")})
def apply_patch(patch, cwd="."):
    r = subprocess.run(["git", "apply", "--whitespace=fix", "-"], input=patch, cwd=safe(cwd), capture_output=True, text=True)
    return (r.stderr or "patch applied") + f"[exit {r.returncode}]"

@tool("bulk_replace", "Regex find/replace across files. Defaults to a dry run.", {"pattern": ("string", "regex"), "replacement": ("string", ""), "glob?": ("string", "*.py"), "dry_run?": ("boolean", "default true")})
def bulk_replace(pattern, replacement, glob="*", dry_run=True):
    rx, out = re.compile(pattern), []
    for p in files(glob):
        try: t = p.read_text()
        except Exception: continue
        n = len(rx.findall(t))
        if n:
            out.append(f"{rel(p)}: {n}")
            if not dry_run: p.write_text(rx.sub(replacement, t))
    return ("DRY RUN\n" if dry_run else "APPLIED\n") + ("\n".join(out) or "no matches")

@tool("code_stats", "Lines of code per file type.", {})
def code_stats():
    c = {}
    for p in files():
        try: c[p.suffix or "(none)"] = c.get(p.suffix or "(none)", 0) + len(p.read_text().splitlines())
        except Exception: pass
    return "\n".join(f"{k}: {v}" for k, v in sorted(c.items(), key=lambda x: -x[1])[:20])

@tool("find_todos", "List TODO/FIXME/HACK comments.", {})
def find_todos():
    out = []
    for p in files():
        try:
            for i, l in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"\b(TODO|FIXME|HACK|XXX)\b", l): out.append(f"{rel(p)}:{i}: {l.strip()[:120]}")
        except Exception: pass
    return "\n".join(out[:100]) or "none"

@tool("outline", "List functions/classes in a source file.", {"path": ("string", "")})
def outline(path):
    t = safe(path).read_text()
    if path.endswith(".py"):
        return "\n".join(f"{n.lineno}: {type(n).__name__} {n.name}" for n in ast.walk(ast.parse(t)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return "\n".join(f"{i}: {l.strip()[:100]}" for i, l in enumerate(t.splitlines(), 1) if re.match(r"\s*(export\s+)?(default\s+)?(async\s+)?(function|class|interface|type|def|func|fn|struct)\b", l))

@tool("diff_files", "Unified diff between two files.", {"a": ("string", ""), "b": ("string", "")})
def diff_files(a, b): return T("".join(difflib.unified_diff(safe(a).read_text().splitlines(1), safe(b).read_text().splitlines(1), a, b)) or "identical")

@tool("json_get", "Read a value from a JSON file by dotted path (a.b.0.c).", {"path": ("string", ""), "key_path?": ("string", "")})
def json_get(path, key_path=""):
    d = json.loads(safe(path).read_text())
    for k in filter(None, key_path.split(".")): d = d[int(k)] if isinstance(d, list) else d[k]
    return T(json.dumps(d, indent=1))

@tool("archive", "Zip a file or folder.", {"path": ("string", ""), "out": ("string", "zip path")})
def archive(path, out): shutil.make_archive(str(safe(out)).removesuffix(".zip"), "zip", safe(path)); return "created " + out

@tool("extract", "Unzip an archive.", {"path": ("string", ""), "out?": ("string", "dir")})
def extract(path, out="."): zipfile.ZipFile(safe(path)).extractall(safe(out)); return "extracted"

@tool("csv_preview", "Preview the first rows of a CSV.", {"path": ("string", ""), "rows?": ("integer", "")})
def csv_preview(path, rows=10): return "\n".join(safe(path).read_text(errors="replace").splitlines()[:rows])

@tool("env_keys", "List variable NAMES in a .env file (never values).", {"path?": ("string", "")})
def env_keys(path=".env"): return "\n".join(l.split("=")[0] for l in safe(path).read_text().splitlines() if "=" in l and not l.startswith("#"))

@tool("file_hash", "Hash a file.", {"path": ("string", ""), "algo?": ("string", "sha256|md5|sha1")})
def file_hash(path, algo="sha256"): return hashlib.new(algo, safe(path).read_bytes()).hexdigest()

# ================= dev & runtime =================
@tool("detect_stack", "Detect languages, package managers and the right test/run commands.", {"cwd?": ("string", "")})
def detect_stack(cwd="."):
    b, m = safe(cwd), {"package.json": "Node (npm test / npm run build)", "requirements.txt": "Python (pip, pytest)", "pyproject.toml": "Python project", "go.mod": "Go (go test ./...)", "Cargo.toml": "Rust (cargo test)", "pom.xml": "Java/Maven", "Gemfile": "Ruby", "Dockerfile": "Docker", "Makefile": "make targets"}
    return "\n".join(v for k, v in m.items() if (b / k).exists()) or "unknown"

@tool("run_tests", "Auto-detect and run the project's test suite.", {"cwd?": ("string", "")})
def run_tests(cwd="."):
    b = safe(cwd)
    if (b / "package.json").exists() and '"test"' in (b / "package.json").read_text(): c = "npm test --silent"
    elif (b / "go.mod").exists(): c = "go test ./..."
    elif (b / "Cargo.toml").exists(): c = "cargo test"
    else: c = "python -m pytest -x -q"
    return f"$ {c}\n" + run(c, cwd)

@tool("lint", "Run the linter (ruff for Python, eslint for JS).", {"cwd?": ("string", "")})
def lint(cwd="."):
    return run("ruff check ." if any(safe(cwd).glob("*.py")) or (safe(cwd) / "pyproject.toml").exists() else "npx --no-install eslint .", cwd, 180)

@tool("python_eval", "Run a Python snippet for quick computation/experiments in the configured isolated sandbox.", {"code": ("string", "")})
def python_eval(code): return run("python3 -c " + __import__("shlex").quote(code), ".", 60)

@tool("ipython_eval", "Run a Python snippet in IPython inside the persistent Vercel Linux sandbox, installing IPython on demand when sandbox networking is enabled; otherwise clearly fall back to standard Python.", {"code": ("string", "Python snippet")})
def ipython_eval(code):
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    python_payload = "import base64; exec(compile(base64.b64decode('" + encoded + "'), '<forge-ipython>', 'exec'))"
    quote = __import__("shlex").quote
    network_enabled = os.getenv("SANDBOX_NETWORK", "0") == "1"
    install = ("if ! command -v ipython >/dev/null 2>&1; then "
               "python3 -m pip install --user --disable-pip-version-check ipython 2>&1 | tail -8; fi; "
               if network_enabled else "")
    command = (
        install
        + "if command -v ipython >/dev/null 2>&1; then "
        + "ipython -c " + quote(python_payload) + "; "
        + "elif python3 -c 'import IPython' >/dev/null 2>&1; then "
        + "python3 -m IPython -c " + quote(python_payload) + "; "
        + "else printf '%s\\n' 'IPython is not installed. Enable SANDBOX_NETWORK=1 to let the isolated sandbox install it.'; "
        + "python3 -c " + quote(python_payload) + "; fi"
    )
    return run(command, ".", 180)

@tool("linux_package_install", "Install Linux system packages inside the isolated Vercel Sandbox when its image supports apt, dnf, yum, or apk. Package names are validated; this never runs on the Forge host.", {"packages": ("string", "space-separated Linux package names")})
def linux_package_install(packages):
    names = str(packages or "").split()
    if not names or len(names) > 30 or any(not re.fullmatch(r"[A-Za-z0-9.+:_-]{1,80}", n) for n in names):
        return "Invalid package list. Use up to 30 simple package names separated by spaces."
    args = " ".join(shlex.quote(n) for n in names)
    command = (
        "set -e; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "if [ \"$(id -u)\" -eq 0 ]; then apt-get update && apt-get install -y " + args + "; "
        "elif command -v sudo >/dev/null 2>&1; then sudo -n apt-get update && sudo -n apt-get install -y " + args + "; "
        "else echo 'apt-get exists but this sandbox user has no root/sudo access'; exit 126; fi; "
        "elif command -v dnf >/dev/null 2>&1; then dnf install -y " + args + "; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y " + args + "; "
        "elif command -v apk >/dev/null 2>&1; then apk add " + args + "; "
        "else echo 'No supported Linux package manager (apt-get, dnf, yum, apk) found in this sandbox image'; exit 127; fi"
    )
    return run(command, ".", 300)

@tool("sandbox_runtime_info", "Inspect Linux distribution, Python, Node, package managers, IPython, and sandbox network configuration.", {})
def sandbox_runtime_info():
    command = "echo '--- Linux distribution ---'; cat /etc/os-release 2>/dev/null | head -8; echo '--- runtimes ---'; python3 --version 2>&1; node --version 2>&1 || true; npm --version 2>&1 || true; git --version 2>&1 || true; echo '--- IPython ---'; if command -v ipython >/dev/null 2>&1; then ipython --version; elif python3 -c 'import IPython' >/dev/null 2>&1; then python3 -c 'import IPython; print(IPython.__version__)'; else echo 'IPython: not installed'; fi; echo '--- package managers ---'; command -v pip || true; command -v uv || true; command -v apt-get || true; echo 'network policy is controlled by SANDBOX_NETWORK on Forge host'"
    return run(command, ".", 30)

@tool("node_eval", "Run a JavaScript snippet with Node inside the configured isolated sandbox; refuses host execution by default.", {"code": ("string", "")})
def node_eval(code): return run(f"node -e {json.dumps(code)}", ".", 60)

@tool("pip_install", "pip install packages.", {"packages": ("string", "space-separated")})
def pip_install(packages): return run(f"pip install {packages} --break-system-packages 2>&1 | tail -5")

@tool("npm_install", "npm install packages (or all deps).", {"packages?": ("string", ""), "cwd?": ("string", "")})
def npm_install(packages="", cwd="."): return run(f"npm install {packages} 2>&1 | tail -8", cwd)

@tool("dependency_list", "List declared dependencies.", {"cwd?": ("string", "")})
def dependency_list(cwd="."):
    b, out = safe(cwd), []
    if (b / "package.json").exists(): d = json.loads((b / "package.json").read_text()); out += [f"{k}@{v}" for s in ("dependencies", "devDependencies") for k, v in d.get(s, {}).items()]
    if (b / "requirements.txt").exists(): out += (b / "requirements.txt").read_text().split()
    return "\n".join(out) or "none"

@tool("http_request", "Make an HTTP request (test APIs, webhooks).", {"method": ("string", "GET/POST/..."), "url": ("string", ""), "headers?": ("string", "JSON object"), "body?": ("string", "")})
def http_request(method, url, headers="{}", body=""):
    r = httpx.request(method, url, headers=json.loads(headers or "{}"), content=body or None, timeout=30, follow_redirects=True)
    return f"{r.status_code} {r.reason_phrase}\n{dict(list(r.headers.items())[:8])}\n\n{T(r.text, 4000)}"

@tool("check_url", "Check if a URL is up: status and latency.", {"url": ("string", "")})
def check_url(url): r = get(url); return f"{r.status_code} in {r.elapsed.total_seconds():.2f}s"

@tool("port_check", "Check whether a TCP port is open.", {"host": ("string", ""), "port": ("integer", "")})
def port_check(host, port):
    try: socket.create_connection((host, port), 3).close(); return "open"
    except Exception as e: return f"closed ({e})"

@tool("dns_lookup", "Resolve a hostname.", {"host": ("string", "")})
def dns_lookup(host): return "\n".join(sorted({a[4][0] for a in socket.getaddrinfo(host, None)}))

@tool("sql_query", "Run a read-only SQL query against a SQLite file in the workspace.", {"db_path": ("string", ""), "query": ("string", "")})
def sql_query(db_path, query):
    c = sqlite3.connect(safe(db_path)); c.execute("PRAGMA query_only=ON"); cur = c.execute(query)
    return "\t".join(d[0] for d in cur.description or []) + "\n" + "\n".join("\t".join(map(str, r)) for r in cur.fetchmany(50))

@tool("docker_ps", "List running Docker containers.", {})
def docker_ps(): return run("docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'", ".", 20)

@tool("process_list", "Top processes by CPU.", {})
def process_list(): return run("ps aux --sort=-%cpu | head -12", ".", 10)

@tool("system_info", "OS, CPU, memory, disk and tool versions.", {})
def system_info(): return run("uname -a; python3 --version; node --version 2>&1; git --version; df -h . | tail -1; free -h 2>/dev/null | head -2", ".", 10)

# ================= git & GitHub =================
@tool("git_log", "Recent commits.", {"repo_dir": ("string", ""), "n?": ("integer", "")})
def git_log(repo_dir, n=15): return run(f"git log --oneline --decorate -n {int(n)}", repo_dir, 30)

@tool("git_branches", "List branches.", {"repo_dir": ("string", "")})
def git_branches(repo_dir): return run("git branch -a -vv", repo_dir, 30)

@tool("git_show", "Show a commit.", {"repo_dir": ("string", ""), "ref?": ("string", "default HEAD")})
def git_show(repo_dir, ref="HEAD"): return T(run(f"git show --stat -p {ref} | head -200", repo_dir, 30))

@tool("git_blame", "Who last changed a line.", {"repo_dir": ("string", ""), "path": ("string", ""), "line": ("integer", "")})
def git_blame(repo_dir, path, line): return run(f"git blame -L {int(line)},{int(line)} -- {path}", repo_dir, 30)

@tool("git_changes", "Working tree status plus diff summary.", {"repo_dir": ("string", "")})
def git_changes(repo_dir): return run("git status -sb; git diff --stat", repo_dir, 30)

@tool("github_get_file", "Read a file from a GitHub repo without cloning.", {"repo": ("string", "owner/name"), "path": ("string", ""), "ref?": ("string", "")})
def github_get_file(repo, path, ref=""):
    d = gh("GET", f"/repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else ""))
    return T(base64.b64decode(d["content"]).decode(errors="replace")) if "content" in d else json.dumps(d)

@tool("github_search_code", "Search code across GitHub.", {"query": ("string", "")})
def github_search_code(query): return "\n".join(f"{i['repository']['full_name']}: {i['path']}" for i in gh("GET", f"/search/code?q={up.quote(query)}&per_page=15").get("items", []))

@tool("github_prs", "List open pull requests.", {"repo": ("string", "")})
def github_prs(repo): return "\n".join(f"#{p['number']} {p['title']} ({p['user']['login']})" for p in gh("GET", f"/repos/{repo}/pulls?state=open&per_page=20"))

@tool("github_pr_comment", "Comment on a PR or issue.", {"repo": ("string", ""), "number": ("integer", ""), "body": ("string", "")})
def github_pr_comment(repo, number, body): return json.dumps(gh("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}))[:200]

@tool("github_create_issue", "Open an issue.", {"repo": ("string", ""), "title": ("string", ""), "body?": ("string", "")})
def github_create_issue(repo, title, body=""): return json.dumps(gh("POST", f"/repos/{repo}/issues", json={"title": title, "body": body}))[:300]

@tool("github_workflow_runs", "Recent CI runs and their status.", {"repo": ("string", "")})
def github_workflow_runs(repo): return "\n".join(f"{r['name']}: {r['status']}/{r['conclusion']} {r['head_branch']}" for r in gh("GET", f"/repos/{repo}/actions/runs?per_page=8").get("workflow_runs", []))

@tool("github_repo_info", "Repo overview: stars, language, default branch.", {"repo": ("string", "")})
def github_repo_info(repo): d = gh("GET", f"/repos/{repo}"); return "\n".join(f"{k}: {d.get(k)}" for k in ("description", "language", "default_branch", "stargazers_count", "open_issues_count", "pushed_at"))

# ================= web & research =================
@tool("web_search", "Search the web (DuckDuckGo).", {"query": ("string", "")})
def web_search(query):
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    items = re.findall(r'class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)[:8]
    return "\n\n".join(f"{strip(t)}\n{up.unquote(m.group(1)) if (m := re.search(r'uddg=([^&]+)', u)) else u}\n{strip(s)}" for u, t, s in items) or "no results"

@tool("extract_links", "List the links on a web page.", {"url": ("string", "")})
def extract_links(url): return "\n".join(sorted(set(up.urljoin(url, h) for h in re.findall(r'href="([^"#]+)"', get(url).text)))[:60])

@tool("pypi_info", "Latest version and summary of a PyPI package.", {"package": ("string", "")})
def pypi_info(package): i = get(f"https://pypi.org/pypi/{package}/json").json()["info"]; return f"{i['name']} {i['version']}\n{i['summary']}\nrequires: {i.get('requires_python')}"

@tool("npm_info", "Latest version and summary of an npm package.", {"package": ("string", "")})
def npm_info(package): d = get(f"https://registry.npmjs.org/{package}/latest").json(); return f"{d['name']} {d['version']}\n{d.get('description')}"

@tool("stack_search", "Search Stack Overflow questions.", {"query": ("string", "")})
def stack_search(query):
    d = get("https://api.stackexchange.com/2.3/search/advanced", params={"q": query, "site": "stackoverflow", "pagesize": 6, "order": "desc", "sort": "relevance"}).json()
    return "\n".join(f"[{i['score']}] {i['title']} {i['link']}" for i in d.get("items", []))

@tool("wikipedia", "Short Wikipedia summary of a topic.", {"topic": ("string", "")})
def wikipedia(topic): return get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{up.quote(topic)}").json().get("extract", "not found")

# ================= utilities =================
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg}
def _ev(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)): return n.value
    if isinstance(n, ast.BinOp):
        if isinstance(n.op, ast.Pow) and abs(_ev(n.right)) > 1000: raise ValueError("exponent too large")
        return _OPS[type(n.op)](_ev(n.left), _ev(n.right))
    if isinstance(n, ast.UnaryOp): return _OPS[type(n.op)](_ev(n.operand))
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and hasattr(math, n.func.id): return getattr(math, n.func.id)(*[_ev(a) for a in n.args])
    if isinstance(n, ast.Name) and n.id in ("pi", "e"): return getattr(math, n.id)
    raise ValueError("unsupported expression")

@tool("calculate", "Exact math: + - * / ** % and sqrt, log, sin, cos, pi, e...", {"expr": ("string", "")})
def calculate(expr): return str(_ev(ast.parse(expr, mode="eval").body))

@tool("regex_test", "Test a regex against text and show matches.", {"pattern": ("string", ""), "text": ("string", "")})
def regex_test(pattern, text): return json.dumps([m.group(0) if not m.groups() else list(m.groups()) for m in re.finditer(pattern, text)][:50])

@tool("encode_decode", "Encode/decode text. mode: b64enc b64dec urlenc urldec hexenc hexdec", {"mode": ("string", ""), "text": ("string", "")})
def encode_decode(mode, text):
    f = {"b64enc": lambda s: base64.b64encode(s.encode()).decode(), "b64dec": lambda s: base64.b64decode(s).decode(), "urlenc": up.quote, "urldec": up.unquote, "hexenc": lambda s: s.encode().hex(), "hexdec": lambda s: bytes.fromhex(s).decode()}
    return f[mode](text)

@tool("hash_text", "Hash text (md5, sha1, sha256, sha512).", {"algo": ("string", ""), "text": ("string", "")})
def hash_text(algo, text): return hashlib.new(algo, text.encode()).hexdigest()

@tool("generate_uuid", "Generate UUIDs.", {"n?": ("integer", "")})
def generate_uuid(n=1): return "\n".join(str(uuid.uuid4()) for _ in range(min(int(n), 20)))

@tool("jwt_decode", "Decode a JWT's header and payload (no signature check).", {"token": ("string", "")})
def jwt_decode(token):
    d = lambda s: json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    h, p = token.split(".")[:2]; return json.dumps({"header": d(h), "payload": d(p)}, indent=1)

@tool("json_format", "Validate and pretty-print JSON.", {"text": ("string", "")})
def json_format(text): return json.dumps(json.loads(text), indent=2)

@tool("now", "Current date and time.", {"utc_offset_hours?": ("integer", "")})
def now(utc_offset_hours=0): return datetime.now(timezone(timedelta(hours=utc_offset_hours))).isoformat(timespec="seconds")

@tool("convert_timestamp", "Convert a unix timestamp to ISO time, or ISO to unix.", {"value": ("string", "")})
def convert_timestamp(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if re.fullmatch(r"\d+(\.\d+)?", value) else str(int(datetime.fromisoformat(value).timestamp()))

@tool("diff_text", "Unified diff of two text blobs.", {"a": ("string", ""), "b": ("string", "")})
def diff_text(a, b): return "".join(difflib.unified_diff(a.splitlines(1), b.splitlines(1), "a", "b")) or "identical"
