#!/usr/bin/env python3
"""Cheap structural inventory of a project or a folder of documents, for planning what to read.

Prints a budgeted markdown summary to stdout and (optionally) writes full detail to JSON.
Reads *structure* — headings, page/slide/sheet counts, lead sentences, top-level symbols, imports,
links between notes — but never dumps file contents.

Works on codebases, documentation sets, note vaults (Markdown / Obsidian [[wiki links]]),
folders of PDFs / Word / PowerPoint / Excel files, single files, or a list of files and folders.

Usage:
  python scan_project.py <path> [<path> ...] [--out scan.json] [--max-lines 240]
                         [--exclude GLOB ...] [--since-graph]
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_formats as df  # noqa: E402

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode", ".mindgraph", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "vendor", "coverage",
    ".gradle", "out", "bin", "obj", ".cache", ".ipynb_checkpoints", "wandb", "mlruns",
    "outputs", "lightning_logs", ".terraform", ".DS_Store", ".obsidian", ".trash", "_site",
}
IGNORE_FILES = re.compile(r"(^|/)(~\$[^/]*|\.~lock\.[^/]*#|Thumbs\.db|desktop\.ini|\.DS_Store)$", re.I)
CODE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C/C++ header",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++ header", ".cs": "C#", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".lua": "Lua",
    ".r": "R", ".jl": "Julia", ".m": "MATLAB/ObjC", ".sql": "SQL", ".proto": "Protobuf",
    ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart", ".ql": "CodeQL", ".qll": "CodeQL",
    ".ps1": "PowerShell",
}
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "environment.yml",
    "package.json", "tsconfig.json", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "Cargo.toml", "go.mod", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "Makefile", "CMakeLists.txt", "Gemfile", "composer.json",
    "CLAUDE.md", "AGENTS.md", ".env.example", "qlpack.yml", "codeql-pack.yml", "mkdocs.yml",
}
CONFIG_EXT = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".json"}
ENTRY_NAMES = {
    "main.py", "__main__.py", "app.py", "manage.py", "server.py", "cli.py", "run.py",
    "train.py", "eval.py", "evaluate.py", "inference.py", "predict.py", "index.js",
    "index.ts", "main.ts", "main.js", "server.js", "server.ts", "main.go", "main.rs",
    "Main.java", "Application.java", "main.cpp", "main.c", "App.tsx", "App.jsx",
}
MAX_TEXT_BYTES = 1_500_000
DOC_TIME_BUDGET = 90.0   # seconds of rich-document probing (PDF/Office) before falling back to metadata


def run(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
    except Exception:
        return None


def list_files(root: Path):
    """Prefer git's view (respects .gitignore); fall back to a walk with ignore dirs.
    Paths are always POSIX-style relative paths."""
    out = run(["git", "-c", "core.quotepath=off", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
    if out:
        files = []
        for rel in out.split("\0"):
            if not rel:
                continue
            parts = rel.split("/")
            if any(p in IGNORE_DIRS for p in parts[:-1]) or IGNORE_FILES.search(rel):
                continue
            if (root / rel).is_file():
                files.append(rel)
        return sorted(files), True
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in IGNORE_DIRS and (not d.startswith(".") or d == ".github"))
        for f in filenames:
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if not IGNORE_FILES.search(rel):
                files.append(rel)
    return sorted(files), False


def read_text(p: Path):
    try:
        if p.stat().st_size > MAX_TEXT_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def first_line(s: str | None, n=90):
    if not s:
        return ""
    s = s.strip().splitlines()[0].strip() if s.strip() else ""
    return s[: n - 1] + "…" if len(s) > n else s


def human(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ---------- symbol & import extraction ----------

def py_info(text: str):
    syms, imports, is_entry = [], [], False
    try:
        tree = ast.parse(text)
    except Exception:
        return syms, imports, ("__main__" in text), ""
    doc = first_line(ast.get_docstring(tree))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            syms.append({"name": node.name + "()", "doc": first_line(ast.get_docstring(node), 70)})
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")]
            syms.append({"name": "class " + node.name, "doc": first_line(ast.get_docstring(node), 70),
                         "methods": methods[:8]})
        elif isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.If):
            src = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "__name__" in src and "__main__" in src:
                is_entry = True
    return syms, imports, is_entry, doc


RE_JS_SYM = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(function|class|const|let|interface|type|enum)\s+([A-Za-z0-9_$]+)", re.M)
RE_JS_IMP = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(|require\()\s*['"]([^'"]+)['"]""")
RE_JAVA_SYM = re.compile(r"^\s*(?:public\s+|protected\s+|private\s+)?(?:abstract\s+|final\s+|static\s+)*(class|interface|enum|record)\s+([A-Za-z0-9_]+)", re.M)
RE_JAVA_MAIN = re.compile(r"public\s+static\s+void\s+main\s*\(")
RE_JAVA_IMP = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)
RE_JAVA_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
RE_GO_SYM = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z][A-Za-z0-9_]*)", re.M)
RE_RS_SYM = re.compile(r"^\s*pub\s+(?:async\s+)?(fn|struct|enum|trait)\s+([A-Za-z0-9_]+)", re.M)
RE_C_SYM = re.compile(r"^(?:class|struct)\s+([A-Za-z0-9_]+)\s*[:{]", re.M)
RE_CPP_INC = re.compile(r'^\s*#include\s+"([^"]+)"', re.M)
RE_QL_SYM = re.compile(r"^\s*(?:module|class|predicate)\s+([A-Za-z0-9_]+)", re.M)
RE_CS_SYM = re.compile(r"^\s*(?:public|internal)\s+(?:static\s+|sealed\s+|abstract\s+|partial\s+)*(class|interface|record|struct|enum)\s+([A-Za-z0-9_]+)", re.M)


