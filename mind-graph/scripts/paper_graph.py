#!/usr/bin/env python3
"""Paper ↔ code layer of a mind graph: validation and the mapping table.

Graph conventions (see references/graph_schema.md › Paper ↔ code):
  - paper-side nodes carry `paper_ref`: "Eq. 7", "Alg. 1", "Alg. 1 L4", "§3.2", "Table 2",
    "App. A", "hp:clip" (a string or a list). `paper` names the paper file when there are several.
  - code nodes link to them with `implements` links: {"from": code, "to": paper_node,
    "type": "implements", "fidelity": "exact" | "approx" | "partial" | "deviates"}.
  - every place where code and paper disagree is a `question` node with
    `deviation`: {"type", "paper", "code", "impact"}, linked (usually `contrasts`) to the paper
    node and the code node, or carrying the `paper_ref` it deviates from. Code-only tricks are
    deviations with type "implementation_trick" linked to the code node only.
build_mindgraph.py turns this into a table (MINDGRAPH.md + the viewer's Paper ↔ Code tab) and, if
.mindgraph/paper_map.json (from paper_code.py) exists, reports equations, algorithms and
hyperparameter differences that the graph doesn't account for yet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FIDELITY = ("exact", "approx", "partial", "deviates")
DEV_TYPES = ("coefficient", "hyperparameter", "missing_step", "extra_step", "different_formula",
             "implementation_trick", "other")
STATUS = {"exact": "✓", "approx": "≈", "mapped": "→", "partial": "◐", "deviates": "⚠", "missing": "∅"}
KIND_RANK = {"sec": 0, "app": 5, "eq": 1, "alg": 2, "tab": 3, "fig": 3, "hp": 4}


def norm_ref(s: str) -> str | None:
    t = str(s).strip().lower().replace("§", "sec ").replace("–", "-")
    t = re.sub(r"\s+", " ", t)
    pats = [
        (r"^(?:eq(?:uation)?s?|eqn)[.:]?\s*\(?(\d+(?:\.\d+)?)\)?$", "eq:{0}"),
        (r"^alg(?:orithm)?[.:]?\s*(\d+)(?:\s*[,.]?\s*(?:line|l|step|s)\.?\s*|\.)(\d+)$", "alg:{0}.{1}"),
        (r"^alg(?:orithm)?[.:]?\s*(\d+)$", "alg:{0}"),
        (r"^sec(?:tion)?[.:]?\s*(\d+(?:\.\d+){0,3})$", "sec:{0}"),
        (r"^(?:app(?:endix)?)[.:]?\s*([a-h](?:\.\d+){0,3})$", "app:{0}"),
        (r"^tab(?:le)?[.:]?\s*(\d+)$", "tab:{0}"),
        (r"^fig(?:ure)?[.:]?\s*(\d+)$", "fig:{0}"),
        (r"^hp[.:]\s*([a-z0-9_]+)$", "hp:{0}"),
    ]
    for rx, fmt in pats:
        m = re.match(rx, t)
        if m:
            return fmt.format(*m.groups())
    return None


def pretty(ref: str) -> str:
    k, _, v = ref.partition(":")
    if k == "alg" and "." in v:
        a, b = v.split(".", 1)
        return f"Alg. {a} L{b}"
    return {"eq": f"Eq. {v}", "alg": f"Alg. {v}", "sec": f"§{v}", "app": f"App. {v.upper()}",
            "tab": f"Table {v}", "fig": f"Fig. {v}", "hp": f"hp: {v}"}.get(k, ref)


def sort_key(ref: str, order: dict):
    if ref in order:
        return (0, order[ref], ref)
    k, _, v = ref.partition(":")
    nums = [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", v)]
    return (1, KIND_RANK.get(k, 9), nums, v)


def refs_of(node) -> list[str]:
    raw = node.get("paper_ref")
    if not raw:
        return []
    vals = raw if isinstance(raw, list) else re.split(r"\s*[,;]\s*", str(raw))
    return [r for r in (norm_ref(v) for v in vals) if r]


def walk(node):
    yield node
    for c in node.get("children") or []:
        yield from walk(c)


def validate(g: dict):
    errors, warnings = [], []
    nodes = {n.get("id"): n for n in walk(g.get("root") or {}) if n.get("id")}
    linked = set()
    for i, l in enumerate(g.get("links") or []):
        linked |= {l.get("from"), l.get("to")}
        if l.get("fidelity") is not None:
            if l.get("type") != "implements":
                warnings.append(f"links[{i}]: 'fidelity' only applies to implements links")
            if l["fidelity"] not in FIDELITY:
                warnings.append(f"links[{i}]: fidelity '{l['fidelity']}' should be one of {', '.join(FIDELITY)}")
    for nid, n in nodes.items():
        raw = n.get("paper_ref")
        if raw:
            vals = raw if isinstance(raw, list) else re.split(r"\s*[,;]\s*", str(raw))
            bad = [v for v in vals if not norm_ref(v)]
            if bad:
                warnings.append(f"node '{nid}': paper_ref {bad} not understood; use e.g. 'Eq. 7', 'Alg. 1 L4', "
                                "'§3.2', 'Table 2', 'App. A', 'hp:clip'")
        dev = n.get("deviation")
        if dev is not None:
            if not isinstance(dev, dict):
                errors.append(f"node '{nid}': deviation must be an object with type / paper / code / impact")
                continue
            if dev.get("type") not in DEV_TYPES:
                warnings.append(f"node '{nid}': deviation.type '{dev.get('type')}' should be one of {', '.join(DEV_TYPES)}")
            if not dev.get("code"):
                warnings.append(f"node '{nid}': deviation should say what the code does ('code')")
            if not dev.get("paper") and dev.get("type") != "implementation_trick":
                warnings.append(f"node '{nid}': deviation should say what the paper says ('paper')")
            if n.get("kind") not in (None, "question"):
                warnings.append(f"node '{nid}': deviation nodes should have kind 'question'")
            if nid not in linked and not refs_of(n):
                warnings.append(f"node '{nid}': deviation is not linked to any paper or code node")
    return errors, warnings


def load_inventory(base: Path):
    p = base / "paper_map.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_table(g: dict, base: Path):
    nodes = {n["id"]: n for n in walk(g["root"]) if n.get("id")}
    paper_nodes = {nid: n for nid, n in nodes.items() if refs_of(n) and not n.get("deviation")}
    dev_nodes = {nid: n for nid, n in nodes.items() if isinstance(n.get("deviation"), dict)}
    if not paper_nodes and not dev_nodes:
        return None
    links = g.get("links") or []
    inv = load_inventory(base)
    order, inv_label = {}, {}
    if inv:
        k = 0
        for p in inv.get("papers", []):
            for it in p.get("items", []):
                order.setdefault(it["id"], k)
                inv_label.setdefault(it["id"], it.get("label", ""))
                k += 1
                for s in it.get("steps", []):
                    order.setdefault(f"{it['id']}.{s['n']}", k)
                    k += 1
        for r in inv.get("hp_check", []):
            order.setdefault(f"hp:{r['key']}", 10_000 + len(order))

    def first_src(n):
        for s in n.get("sources") or []:
            if isinstance(s, dict):
                loc = s.get("lines") or s.get("page") or s.get("section")
                return s.get("path", "") + (f":{loc}" if loc else "")
            return str(s)
        return ""

    rows = {}

    def row(ref):
        if ref not in rows:
            rows[ref] = {"ref": ref, "pretty": pretty(ref), "nodes": [], "implementers": [], "deviations": [],
                         "paper_text": inv_label.get(ref, "")}
        return rows[ref]

    for nid, n in paper_nodes.items():
        for ref in refs_of(n):
            row(ref)["nodes"].append({"id": nid, "label": n.get("label", nid)})
    attached = set()
    for l in links:
        a, b = l.get("from"), l.get("to")
        for pn, other in ((b, a), (a, b)):
            if pn in paper_nodes and other in nodes and other not in paper_nodes:
                for ref in refs_of(paper_nodes[pn]):
                    r = row(ref)
                    if other in dev_nodes:
                        if all(d["id"] != other for d in r["deviations"]):
                            r["deviations"].append(_dev(other, dev_nodes[other]))
                        attached.add(other)
                    elif l.get("type") == "implements":
                        if all(x["id"] != other for x in r["implementers"]):
                            r["implementers"].append({"id": other, "label": nodes[other].get("label", other),
                                                      "fidelity": l.get("fidelity"), "where": first_src(nodes[other])})
    for did, d in dev_nodes.items():
        for ref in refs_of(d):
            r = row(ref)
            if all(x["id"] != did for x in r["deviations"]):
                r["deviations"].append(_dev(did, d))
            attached.add(did)
    for r in rows.values():
        fids = [x["fidelity"] for x in r["implementers"]]
        if r["deviations"] or "deviates" in fids:
            r["status"] = "deviates"
        elif not r["implementers"]:
            r["status"] = "missing"
        elif "partial" in fids:
            r["status"] = "partial"
        elif fids and all(f == "exact" for f in fids):
            r["status"] = "exact"
        elif all(f in ("exact", "approx") for f in fids):
            r["status"] = "approx"
        else:
            r["status"] = "mapped"
        r["symbol"] = STATUS[r["status"]]
    extras = [_dev(did, d) for did, d in dev_nodes.items() if did not in attached]
    for e in extras:  # code-only: say which code node it hangs on
        e["code_nodes"] = [nodes[o].get("label", o) for l in links for o in (l.get("from"), l.get("to"))
                           if e["id"] in (l.get("from"), l.get("to")) and o != e["id"] and o in nodes]
    ordered = sorted(rows.values(), key=lambda r: sort_key(r["ref"], order))
    referenced = set(rows)
    unmapped, hp_open = [], []
    if inv:
        for p in inv.get("papers", []):
            for it in p.get("items", []):
                if it["kind"] in ("equation", "algorithm") and it["id"] not in referenced \
                        and not any(r.startswith(it["id"] + ".") for r in referenced):
                    unmapped.append({"ref": it["id"], "pretty": pretty(it["id"]), "where": it.get("where", ""),
                                     "text": (it.get("label") if it["kind"] == "algorithm" else it.get("text", ""))[:90]})
        for r in inv.get("hp_check", []):
            if r["status"] in ("≠", "≈ some differ") and f"hp:{r['key']}" not in referenced:
                pv = sorted({h["value"] for h in r["paper"]})
                cv = sorted({c["value"] for c in r["code"]})
                hp_open.append({"ref": f"hp:{r['key']}", "paper": pv, "code": cv,
                                "where": r["code"][0]["path"] + f":{r['code'][0]['line']}" if r["code"] else ""})
    counts = {s: sum(1 for r in ordered if r["status"] == s) for s in STATUS}
    return {"rows": ordered, "extras": extras, "unmapped": unmapped, "hp_open": hp_open, "counts": counts,
            "has_inventory": bool(inv)}


def _dev(nid, n):
    d = n.get("deviation") or {}
    return {"id": nid, "label": n.get("label", nid), "type": d.get("type", "other"), "paper": d.get("paper", ""),
            "code": d.get("code", ""), "impact": d.get("impact", ""), "status": n.get("evidence_status")}


def coverage_warnings(t: dict) -> list[str]:
    w = []
    if t["unmapped"]:
        w.append("paper↔code: equations/algorithms not mapped to any node yet: "
                 + ", ".join(f"{u['pretty']} ({u['where']})" for u in t["unmapped"][:12])
                 + (" …" if len(t["unmapped"]) > 12 else "")
                 + ". Map them, or record them as not implemented (a node with paper_ref and no implements link).")
    if t["hp_open"]:
        w.append("paper↔code: hyperparameters that differ between paper and code but have no node: "
                 + ", ".join(f"{h['ref']} (paper {', '.join(f'{v:g}' for v in h['paper'])} vs code "
                             f"{', '.join(f'{v:g}' for v in h['code'])})" for h in t["hp_open"][:10])
                 + ". Add a deviation node (paper_ref 'hp:<key>') or note why they don't matter.")
    return w


def md_lines(t: dict) -> list[str]:
    c = t["counts"]
    L = ["", "## Paper ↔ code", ""]
    L.append(f"{len(t['rows'])} paper items: {c['exact']} ✓ exact · {c['approx'] + c['mapped']} ≈ approx/mapped · "
             f"{c['partial']} ◐ partial · {c['deviates']} ⚠ deviate · {c['missing']} ∅ not in code"
             + (f" · {len(t['extras'])} code-only tricks" if t["extras"] else "")
             + (f" · {len(t['unmapped'])} paper items unmapped" if t["unmapped"] else ""))
    L += ["", "| Paper | Concept | Code | Status |", "|---|---|---|---|"]
    for r in t["rows"][:60]:
        d0 = r["deviations"][0] if r["deviations"] else {}
        concept = ", ".join(f"{n['label']} `{n['id']}`" for n in r["nodes"][:2]) or d0.get("paper") or r["paper_text"] or "—"
        code = "; ".join(f"{x['label']} `{x['id']}`" + (f" ({x['where']})" if x["where"] else "")
                         for x in r["implementers"][:2]) or (d0.get("code") if not r["nodes"] else "") or "—"
        status = f"{r['symbol']} {r['status']}" + (" — " + "; ".join(d["label"] for d in r["deviations"][:2]) if r["deviations"] else "")
        L.append(f"| {r['pretty']} | {concept} | {code} | {status} |".replace("\n", " "))
    devs = [d for r in t["rows"] for d in r["deviations"]]
    seen, uniq = set(), []
    for d in devs + t["extras"]:
        if d["id"] not in seen:
            seen.add(d["id"])
            uniq.append(d)
    if uniq:
        L += ["", "### Deviations (paper vs code)", ""]
        for d in uniq[:30]:
            L.append(f"- ⚠ **{d['label']}** `{d['id']}` ({d['type']})"
                     + (f" — paper: {d['paper']}" if d["paper"] else "")
                     + (f"; code: {d['code']}" if d["code"] else "")
                     + (f"; impact: {d['impact']}" if d["impact"] else ""))
    if t["unmapped"]:
        L.append("")
        L.append("_Not mapped yet:_ " + ", ".join(u["pretty"] for u in t["unmapped"][:20]))
    return L
