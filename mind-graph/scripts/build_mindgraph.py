#!/usr/bin/env python3
"""Validate a mind graph and render it.

Reads graph.json (see references/graph_schema.md) and writes, next to it:
  mindgraph.html  – interactive viewer (self-contained, offline)
  MINDGRAPH.md    – compact outline for future agent sessions
  mindmap.mmd     – Mermaid mindmap

If .mindgraph/scan.json (from scan_project.py) sits next to graph.json, the build also reports
documents in the scanned folder that no node points to (coverage).

Node evidence (see evidence.py): quotes in `node.evidence` are located in their files. New or
edited evidence must be found (else error) and gets a baseline; existing evidence is compared with
its baseline and nodes are marked ok / changed / stale in all outputs.

Usage:
  python build_mindgraph.py <graph.json> [--check] [--accept IDS|all] [--out-dir DIR] [--md-depth 3]
    --check    validate and verify evidence, write nothing
    --accept   re-baseline evidence of these node ids (comma-separated) after re-reading them
  python build_mindgraph.py --diff OLD NEW [--out-dir DIR]
    compare two graphs (paths, or git objects like main:.mindgraph/graph.json) and write
    mindgraph_diff.html (changes highlighted in the viewer) and DIFF.md next to NEW
Exit code 1 if there are errors (warnings don't fail the build).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evidence as evmod  # noqa: E402
import paper_graph as pgmod  # noqa: E402

TEMPLATE = HERE.parent / "assets" / "viewer.html"
KINDS = {"root", "concept", "module", "file", "symbol", "doc", "data", "external", "question"}
LINK_TYPES = {"depends_on", "calls", "data_flow", "explains", "implements", "configures", "contrasts", "related"}
PLACEHOLDER = "/*__GRAPH_DATA__*/null"
LOCATORS = ("lines", "section", "page", "slide", "sheet")
LOW_VALUE = re.compile(r"(^|/)(CODE_OF_CONDUCT|SECURITY|LICENSE|CHANGELOG|CHANGES|HISTORY|AUTHORS|CONTRIBUTORS|NOTICE)", re.I)


def walk(node, depth=0, parent=None):
    yield node, depth, parent
    for c in node.get("children") or []:
        yield from walk(c, depth + 1, node)


def validate(g: dict, base: Path):
    errors, warnings = [], []
    meta = g.get("meta") or {}
    if not meta.get("title"):
        errors.append("meta.title is missing")
    root = g.get("root")
    if not isinstance(root, dict):
        return ["'root' node is missing"], warnings
    ids = {}
    root_dir = (base / meta.get("root_path", "..")).resolve()
    missing_src = []
    max_depth = 0
    for node, depth, parent in walk(root):
        nid = node.get("id")
        max_depth = max(max_depth, depth)
        where = f"node '{nid or node.get('label', '?')}'"
        if not nid:
            errors.append(f"{where}: missing id")
            continue
        if nid in ids:
            errors.append(f"duplicate id '{nid}'")
        ids[nid] = node
        if not node.get("label"):
            errors.append(f"{where}: missing label")
        elif len(node["label"]) > 40:
            warnings.append(f"{where}: label is {len(node['label'])} chars; keep ≤ 40 so it fits on canvas")
        if not node.get("summary"):
            warnings.append(f"{where}: no summary")
        elif len(node["summary"]) > 320:
            warnings.append(f"{where}: summary {len(node['summary'])} chars; move detail into 'details'")
        kind = node.get("kind", "concept")
        if kind not in KINDS:
            warnings.append(f"{where}: unknown kind '{kind}' (use one of {sorted(KINDS)})")
        n_kids = len(node.get("children") or [])
        if n_kids > 12:
            warnings.append(f"{where}: {n_kids} children; group them (3–9 is easier to scan)")
        if parent is not None and n_kids == 1 and depth == 1:
            warnings.append(f"{where}: pillar with a single child; consider merging")
        for s in node.get("sources") or []:
            p = s.get("path") if isinstance(s, dict) else s
            if not p or re.match(r"^https?://", p):
                continue
            if not (root_dir / p).exists():
                tracked = any(isinstance(e, dict) and e.get("ctx") and e.get("path") == p
                              for e in node.get("evidence") or [])
                if tracked:  # verified earlier; evidence check marks the node stale instead of blocking
                    warnings.append(f"{where}: source {p} is gone; node marked stale — update or remove it")
                else:
                    missing_src.append(f"{nid}: {p}")
            if isinstance(s, dict):
                for k in ("page", "slide"):
                    if k in s and not re.fullmatch(r"\d+(\s*-\s*\d+)?", str(s[k])):
                        warnings.append(f"{where}: source {k} '{s[k]}' should be a number or range like 3-5")
    if missing_src:
        errors.append("sources that don't exist under root_path "
                      f"({root_dir}) – fix paths or remove claims:\n    " + "\n    ".join(missing_src[:30]))
    if max_depth > 6:
        warnings.append(f"tree depth {max_depth}; 3–4 levels reads best")
    pillars = len(root.get("children") or [])
    if pillars < 2 or pillars > 10:
        warnings.append(f"root has {pillars} pillars; 3–8 is the sweet spot")
    for i, l in enumerate(g.get("links") or []):
        for end in ("from", "to"):
            if l.get(end) not in ids:
                errors.append(f"links[{i}].{end} '{l.get(end)}' is not a node id")
        if l.get("type") and l["type"] not in LINK_TYPES:
            warnings.append(f"links[{i}]: unknown type '{l['type']}'")
    n_links = len(g.get("links") or [])
    if n_links > max(25, len(ids)):
        warnings.append(f"{n_links} cross-links for {len(ids)} nodes; keep the ones that explain architecture")
    for i, st in enumerate(g.get("learning_path") or []):
        if st.get("node") not in ids:
            errors.append(f"learning_path[{i}] node '{st.get('node')}' is not a node id")
    if not g.get("learning_path"):
        warnings.append("no learning_path; add 5–12 ordered stops")
    for i, t in enumerate(g.get("glossary") or []):
        if t.get("node") and t["node"] not in ids:
            warnings.append(f"glossary[{i}] node '{t['node']}' is not a node id")
    bad_read = [p for p in (meta.get("sources_read") or [])
                if not re.match(r"^https?://", str(p)) and not (root_dir / split_read(p)[0]).exists()]
    if bad_read:
        warnings.append("meta.sources_read entries not found: " + ", ".join(map(str, bad_read[:8])))
    return errors, warnings


def split_read(entry) -> tuple[str, str]:
    """'docs/a b.pdf (skimmed)' -> ('docs/a b.pdf', 'skimmed')"""
    m = re.match(r"^(.*?)\s*(?:\(([^()]*)\)|(§.*))?$", str(entry).strip())
    return (m.group(1), m.group(2) or m.group(3) or "") if m else (str(entry), "")


def coverage(g: dict, base: Path):
    """Documents the scan found that no node's sources (or meta.scope) account for."""
    scan_path = base / "scan.json"
    if not scan_path.exists():
        return None
    try:
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    wanted = [d for d in (scan.get("primary_docs") or scan.get("docs") or []) + (scan.get("data_files") or [])
              if not LOW_VALUE.search(d)]
    if not wanted:
        return None
    meta = g.get("meta") or {}
    covered_files, covered_dirs = set(), []
    for node, _, _ in walk(g["root"]):
        for s in node.get("sources") or []:
            p = (s.get("path") if isinstance(s, dict) else s) or ""
            p = re.sub(r"^\./", "", p.replace("\\", "/"))
            if p.endswith("/") or p in ("", "."):
                covered_dirs.append(p)
            else:
                covered_files.add(p)
    scope = str(meta.get("scope", ""))
    missing = [d for d in wanted
               if d not in covered_files and not any(d.startswith(x) for x in covered_dirs)
               and d not in scope]
    return {"total": len(wanted), "missing": missing, "mode": scan.get("mode")}