def other_info(ext: str, text: str):
    syms, imports, is_entry = [], [], False
    if ext in (".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue", ".svelte"):
        syms = [{"name": f"{k} {n}"} for k, n in RE_JS_SYM.findall(text)]
        imports = RE_JS_IMP.findall(text)
    elif ext in (".java", ".kt", ".scala"):
        syms = [{"name": f"{k} {n}"} for k, n in RE_JAVA_SYM.findall(text)]
        imports = RE_JAVA_IMP.findall(text)
        is_entry = bool(RE_JAVA_MAIN.search(text))
    elif ext == ".cs":
        syms = [{"name": f"{k} {n}"} for k, n in RE_CS_SYM.findall(text)]
        is_entry = "static void Main(" in text or "static async Task Main(" in text
    elif ext == ".go":
        syms = [{"name": n + "()"} for n in RE_GO_SYM.findall(text)]
        is_entry = "func main()" in text
    elif ext == ".rs":
        syms = [{"name": f"{k} {n}"} for k, n in RE_RS_SYM.findall(text)]
        is_entry = "fn main()" in text
    elif ext in (".c", ".cc", ".cpp", ".h", ".hpp"):
        syms = [{"name": n} for n in RE_C_SYM.findall(text)]
        imports = RE_CPP_INC.findall(text)
        is_entry = bool(re.search(r"\bint\s+main\s*\(", text))
    elif ext in (".ql", ".qll"):
        syms = [{"name": n} for n in RE_QL_SYM.findall(text)]
    return syms, imports, is_entry


# ---------- import & link resolution ----------

def resolve_python(files_py: set[str], src_file: str, mod: str):
    """Map an import string to a project file, if it is internal."""
    cands = []
    if mod.startswith("."):
        level = len(mod) - len(mod.lstrip("."))
        base = posixpath.dirname(src_file)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        rest = mod.lstrip(".").replace(".", "/")
        stem = posixpath.join(base, rest) if rest else base
        cands += [f"{stem}.py", f"{stem}/__init__.py"]
    else:
        rel = mod.replace(".", "/")
        here = posixpath.dirname(src_file)  # sibling module of a script (sys.path[0] imports)
        cands += [posixpath.join(here, rel) + ".py", posixpath.join(here, rel, "__init__.py")]
        for prefix in ("", "src/", "lib/"):
            cands += [f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py"]
        parts = mod.split(".")
        if len(parts) > 1:
            cands.append("/".join(parts[:-1]) + ".py")
    for c in cands:
        c = posixpath.normpath(c)
        if c in files_py:
            return c
    return None


def resolve_js(files: set[str], src_file: str, spec: str):
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(src_file), spec))
    for c in (base, *(base + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue")),
              *(base + "/index" + e for e in (".ts", ".tsx", ".js", ".jsx"))):
        if c in files:
            return c
    return None


class LinkResolver:
    """Resolve note/doc link targets ([[wiki]], relative links, `path` mentions) to files."""

    def __init__(self, files):
        self.files = set(files)
        self.by_stem = defaultdict(list)
        self.by_name = defaultdict(list)
        for f in files:
            name = posixpath.basename(f)
            self.by_name[name.lower()].append(f)
            self.by_stem[self.key(posixpath.splitext(name)[0])].append(f)

    @staticmethod
    def key(stem: str) -> str:
        """'Value Methods' ≈ 'value-methods' ≈ 'value_methods'"""
        return re.sub(r"[\s_\-]+", "-", stem.strip().lower())

    def resolve(self, src: str, target: str):
        if target.startswith("[["):
            t = target[2:].strip().replace("\\", "/")
            base = posixpath.basename(t)
            cands = self.by_name.get(base.lower()) or self.by_stem.get(self.key(base)) or []
            if "/" in t:
                cands = [c for c in cands if posixpath.splitext(c)[0].lower().endswith(posixpath.splitext(t)[0].lower())] or cands
            return sorted(cands, key=len)[0] if cands else None
        t = unquote(target).replace("\\", "/").strip()
        if not t or t.startswith(("http:", "https:")):
            return None
        bases = [posixpath.dirname(src), ""] if not t.startswith("/") else [""]
        for b in bases:
            p = posixpath.normpath(posixpath.join(b, t.lstrip("/")))
            for c in (p, p + ".md", p.rstrip("/") + "/README.md", p.rstrip("/") + "/index.md"):
                if c in self.files and c != src:
                    return c
        return None


# ---------- main scan ----------

def select_files(files, select, excludes):
    if select:
        files = [f for f in files if any(f == s or f.startswith(s.rstrip("/") + "/") for s in select)]
    if excludes:
        files = [f for f in files if not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.rstrip("/") + "/*")
                                             or f.startswith(g.rstrip("/*") + "/") for g in excludes)]
    return files


