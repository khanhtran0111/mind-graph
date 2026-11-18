#!/usr/bin/env python3
"""Conceptual diff between two mind graphs (two commits, two releases, before/after a PR).

Nodes are matched by id (ids are meant to stay stable); leftover nodes with the same label on
both sides are matched as "id renamed". For every node the diff reports:
  added · removed · changed (label, summary, details, kind, importance, sources, paper_ref,
  deviation, evidence status) · moved (now under a different parent)
and for links: added · removed · changed (label / fidelity).

`merge()` returns one graph for the viewer: the new tree, with removed nodes put back where they
used to be, every node/link tagged with `diff`, and a `diff` summary block at the top level.
`md()` renders DIFF.md, a summary to paste into a PR description or hand to an agent.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ORDER = {"added": 0, "removed": 1, "changed": 2, "moved": 3}


def load(spec: str, cwd: Path | None = None):
    """A path to graph.json, or a git object 'REV:path' (e.g. main:.mindgraph/graph.json)."""
    p = Path(spec)
    if p.exists():
        parts = p.resolve().parts
        label = spec if len(spec) <= 48 else "…/" + "/".join(parts[-3:])
        return json.loads(p.read_text(encoding="utf-8")), label
    m = re.match(r"^([^:]{2,}):(.+)$", spec)  # 2+ chars before ':' so 'C:\…' is never a revision
    if m:
        r = subprocess.run(["git", "show", f"{m.group(1)}:{m.group(2)}"], cwd=cwd, capture_output=True)
        if r.returncode == 0:
            return json.loads(r.stdout.decode("utf-8")), spec
        raise SystemExit(f"git show {spec} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    raise SystemExit(f"not found: {spec} (give a graph.json path or REV:path)")


def index(g):
    out = {}

    def walk(n, parent, depth, pos):
        out[n["id"]] = {"node": n, "parent": parent, "depth": depth, "pos": pos}
        for i, c in enumerate(n.get("children") or []):
            walk(c, n["id"], depth + 1, i)
    walk(g["root"], None, 0, 0)
    return out


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _src_paths(n):
    out = set()
    for s in n.get("sources") or []:
        out.add(s.get("path") if isinstance(s, dict) else str(s))
    return {p for p in out if p}


def _refs(n):
    r = n.get("paper_ref")
    return sorted(r if isinstance(r, list) else ([r] if r else []))


def match(oi, ni):
    """old id → new id."""
    idmap = {i: i for i in oi if i in ni}
    lo = [i for i in oi if i not in idmap]
    ln = [i for i in ni if i not in set(idmap.values())]
    by_label_o, by_label_n = defaultdict(list), defaultdict(list)
    for i in lo:
        by_label_o[_norm(oi[i]["node"].get("label"))].append(i)
    for i in ln:
        by_label_n[_norm(ni[i]["node"].get("label"))].append(i)
    for lab, os_ in by_label_o.items():
        ns = by_label_n.get(lab, [])
        if lab and len(os_) == 1 and len(ns) == 1:
            idmap[os_[0]] = ns[0]
    return idmap


def node_changes(o, n, oi, ni, idmap, oid, nid):
    """Return (kind, details, extra) for a matched pair; kind None if nothing changed."""
    det, extra = [], {}
    on, nn = o["node"], n["node"]
    if oid != nid:
        det.append(f"id renamed: {oid} → {nid}")
    if _norm(on.get("label")) != _norm(nn.get("label")):
        det.append(f"label: “{on.get('label')}” → “{nn.get('label')}”")
    if _norm(on.get("summary")) != _norm(nn.get("summary")):
        ratio = difflib.SequenceMatcher(None, _norm(on.get("summary")), _norm(nn.get("summary"))).ratio()
        det.append("summary rewritten" if ratio < 0.6 else "summary edited")
        extra["old_summary"] = on.get("summary", "")
    if _norm(on.get("details")) != _norm(nn.get("details")):
        det.append("details edited")
    if (on.get("kind") or "concept") != (nn.get("kind") or "concept"):
        det.append(f"kind: {on.get('kind')} → {nn.get('kind')}")
    if (on.get("importance") or 2) != (nn.get("importance") or 2):
        det.append(f"importance: {on.get('importance') or 2} → {nn.get('importance') or 2}")
    os_, ns_ = _src_paths(on), _src_paths(nn)
    for p in sorted(ns_ - os_):
        det.append(f"+ source {p}")
    for p in sorted(os_ - ns_):
        det.append(f"− source {p}")
    if _refs(on) != _refs(nn):
        det.append(f"paper_ref: {', '.join(_refs(on)) or '—'} → {', '.join(_refs(nn)) or '—'}")
    od, nd = on.get("deviation"), nn.get("deviation")
    if bool(od) != bool(nd):
        det.append("deviation added" if nd else "deviation resolved")
    elif od and nd and json.dumps(od, sort_keys=True) != json.dumps(nd, sort_keys=True):
        det.append("deviation details changed")
    oe, ne = on.get("evidence_status"), nn.get("evidence_status")
    if oe != ne and (oe or ne):
        det.append(f"evidence: {oe or 'none'} → {ne or 'none'}")
    moved = False
    op = o["parent"]
    if op is not None and idmap.get(op, op) != n["parent"]:
        moved = True
        olab = oi[op]["node"].get("label", op)
        nlab = ni[n["parent"]]["node"].get("label", n["parent"]) if n["parent"] else "—"
        extra["moved_from"] = olab
        det.insert(0, f"moved: {olab} → {nlab}")
    if not det:
        return None, [], {}
    return ("moved" if moved and len(det) == 1 else "changed"), det, extra


def link_key(l, idmap=None):
    f, t = l.get("from"), l.get("to")
    if idmap is not None:
        f, t = idmap.get(f, f), idmap.get(t, t)
    return (f, t, l.get("type") or "related")


def diff(old, new):
    oi, ni = index(old), index(new)
    idmap = match(oi, ni)
    back = {v: k for k, v in idmap.items()}
    res = {"added": [], "removed": [], "changed": [], "moved": [], "links": {"added": [], "removed": [], "changed": []}}
    node_info = {}
    for nid, n in ni.items():
        if nid not in back:
            res["added"].append(nid)
            node_info[nid] = ("added", [], {})
        else:
            kind, det, extra = node_changes(oi[back[nid]], n, oi, ni, idmap, back[nid], nid)
            if kind:
                res[kind].append(nid)
                node_info[nid] = (kind, det, extra)
    for oid in oi:
        if oid not in idmap:
            res["removed"].append(oid)
    ol = {link_key(l, idmap): l for l in old.get("links") or []}
    nl = {link_key(l): l for l in new.get("links") or []}
    for k, l in nl.items():
        if k not in ol:
            res["links"]["added"].append(k)
        else:
            o = ol[k]
            ch = [f"{f}: {o.get(f) or '—'} → {l.get(f) or '—'}" for f in ("label", "fidelity") if (o.get(f) or "") != (l.get(f) or "")]
            if ch:
                res["links"]["changed"].append((k, ch))
    for k in ol:
        if k not in nl:
            res["links"]["removed"].append(k)
    # learning path & glossary (reported in DIFF.md only)
    olp = [idmap.get(s.get("node"), s.get("node")) for s in old.get("learning_path") or []]
    nlp = [s.get("node") for s in new.get("learning_path") or []]
    res["path"] = {"added": [x for x in nlp if x not in olp], "removed": [x for x in olp if x not in nlp],
                   "reordered": [x for x in nlp if x in olp] != [x for x in olp if x in nlp]}
    og = {_norm(t.get("term")) : t.get("term") for t in old.get("glossary") or []}
    ng = {_norm(t.get("term")): t.get("term") for t in new.get("glossary") or []}
    res["glossary"] = {"added": [ng[k] for k in ng if k not in og], "removed": [og[k] for k in og if k not in ng]}
    return res, oi, ni, idmap, node_info


def merge(old, new, old_label="old", new_label="new"):
    """New graph + removed nodes re-inserted, everything tagged with `diff`."""
    res, oi, ni, idmap, info = diff(old, new)
    g = copy.deepcopy(new)
    mi = index(g)
    for nid, (kind, det, extra) in info.items():
        n = mi[nid]["node"]
        n["diff"] = kind
        if det:
            n["diff_detail"] = det
        n.update({k: v for k, v in extra.items()})
    removed = set(res["removed"])

    def pruned(oid):
        n = copy.deepcopy({k: v for k, v in oi[oid]["node"].items() if k != "children"})
        n["diff"] = "removed"
        n["children"] = [pruned(c["id"]) for c in oi[oid]["node"].get("children") or [] if c["id"] in removed]
        return n

    tops = [oid for oid in res["removed"] if oi[oid]["parent"] not in removed]
    for oid in sorted(tops, key=lambda i: oi[i]["pos"]):
        parent_new = idmap.get(oi[oid]["parent"])
        target = mi[parent_new]["node"] if parent_new in mi else g["root"]
        kids = target.setdefault("children", [])
        kids.insert(min(oi[oid]["pos"], len(kids)), pruned(oid))
    # links: tag new ones, put removed ones back
    ladd = set(res["links"]["added"])
    lchg = {k: ch for k, ch in res["links"]["changed"]}
    for l in g.get("links") or []:
        k = link_key(l)
        if k in ladd:
            l["diff"] = "added"
        elif k in lchg:
            l["diff"] = "changed"
            l["diff_detail"] = lchg[k]
    rem = set(res["links"]["removed"])
    for l in old.get("links") or []:
        k = link_key(l, idmap)
        if k in rem:
            g.setdefault("links", []).append(dict(l, **{"from": k[0], "to": k[1], "diff": "removed"}))
    om, nm = old.get("meta") or {}, new.get("meta") or {}
    summary = {
        "old": {"label": old_label, "title": om.get("title"), "generated_at": om.get("generated_at"), "git_commit": om.get("git_commit")},
        "new": {"label": new_label, "title": nm.get("title"), "generated_at": nm.get("generated_at"), "git_commit": nm.get("git_commit")},
        "counts": {k: len(res[k]) for k in ("added", "removed", "changed", "moved")},
        "links": {k: len(v) for k, v in res["links"].items()},
        "areas": areas(res, oi, ni, idmap),
        "path": res["path"], "glossary": res["glossary"],
    }
    g["diff"] = summary
    g.setdefault("meta", {})["title"] = (nm.get("title") or "Mind graph") + " — changes"
    return g, res, oi, ni


def areas(res, oi, ni, idmap):
    """Changes per pillar (depth-1 ancestor), most changed first."""
    def pillar(idx, i):
        while idx[i]["depth"] > 1:
            i = idx[i]["parent"]
        return i if idx[i]["depth"] == 1 else None
    cnt = defaultdict(Counter)
    for k in ("added", "changed", "moved"):
        for nid in res[k]:
            p = pillar(ni, nid)
            if p:
                cnt[ni[p]["node"].get("label", p)][k] += 1
    for oid in res["removed"]:
        p = pillar(oi, oid)
        if p:
            cnt[oi[p]["node"].get("label", p)]["removed"] += 1
    return sorted(([lab, dict(c)] for lab, c in cnt.items()), key=lambda x: -sum(x[1].values()))


def md(g, res, oi, ni) -> str:
    d = g["diff"]
    c, lc = d["counts"], d["links"]

    def ver(v):
        bits = [v["label"]]
        if v.get("git_commit"):
            bits.append(f"commit {v['git_commit']}")
        if v.get("generated_at"):
            bits.append(str(v["generated_at"])[:10])
        return " · ".join(bits)

    def lab(idx, i):
        return idx[i]["node"].get("label", i) if i in idx else i

    L = [f"# Mind graph diff: {d['new'].get('title') or ''}", "",
         f"> {ver(d['old'])}  →  {ver(d['new'])}", "",
         f"**{c['added']} added · {c['removed']} removed · {c['changed']} changed · {c['moved']} moved** nodes; "
         f"links +{lc['added']} −{lc['removed']} ~{lc['changed']}"]
    if d["areas"]:
        L += ["", "## Areas most changed", ""]
        for name, cc in d["areas"][:10]:
            L.append(f"- **{name}**: " + ", ".join(f"{v} {k}" for k, v in sorted(cc.items(), key=lambda kv: ORDER[kv[0]])))
    if res["added"]:
        L += ["", "## Added", ""]
        for i in res["added"]:
            n = ni[i]
            if n["parent"] in res["added"]:
                continue  # shown with its parent
            sub = sum(1 for j in res["added"] if _under(ni, j, i)) - 1
            L.append(f"- **{n['node'].get('label')}** `{i}` (in {lab(ni, n['parent'])})"
                     + (f" — {n['node'].get('summary', '')[:160]}" if n["node"].get("summary") else "")
                     + (f" (+{sub} nodes below)" if sub > 0 else ""))
    if res["removed"]:
        L += ["", "## Removed", ""]
        for i in res["removed"]:
            o = oi[i]
            if o["parent"] in res["removed"]:
                continue
            sub = sum(1 for j in res["removed"] if _under(oi, j, i)) - 1
            L.append(f"- **{o['node'].get('label')}** `{i}` (was in {lab(oi, o['parent'])})"
                     + (f" — {o['node'].get('summary', '')[:160]}" if o["node"].get("summary") else "")
                     + (f" (+{sub} nodes below)" if sub > 0 else ""))
    mi = index(g)
    if res["changed"]:
        L += ["", "## Changed", ""]
        for i in res["changed"]:
            L.append(f"- **{ni[i]['node'].get('label')}** `{i}` — " + "; ".join(mi[i]["node"].get("diff_detail", [])[:6]))
    if res["moved"]:
        L += ["", "## Moved", ""]
        for i in res["moved"]:
            L.append(f"- **{ni[i]['node'].get('label')}** `{i}` — " + "; ".join(mi[i]["node"].get("diff_detail", [])))
    lk = res["links"]
    if any(lk.values()):
        L += ["", "## Relationships", ""]

        def ln(k):
            return f"{lab(ni, k[0]) if k[0] in ni else lab(oi, k[0])} —{k[2]}→ {lab(ni, k[1]) if k[1] in ni else lab(oi, k[1])}"
        L += [f"- + {ln(k)}" for k in lk["added"]]
        L += [f"- − {ln(k)}" for k in lk["removed"]]
        L += [f"- ~ {ln(k)}: " + "; ".join(ch) for k, ch in lk["changed"]]
    p, gl = d["path"], d["glossary"]
    if p["added"] or p["removed"] or p["reordered"] or gl["added"] or gl["removed"]:
        L += ["", "## Learning path & glossary", ""]
        if p["added"]:
            L.append("- path stops added: " + ", ".join(lab(ni, x) for x in p["added"]))
        if p["removed"]:
            L.append("- path stops removed: " + ", ".join(lab(oi, x) if x in oi else x for x in p["removed"]))
        if p["reordered"]:
            L.append("- path reordered")
        if gl["added"]:
            L.append("- glossary + " + ", ".join(gl["added"]))
        if gl["removed"]:
            L.append("- glossary − " + ", ".join(gl["removed"]))
    if not any(c.values()) and not any(lk.values()):
        L += ["", "No conceptual changes."]
    return "\n".join(L) + "\n"


def _under(idx, i, top):
    while i is not None:
        if i == top:
            return True
        i = idx[i]["parent"]
    return False