def fill_meta(g: dict, base: Path):
    meta = g.setdefault("meta", {})
    meta.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    root_dir = (base / meta.get("root_path", "..")).resolve()
    if not meta.get("git_commit"):
        try:
            r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root_dir,
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                meta["git_commit"] = r.stdout.strip()
        except Exception:
            pass
    nodes = list(walk(g["root"]))
    meta["stats"] = {"nodes": len(nodes), "links": len(g.get("links") or []),
                     "depth": max(d for _, d, _ in nodes), "pillars": len(g["root"].get("children") or [])}


def fmt_src(node, limit=3):
    out = []
    for s in node.get("sources") or []:
        if isinstance(s, str):
            out.append(s)
        else:
            extra = locator(s)
            out.append(s.get("path", "") + (f" ({extra})" if extra and not s.get("lines") else
                                             f":{extra}" if extra else ""))
    return ", ".join(out[:limit]) + (" …" if len(out) > limit else "")


def locator(s: dict) -> str:
    """Human form of where inside a file: 'lines 10-40' → '10-40', page 5 → 'p. 5'."""
    if s.get("lines"):
        return str(s["lines"])
    bits = []
    if s.get("page"):
        bits.append(f"p. {s['page']}")
    if s.get("slide"):
        bits.append(f"slide {s['slide']}")
    if s.get("sheet"):
        bits.append(f"sheet {s['sheet']}")
    if s.get("section"):
        bits.append(str(s["section"]))
    return ", ".join(bits)