def scan(root: Path, select=None, excludes=None):
    t0 = time.time()
    files, via_git = list_files(root)
    files = select_files(files, select, excludes or [])
    fileset = set(files)
    py_files = {f for f in files if f.endswith(".py")}
    info = {}
    langs, lang_lines = Counter(), Counter()
    docs, data_files, images, legacy, configs, entries = [], [], [], [], [], []
    other_ext = Counter()
    java_pkg, raw_imports, raw_links = {}, {}, {}
    doc_time = 0.0
    skipped_probe = 0

    for rel in files:
        p = root / rel
        ext = p.suffix.lower()
        name = p.name
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rec = {"path": rel, "bytes": size}
        if ext in CODE_EXT:
            langs[CODE_EXT[ext]] += 1
            text = read_text(p)
            if text is not None:
                rec["lines"] = text.count("\n") + 1
                lang_lines[CODE_EXT[ext]] += rec["lines"]
                if ext == ".py":
                    syms, imps, is_entry, doc = py_info(text)
                    if doc:
                        rec["doc"] = doc
                else:
                    syms, imps, is_entry = other_info(ext, text)
                    if ext in (".java", ".kt"):
                        m = RE_JAVA_PKG.search(text)
                        if m:
                            java_pkg[rel] = m.group(1) + "." + p.stem
                rec["symbols"] = syms[:25]
                raw_imports[rel] = imps
                if is_entry or name in ENTRY_NAMES:
                    entries.append(rel)
        elif ext in df.SUPPORTED:
            rich = ext not in df.TEXT_EXT and ext not in (".csv", ".tsv", ".ipynb", ".html", ".htm")
            if rich and doc_time > DOC_TIME_BUDGET:
                rec.update({"format": ext.lstrip("."), "note": "not probed (scan time budget); use extract_text.py"})
                skipped_probe += 1
            else:
                t1 = time.time()
                rec.update(df.probe(p))
                if rich:
                    doc_time += time.time() - t1
            raw_links[rel] = rec.pop("links", None) or []
            if ext == ".ipynb":
                langs["Notebook"] += 1
                rec["kind"] = "notebook"
                docs.append(rel)
            elif ext in df.DATA_EXT:
                rec["kind"] = "data"
                data_files.append(rel)
            else:
                rec["kind"] = "doc"
                docs.append(rel)
            rec["tokens_est"] = int(rec.get("words", 0) * 1.4)
        elif ext in df.LEGACY_EXT:
            rec.update({"kind": "doc", "format": ext.lstrip("."),
                        "note": f"legacy format; convert to {df.LEGACY_EXT[ext]} to read"})
            legacy.append(rel)
            docs.append(rel)
        elif ext in df.IMAGE_EXT:
            images.append(rel)
        elif name in CONFIG_NAMES or (ext in CONFIG_EXT and size < 200_000 and rel.count("/") <= 2):
            configs.append(rel)
        else:
            other_ext[ext or "(no ext)"] += 1
        info[rel] = rec

    # links between documents (and from documents to code): wiki links, relative links, `paths`
    resolver = LinkResolver(files)
    doc_edges = defaultdict(set)
    for rel, links in raw_links.items():
        for t in links:
            dst = resolver.resolve(rel, t)
            if dst and dst != rel:
                doc_edges[rel].add(dst)
    doc_indeg = Counter(d for ds in doc_edges.values() for d in ds)
    linked_docs = [{"path": f, "linked_from": n} for f, n in doc_indeg.most_common(15) if n >= 2]
    touched = set(doc_edges) | set(doc_indeg)
    orphans = [d for d in docs if d not in touched]

    # package.json scripts / pyproject scripts as entry hints
    entry_hints = []
    pj = root / "package.json"
    if pj.exists() and "package.json" in fileset:
        try:
            d = json.loads(pj.read_text(encoding="utf-8"))
            for k, v in (d.get("scripts") or {}).items():
                entry_hints.append(f"npm run {k}: {v}"[:110])
        except Exception:
            pass
    pp = root / "pyproject.toml"
    if pp.exists() and "pyproject.toml" in fileset:
        txt = read_text(pp) or ""
        m = re.search(r"\[project\.scripts\]\n((?:[^\[\n].*\n?)+)", txt)
        if m:
            entry_hints += ["script " + l.strip() for l in m.group(1).splitlines() if l.strip()][:10]

    # internal import graph
    pkg_to_file = {v: k for k, v in java_pkg.items()}
    edges = defaultdict(set)
    for src, imps in raw_imports.items():
        for imp in imps:
            dst = None
            if src.endswith(".py"):
                dst = resolve_python(py_files, src, imp)
            elif src.endswith((".java", ".kt")):
                dst = pkg_to_file.get(imp)
            elif src.endswith((".c", ".cc", ".cpp", ".h", ".hpp")):
                cand = posixpath.normpath(posixpath.join(posixpath.dirname(src), imp))
                dst = cand if cand in fileset else next((f for f in fileset if f.endswith("/" + imp)), None)
            else:
                dst = resolve_js(fileset, src, imp)
            if dst and dst != src:
                edges[src].add(dst)
    indeg = Counter(d for ds in edges.values() for d in ds)
    hubs = [{"path": f, "imported_by": n} for f, n in indeg.most_common(15) if n >= 2]

    # externals (top third-party python/js imports)
    ext_deps = Counter()
    for src, imps in raw_imports.items():
        for imp in imps:
            if src.endswith(".py") and not imp.startswith(".") and not resolve_python(py_files, src, imp):
                ext_deps[imp.split(".")[0]] += 1
            elif src.endswith((".js", ".ts", ".tsx", ".jsx")) and not imp.startswith("."):
                ext_deps[imp.split("/")[0] if not imp.startswith("@") else "/".join(imp.split("/")[:2])] += 1
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    externals = [(k, v) for k, v in ext_deps.most_common(40) if k not in stdlib and k != "__future__"][:20]

    # what kind of folder is this?
    n_code = sum(v for k, v in langs.items() if k != "Notebook")
    code_lines = sum(n for _, n in lang_lines.most_common())
    primary_docs = [d for d in docs if doc_score(d, info) >= 0]
    doc_words = sum(info[d].get("words", 0) for d in primary_docs)
    if n_code == 0 or (len(primary_docs) >= 2 * n_code and doc_words > code_lines * 2):
        mode = "documents"
    elif len(primary_docs) <= max(3, n_code // 10) and doc_words < code_lines:
        mode = "code"
    else:
        mode = "mixed"

    # git context
    git = {}
    head = run(["git", "rev-parse", "--short", "HEAD"], root)
    if head:
        git["commit"] = head.strip()
        git["branch"] = (run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root) or "").strip()
        log = run(["git", "-c", "core.quotepath=off", "log", "-n", "200", "--name-only", "--pretty=format:", "--relative"], root) or ""
        churn = Counter(l for l in log.splitlines() if l.strip() and l in fileset)
        git["churn_top"] = churn.most_common(10)
        git["last_commit"] = (run(["git", "log", "-1", "--pretty=%ad %s", "--date=short"], root) or "").strip()[:100]

    return {
        "root": str(root.resolve()),
        "selection": select or [],
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "via_git": via_git,
        "mode": mode,
        "file_count": len(files),
        "languages": langs.most_common(),
        "language_lines": lang_lines.most_common(),
        "other_ext": other_ext.most_common(12),
        "docs": docs,
        "primary_docs": primary_docs,
        "doc_words": doc_words,
        "data_files": data_files,
        "images": images,
        "legacy_docs": legacy,
        "docs_not_probed": skipped_probe,
        "configs": configs,
        "entry_points": sorted(set(entries)),
        "entry_hints": entry_hints,
        "hubs": hubs,
        "externals": externals,
        "import_edges": {k: sorted(v) for k, v in edges.items()},
        "doc_links": {k: sorted(v) for k, v in doc_edges.items()},
        "linked_docs": linked_docs,
        "orphan_docs": orphans if len(docs) >= 5 else [],
        "git": git,
        "files": info,
        "scan_seconds": round(time.time() - t0, 2),
    }