def render_md(g: dict, md_depth: int, paper_table=None):
    meta, root = g["meta"], g["root"]
    ids = {n["id"]: n for n, _, _ in walk(root)}
    L = [f"# Mind graph: {meta.get('title')}", ""]
    L.append(f"> Generated {meta.get('generated_at', '')[:10]}"
             + (f" at commit {meta['git_commit']}" if meta.get("git_commit") else "")
             + f" · {meta['stats']['nodes']} nodes. Read this before exploring the project; "
               "open files listed here only when the task needs them. "
               "If files changed since this commit, the map may be stale for those areas.")
    ev = meta.get("evidence")
    if ev and ev.get("nodes_with_evidence"):
        L.append(f"> Evidence ({ev['checked_at'][:10]}): {ev['nodes_with_evidence']} nodes are backed by quotes "
                 f"checked against their sources — {ev['ok']} ok, {ev['changed']} changed, {ev['stale']} stale. "
                 "⚠ marks nodes to re-read before trusting; run the build with --check (or scan with "
                 "--since-graph) for the current state.")
    if meta.get("scope"):
        L.append(f"> Scope: {meta['scope']}")
    L += ["", f"**{root.get('label')}** — {root.get('summary', '')}"]
    flagged = [(n, d) for n, d, _ in walk(root) if n.get("evidence_status") in ("stale", "changed")]
    if flagged:
        L += ["", "## Needs re-check", ""]
        flagged.sort(key=lambda nd: nd[0]["evidence_status"] != "stale")
        for n, _ in flagged[:25]:
            bad = [e for e in n.get("evidence") or [] if e.get("status") in ("stale", "changed")]
            what = "; ".join(f"{e.get('path')}" + (f" {e['at']}" if e.get("at") else "") for e in bad[:3])
            L.append(f"- ⚠{n['evidence_status']} **{n['label']}** `{n['id']}` — {what}")
        if len(flagged) > 25:
            L.append(f"- … {len(flagged) - 25} more (see graph.json `evidence_status`)")
    L += ["", "## Map", ""]

    def emit(node, depth):
        for c in node.get("children") or []:
            star = " ★" if c.get("importance") == 3 else ""
            if c.get("evidence_status") in ("stale", "changed"):
                star += f" ⚠{c['evidence_status']}"
            # deeper levels stay terse: one source, short summary (the HTML/graph.json hold the rest)
            src = fmt_src(c, 3 if depth == 1 else 1)
            summ = c.get("summary", "")
            limit = 220 if depth == 1 else 110
            if len(summ) > limit:
                summ = summ[: limit - 1].rstrip() + "…"
            line = f"{'  ' * (depth - 1)}- **{c['label']}**{star} `{c['id']}`"
            if summ:
                line += f" — {summ}"
            if src:
                line += f" [{src}]"
            L.append(line)
            if depth < md_depth:
                emit(c, depth + 1)
            elif c.get("children"):
                L.append(f"{'  ' * depth}- … {sum(1 for _ in walk(c)) - 1} more nodes (see graph.json `{c['id']}`)")
    emit(root, 1)
    links = g.get("links") or []
    if links:
        L += ["", "## Key relationships", ""]
        for l in links[:40]:
            a, b = ids.get(l["from"], {}), ids.get(l["to"], {})
            L.append(f"- {a.get('label', l['from'])} —{l.get('type', 'related')}"
                     + (f" ({l['label']})" if l.get("label") else "")
                     + f"→ {b.get('label', l['to'])}")
    if paper_table:
        L += pgmod.md_lines(paper_table)
    lp = g.get("learning_path") or []
    if lp:
        L += ["", "## Learning path", ""]
        for i, st in enumerate(lp, 1):
            n = ids.get(st["node"], {})
            L.append(f"{i}. **{n.get('label', st['node'])}** — {st.get('note', '')}")
    gl = g.get("glossary") or []
    if gl:
        L += ["", "## Glossary", ""]
        L += [f"- **{t['term']}**: {t.get('definition', '')}" for t in gl[:30]]
    if meta.get("sources_read"):
        L += ["", f"_Files read to build this map ({len(meta['sources_read'])}):_ "
              + ", ".join(meta["sources_read"][:40])
              + (" …" if len(meta["sources_read"]) > 40 else "")]
    return "\n".join(L) + "\n"