def evidence_check(graph_path: Path):
    """Read-only node-level check of the existing graph's evidence quotes."""
    try:
        import evidence
        g = json.loads(graph_path.read_text(encoding="utf-8"))
        root_dir = (graph_path.parent / (g.get("meta") or {}).get("root_path", "..")).resolve()
        rep = evidence.verify_graph(g, root_dir, mutate=False)
    except Exception as e:  # never let the check break the scan
        return {"lines": [f"(evidence check failed: {type(e).__name__}: {e})"], "flagged": []}
    if not rep["with_evidence"]:
        return {"lines": ["No node has evidence quotes yet; only file-level changes are known."], "flagged": []}
    fl = evidence.flagged(rep)
    return {"lines": evidence.report_lines(rep, 30), "flagged": [nid for nid, _ in fl]}


def changed_since_graph(root: Path, graph_path: Path, select=None, excludes=None):
    try:
        g = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    meta = g.get("meta", {})
    commit = meta.get("git_commit")
    res = {"graph_commit": commit, "generated_at": meta.get("generated_at")}
    if commit:
        out = run(["git", "-c", "core.quotepath=off", "diff", "--name-only", "--relative", commit, "--", "."], root)
        unt = run(["git", "-c", "core.quotepath=off", "ls-files", "--others", "--exclude-standard"], root) or ""
        if out is not None:
            ch = sorted(set(l for l in (out + "\n" + unt).splitlines() if l.strip() and not l.startswith(".mindgraph/")))
            res["changed"] = select_files(ch, select, excludes or [])
            return res
    ts = meta.get("generated_at")
    cutoff = graph_path.stat().st_mtime
    if ts:
        try:
            cutoff = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            pass
    files, _ = list_files(root)
    files = select_files(files, select, excludes or [])
    res["changed"] = [f for f in files if (root / f).stat().st_mtime > cutoff]
    return res


# ---------- summary rendering ----------

LOW_VALUE_DOCS = re.compile(r"(^|/)(CODE_OF_CONDUCT|SECURITY|LICENSE|CHANGELOG|CHANGES|HISTORY|AUTHORS|CONTRIBUTORS|"
                            r"PULL_REQUEST_TEMPLATE|ISSUE_TEMPLATE|NOTICE)|^\.github/|requirements.*\.txt$|(^|/)LICENSE", re.I)
KEY_DOC_NAMES = {"agents", "claude", "architecture", "design", "overview", "index", "intro", "introduction",
                 "getting_started", "getting-started", "quickstart", "guide", "agent_guide", "home", "moc",
                 "_index", "toc", "contents", "syllabus", "summary", "abstract", "muc-luc", "mục lục", "tổng quan",
                 "gioi-thieu", "giới thiệu", "spec", "specification", "proposal", "plan", "roadmap"}


def doc_score(path: str, info=None):
    p = path.lower()
    name = posixpath.basename(p)
    depth = p.count("/") + 1
    if LOW_VALUE_DOCS.search(path):
        return -1
    score = 10 - 2 * depth
    stem = name.rsplit(".", 1)[0]
    if name.startswith("readme"):
        score += 40 if depth == 1 else 12
    if stem in KEY_DOC_NAMES or stem.lstrip("0123456789_- ") in KEY_DOC_NAMES:
        score += 30
    if name.endswith((".pdf", ".docx", ".pptx", ".epub")):
        score += 12  # papers/specs/slides are usually primary sources
    if name.endswith((".html", ".htm")):
        score -= 4
    if "/api/" in p or "changelog" in p or "release" in p:
        score -= 8
    return score


def dir_tree(files, budget=45):
    counts = Counter()
    for f in files:
        parts = f.split("/")
        for d in range(1, min(len(parts), 4)):
            counts["/".join(parts[:d]) + "/"] += 1
    dirs = sorted(counts)
    thr = 1
    while True:
        keep = [d for d in dirs if d.count("/") == 1 or counts[d] >= thr]
        if len(keep) <= budget or thr > 10_000:
            break
        thr = max(thr + 1, int(thr * 1.5))
    lines = []
    for d in keep:
        depth = d.count("/") - 1
        lines.append(f"{'  ' * depth}{d.rstrip('/').split('/')[-1]}/ ({counts[d]})")
    hidden = len(dirs) - len(keep)
    if hidden:
        lines.append(f"(+{hidden} smaller dirs with < {thr} files not shown)")
    top_files = [f for f in files if "/" not in f]
    if top_files:
        lines.append("top-level files: " + ", ".join(top_files[:24]) + (" …" if len(top_files) > 24 else ""))
    return lines