def mermaid_text(s: str):
    s = re.sub(r"[()\[\]{}<>\"`]", " ", s)
    return re.sub(r"\s+", " ", s).strip() or "?"


def render_mermaid(g: dict, max_depth=4):
    L = ["mindmap", f"  root(({mermaid_text(g['root']['label'])}))"]

    def emit(node, depth):
        for c in node.get("children") or []:
            L.append("  " * (depth + 1) + mermaid_text(c["label"]))
            if depth < max_depth:
                emit(c, depth + 1)
    emit(g["root"], 1)
    return "\n".join(L) + "\n"


def render_html(g: dict, paper_table=None):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        sys.exit(f"template placeholder missing in {TEMPLATE}")
    payload = dict(g, paper_map=paper_table) if paper_table else g
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    title = re.sub(r"[<>&]", "", g["meta"].get("title", "Mind graph"))
    return tpl.replace(PLACEHOLDER, data).replace("__TITLE__", title)


def run_diff(old_spec: str, new_spec: str, out_dir=None):
    import graph_diff as gd
    old, old_lab = gd.load(old_spec)
    new, new_lab = gd.load(new_spec)
    for name, g in (("OLD", old), ("NEW", new)):
        if not isinstance(g.get("root"), dict):
            sys.exit(f"ERROR: {name} has no root node")
    merged, res, oi, ni = gd.merge(old, new, old_lab, new_lab)
    out = Path(out_dir) if out_dir else (Path(new_spec).resolve().parent if Path(new_spec).exists() else Path.cwd())
    out.mkdir(parents=True, exist_ok=True)
    nodes = list(walk(merged["root"]))
    merged["meta"]["stats"] = {"nodes": len(nodes), "links": len(merged.get("links") or []),
                               "depth": max(d for _, d, _ in nodes), "pillars": len(merged["root"].get("children") or [])}
    (out / "mindgraph_diff.html").write_text(render_html(merged), encoding="utf-8")
    (out / "DIFF.md").write_text(gd.md(merged, res, oi, ni), encoding="utf-8")
    c, lc = merged["diff"]["counts"], merged["diff"]["links"]
    print(f"Diff {old_lab} → {new_lab}: +{c['added']} added · −{c['removed']} removed · ~{c['changed']} changed · "
          f"↷{c['moved']} moved nodes; links +{lc['added']} −{lc['removed']} ~{lc['changed']}")
    for name, cc in merged["diff"]["areas"][:5]:
        print(f"  {name}: " + ", ".join(f"{v} {k}" for k, v in cc.items()))
    print(f"  → {out / 'mindgraph_diff.html'}, {out / 'DIFF.md'}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("graph", nargs="?")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"),
                    help="compare two graphs (path or REV:path) → mindgraph_diff.html + DIFF.md")
    ap.add_argument("--check", action="store_true", help="validate and verify evidence; write nothing")
    ap.add_argument("--accept", default="", metavar="IDS",
                    help="comma-separated node ids (or 'all') whose evidence you re-read: store a new baseline")
    ap.add_argument("--out-dir")
    ap.add_argument("--md-depth", type=int, default=3, help="outline depth in MINDGRAPH.md")
    a = ap.parse_args()
    if a.diff:
        return run_diff(a.diff[0], a.diff[1], a.out_dir)
    if not a.graph:
        ap.error("give a graph.json, or --diff OLD NEW")
    gp = Path(a.graph)
    try:
        g = json.loads(gp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: graph.json is not valid JSON: {e}")
    base = gp.resolve().parent
    errors, warnings = validate(g, base)
    paper_table = None
    if isinstance(g.get("root"), dict):
        e2, w2 = pgmod.validate(g)
        errors += e2
        warnings += w2
        if not e2:
            paper_table = pgmod.build_table(g, base)
            if paper_table:
                warnings += pgmod.coverage_warnings(paper_table)
    cov = coverage(g, base) if isinstance(g.get("root"), dict) else None
    if cov and cov["missing"]:
        n, m = cov["total"], len(cov["missing"])
        warnings.append(f"coverage: {n - m}/{n} scanned documents are referenced by a node; not referenced: "
                        + ", ".join(cov["missing"][:12]) + (" …" if m > 12 else "")
                        + ". Add them to a node's sources, or name them as skipped in meta.scope.")
    ev_rep = None
    if isinstance(g.get("root"), dict):
        root_dir = (base / (g.get("meta") or {}).get("root_path", "..")).resolve()
        accept = {x.strip() for x in a.accept.split(",") if x.strip()}
        ev_rep = evmod.verify_graph(g, root_dir, mutate=not a.check, accept=accept)
        errors += ev_rep["errors"]
        warnings += ev_rep["warnings"]
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("ERROR:", e)
    if ev_rep and ev_rep["with_evidence"]:
        print("\n".join(evmod.report_lines(ev_rep)))
    if errors:
        print(f"\n{len(errors)} error(s); nothing written.")
        sys.exit(1)
    if a.check:
        print("OK (check only)")
        return
    fill_meta(g, base)
    out = Path(a.out_dir) if a.out_dir else base
    out.mkdir(parents=True, exist_ok=True)
    gp.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")  # persist filled meta
    if paper_table:  # rebuild after evidence statuses were filled in
        paper_table = pgmod.build_table(g, base)
    (out / "mindgraph.html").write_text(render_html(g, paper_table), encoding="utf-8")
    md = render_md(g, a.md_depth, paper_table)
    (out / "MINDGRAPH.md").write_text(md, encoding="utf-8")
    (out / "mindmap.mmd").write_text(render_mermaid(g), encoding="utf-8")
    st = g["meta"]["stats"]
    if cov:
        print(f"Coverage: {cov['total'] - len(cov['missing'])}/{cov['total']} documents from scan.json referenced")
    if paper_table:
        c = paper_table["counts"]
        print(f"Paper ↔ code: {len(paper_table['rows'])} items · {c['exact']} exact · {c['approx'] + c['mapped']} approx/mapped · "
              f"{c['partial']} partial · {c['deviates']} deviate · {c['missing']} not in code · "
              f"{len(paper_table['extras'])} code-only · {len(paper_table['unmapped'])} unmapped")
    print(f"Built {st['nodes']} nodes, {st['links']} links, depth {st['depth']} → {out}/")
    est = max(len(md) // 4, len(md.encode("utf-8")) * 10 // 36)  # rough; non-Latin scripts cost more
    print(f"  mindgraph.html, MINDGRAPH.md (~{est} tokens, rough), mindmap.mmd")
    if est > 4500:
        print("  note: MINDGRAPH.md is large for a session primer; try --md-depth 2 or shorter summaries")


if __name__ == "__main__":
    main()