def group_paths(paths, per_group=4):
    groups = defaultdict(list)
    for p in paths:
        groups[posixpath.dirname(p) or "."].append(posixpath.basename(p))
    out = []
    for d, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append(f"{d}/ ({len(names)}): " + ", ".join(names[:per_group]) + (" …" if len(names) > per_group else ""))
    return out


def doc_size(r: dict) -> str:
    bits = [r.get("format", "")]
    for k, unit in (("pages", "p"), ("slides", " slides"), ("sheets", " sheets"), ("rows", " rows")):
        if r.get(k) and not isinstance(r[k], list):
            bits.append(f"{r[k]}{unit}")
    if r.get("words"):
        bits.append(("~" if r.get("words_estimated") else "") + human(r["words"]) + "w")
    return ", ".join(b for b in bits if b)


def render_docs(s, budget: int, full: bool):
    """Documents section. full=True (documents/mixed mode): grouped by folder, with lead lines and outlines."""
    files = s["files"]
    L = []
    ranked = sorted(s["docs"], key=lambda d: (-doc_score(d, files), d))
    main = [d for d in ranked if doc_score(d, files) >= 0]
    toks = sum(files[d].get("tokens_est", 0) for d in main)
    L.append(f"\n## Documents ({len(s['docs'])}; ~{human(s['doc_words'])} words ≈ {human(toks)} tokens in the main ones)")
    if not full:
        for i, d in enumerate(main[:10]):
            r = files[d]
            L.append(f"- {d} ({doc_size(r)})" + (f" — {r['title']}" if r.get("title") else ""))
            for h in (r.get("outline") or [])[: (7 if i < 4 else 3)]:
                L.append(f"    {h}")
        others = [d for d in ranked if d not in main[:10]]
        if others:
            L.append("- other docs: " + " | ".join(group_paths(others, 4)[:6]))
        return L

    # full: group by folder, folders in path order, best docs first inside each folder
    groups = defaultdict(list)
    for d in main:
        groups[posixpath.dirname(d) or "."].append(d)
    n = len(main)
    per_doc = max(0, min(7, (budget - 2 * n - len(groups)) // max(n, 1)))
    shown = set()
    for folder in sorted(groups, key=lambda f: (f != ".", f)):
        ds = groups[folder]
        w = sum(files[d].get("words", 0) for d in ds)
        if budget - len(L) <= 3:
            break
        L.append(f"### {folder}/ ({len(ds)} docs, ~{human(w)} words)")
        for d in sorted(ds, key=lambda d: (-doc_score(d, files), d)):
            if budget - len(L) <= 2:
                break
            r = files[d]
            line = f"- {posixpath.basename(d)} ({doc_size(r)})"
            if r.get("title") and r["title"].lower() not in d.lower():
                line += f" — {r['title'][:90]}"
            if r.get("note"):
                line += f" ⚠ {r['note'][:90]}"
            L.append(line)
            shown.add(d)
            if per_doc >= 1 and r.get("lead"):
                L.append(f"    › {r['lead']}")
            for h in (r.get("outline") or [])[: max(per_doc - 1, 0)]:
                L.append(f"    {h}")
    rest = [d for d in ranked if d not in shown]
    if rest:
        L.append(f"- not shown ({len(rest)}; low-value or over budget): " + " | ".join(group_paths(rest, 4)[:6]))
    return L


def render_summary(s, max_lines=240, changes=None):
    L = []
    files = s["files"]
    mode = s["mode"]
    L.append(f"# Scan: {Path(s['root']).name}" + (f" (selected: {', '.join(s['selection'][:6])})" if s["selection"] else ""))
    tot_lines = sum(n for _, n in s["language_lines"])
    git = s["git"].get("commit", "no")
    L.append(f"{s['file_count']} files · ~{tot_lines:,} code lines · {len(s['docs'])} documents "
             f"(~{human(s['doc_words'])} words) · git: {git}"
             + (f" ({s['git']['last_commit']})" if s["git"].get("last_commit") else ""))
    hint = {
        "documents": "Mode: DOCUMENTS — organize by ideas/themes, not folders. Read outlines below, then open "
                     "documents by unit with extract_text.py (pdf/docx/pptx/xlsx/epub/html…).",
        "mixed": "Mode: MIXED code + documents — build a theory/docs side and an implementation side and "
                 "connect them with `explains` / `implements` links.",
        "code": "Mode: CODE — follow entry points and hub files.",
    }[mode]
    L.append(hint)
    papers = [d for d in s["docs"] if files[d].get("format") in ("pdf", "tex")
              and (files[d].get("pages", 0) >= 4 or files[d].get("format") == "tex"
                   or re.search(r"(^|/)(papers?|arxiv|references?)/", d, re.I))]
    if papers and any(r.get("symbols") for r in files.values()):
        L.append(f"Paper + code detected ({', '.join(papers[:3])}{' …' if len(papers) > 3 else ''}): if this repo implements "
                 "the paper, run paper_code.py <paper> --code <root> for the paper ↔ code map.")
    if s["languages"]:
        L.append("Languages: " + ", ".join(f"{k} {v}" for k, v in s["languages"][:8]))
    fmt = Counter(files[d].get("format", "?") for d in s["docs"] + s["data_files"])
    if fmt:
        L.append("Document formats: " + ", ".join(f"{k} {v}" for k, v in fmt.most_common(10)))
    if s["other_ext"]:
        L.append("Other files: " + ", ".join(f"{k} {v}" for k, v in s["other_ext"][:8]))
    if s["docs_not_probed"]:
        L.append(f"⚠ {s['docs_not_probed']} rich documents not probed (time budget) — see scan.json, read with extract_text.py")
    no_text = [d for d in s["docs"] if "PDF text backend" in files[d].get("note", "")]
    if no_text:
        L.append(f"⚠ {len(no_text)} PDFs listed without text: {df.pdf_backend_hint()}")
    if s["legacy_docs"]:
        L.append(f"⚠ legacy .doc/.ppt/.xls ({len(s['legacy_docs'])}): convert with LibreOffice "
                 f"(`soffice --headless --convert-to docx FILE`) before reading")

    if changes is not None:
        if changes.get("graph_commit") == "no graph yet":
            L.append("No existing .mindgraph/graph.json — full build.")
        else:
            ch = changes.get("changed") or []
            L.append(f"\n## Changed since graph ({changes.get('graph_commit') or changes.get('generated_at')}): {len(ch)} files")
            L += [f"- {c}" for c in ch[:40]] + ([f"- … {len(ch) - 40} more"] if len(ch) > 40 else [])
            ev = changes.get("evidence")
            if ev:
                L.append("\n## Evidence check (which nodes of the map still hold)")
                L += ev["lines"]
                if ev["flagged"]:
                    L.append("Re-read the sources of the ⚠ nodes, fix their summary/quote in graph.json, rebuild. "
                             "Nodes marked ok can be trusted without re-reading.")

    L.append("\n## Tree")
    L += dir_tree(list(files), 30 if mode == "documents" else 45)

    if mode != "documents" and (s["entry_points"] or s["entry_hints"]):
        L.append("\n## Entry points")
        core = [e for e in s["entry_points"] if not re.search(r"(^|/)(examples?|tests?|benchmarks?|docs?)/", e)]
        rest = [e for e in s["entry_points"] if e not in core]
        L += [f"- {e}" for e in core[:12]] + ([f"- … {len(core) - 12} more"] if len(core) > 12 else [])
        L += [f"- {h}" for h in s["entry_hints"][:14]]
        if rest:
            L += ["- runnable examples/tests: " + "; ".join(group_paths(rest, 3)[:4])]
    if s["hubs"]:
        L.append("\n## Hub files (imported by N internal files)")
        L += [f"- {h['path']} ← {h['imported_by']}" for h in s["hubs"][:12]]
    if s["externals"]:
        L.append("\n## Main external deps: " + ", ".join(f"{k}({v})" for k, v in s["externals"][:15]))
    cfg = [c for c in s["configs"] if not c.startswith(".github/")]
    n_gh = len(s["configs"]) - len(cfg)
    if s["configs"] and mode != "documents":
        L.append("\n## Manifests/config: " + ", ".join(cfg[:20]) + (" …" if len(cfg) > 20 else "")
                 + (f" (+{n_gh} under .github/)" if n_gh else ""))

    if s["doc_links"]:
        n_links = sum(len(v) for v in s["doc_links"].values())
        L.append(f"\n## Links between documents ({n_links} links from {len(s['doc_links'])} docs)")
        if s["linked_docs"]:
            L.append("Most linked-to: " + ", ".join(f"{h['path']} ← {h['linked_from']}" for h in s["linked_docs"][:10]))
        to_code = Counter()
        for src, dsts in s["doc_links"].items():
            to_code[src] += sum(1 for d in dsts if Path(d).suffix.lower() in CODE_EXT)
        tc = [(k, v) for k, v in to_code.most_common(6) if v]
        if tc:
            L.append("Docs that point into code: " + ", ".join(f"{k} → {v} files" for k, v in tc))
        if n_links <= 8:
            L += [f"- {src} → {d}" for src, ds in s["doc_links"].items() for d in ds]
        if s["orphan_docs"]:
            L.append(f"Unlinked docs: {len(s['orphan_docs'])} (no links in or out)")

    if s["data_files"]:
        L.append(f"\n## Data files ({len(s['data_files'])})")
        for d in s["data_files"][:10]:
            r = files[d]
            L.append(f"- {d} ({doc_size(r)})" + (": " + "; ".join(r.get("outline", [])[:3]) if r.get("outline") else ""))
        if len(s["data_files"]) > 10:
            L.append(f"- … {len(s['data_files']) - 10} more")
    if s["images"]:
        L.append(f"\n## Images/diagrams ({len(s['images'])}): " + " | ".join(group_paths(s["images"], 3)[:4])
                 + " — view the ones that look like architecture diagrams or figures")

    if s["docs"]:
        reserve = 25 if mode != "documents" and any(r.get("symbols") for r in files.values()) else 4
        budget = max(max_lines - len(L) - reserve, 30)
        L += render_docs(s, budget, full=(mode != "code"))

    if s["git"].get("churn_top"):
        L.append("\n## Most changed recently: " + ", ".join(f"{f}({n})" for f, n in s["git"]["churn_top"][:8]))

    code = [r for r in files.values() if r.get("symbols")]
    if code:
        L.append("\n## Symbols (top-level; central files first)")
        hub_set = {h["path"]: h["imported_by"] for h in s["hubs"]}
        entry_set = set(s["entry_points"])

        def rank(r):
            p = r["path"]
            penalty = 1 if re.search(r"(^|/)(tests?|examples?|benchmarks?|docs?)/", p) else 0
            return (penalty, -(hub_set.get(p, 0) * 1000 + (500 if p in entry_set else 0) + r.get("lines", 0)))
        code.sort(key=rank)
        budget = max(max_lines - len(L) - 2, 20)
        shown = 0
        for r in code:
            if budget <= 0:
                break
            names = []
            for sym in r["symbols"][:10]:
                n = sym["name"]
                if sym.get("methods"):
                    n += "{" + ",".join(sym["methods"][:5]) + "}"
                names.append(n)
            line = f"- {r['path']} ({r.get('lines', '?')}L)" + (f" — {r['doc']}" if r.get("doc") else "")
            line += ": " + "; ".join(names)
            L.append(line[:240])
            budget -= 1
            shown += 1
        if shown < len(code):
            L.append(f"- … {len(code) - shown} more files with symbols (see scan.json → files[path].symbols)")
    return "\n".join(L)


def resolve_inputs(paths):
    """One folder → that folder. One file → its folder, selecting the file.
    Several paths → their common folder, selecting each."""
    ps = [Path(p).resolve() for p in paths]
    for p, raw in zip(ps, paths):
        if not p.exists():
            sys.exit(f"not found: {raw}")
    if len(ps) == 1 and ps[0].is_dir():
        return ps[0], None
    bases = [p if p.is_dir() else p.parent for p in ps]
    root = Path(os.path.commonpath([str(b) for b in bases]))
    return root, [p.relative_to(root).as_posix() if p != root else "" for p in ps]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="folder, file, or several files/folders")
    ap.add_argument("--out", help="write full scan JSON here")
    ap.add_argument("--max-lines", type=int, default=240)
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip matching paths, e.g. --exclude 'archive/*' --exclude '*.csv' (repeatable)")
    ap.add_argument("--since-graph", action="store_true",
                    help="list files changed since <root>/.mindgraph/graph.json was built")
    a = ap.parse_args()
    root, select = resolve_inputs(a.paths)
    if select is not None and "" in select:
        select = None
    s = scan(root, select, a.exclude)
    changes = None
    if a.since_graph:
        gp = root / ".mindgraph" / "graph.json"
        changes = (changed_since_graph(root, gp, select, a.exclude) if gp.exists()
                   else {"changed": [], "graph_commit": "no graph yet"})
        if gp.exists():
            changes["evidence"] = evidence_check(gp)
        s["changes"] = changes
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(render_summary(s, a.max_lines, changes))
    print(f"\nroot: {s['root']}")
    if a.out:
        print(f"(full detail: {a.out}; scanned in {s['scan_seconds']}s)")


if __name__ == "__main__":
    main()
