#!/usr/bin/env python3
"""Node-level evidence: check that the quotes backing each node still exist in their sources.

A node may carry
    "evidence": [{"path": "src/train.py", "quote": "loss = criterion(pred, target)"}]
where `quote` is a short verbatim excerpt (about 20–200 chars) from the file that supports the
node's summary. Code, text and every format doc_formats can extract (pdf, docx, pptx, …) work.

When a quote is first verified, a baseline is stored on the evidence item:
    ctx       hash of the text around the quote (3 non-empty lines either side, same page/slide)
    sig       hash of the node's claim (summary + details + path + quote) when it was verified
    verified  date of verification;  at  where it was found ("L42-44", "page 3")
Later checks compare against the baseline:
    ok        quote found and the text around it is unchanged (it may have moved; that's fine)
    changed   quote found but nearby text changed → re-read before trusting the node
    stale     quote no longer found, or the file is gone → the claim is no longer backed
The baseline is refreshed only when the node's claim or quote is edited (the agent re-checked
it) or when the build is run with --accept <ids|all>. Rebuilding never silently clears a flag.
Matching ignores whitespace, curly-vs-straight quotes and dash variants; if that fails, a loose
match on letters and digits only is tried (PDF hyphenation, line breaks inside words).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import doc_formats as df

CONTEXT = 3                 # non-empty lines of context on each side of the quote
MAX_FILE_BYTES = 8_000_000
BINARY_DOC = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".odt", ".odp", ".ods", ".epub"}
RANK = {"ok": 0, "changed": 1, "stale": 2, "error": 3}

_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                        "—": "-", "−": "-", " ": " ", "​": ""})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).translate(_TRANS)).strip()


def loose(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", s).casefold() if ch.isalnum())


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


class Source:
    """One file as lines (+ unit labels for documents), with lazily built search indexes."""

    def __init__(self, lines, labels=None):
        self.lines, self.labels = lines, labels
        self.normed = [norm(l) for l in lines]
        self.nonempty = [i for i, s in enumerate(self.normed) if s]
        self._idx = {}

    def index(self, mode):
        if mode not in self._idx:
            fn, sep = (norm, " ") if mode == "exact" else (loose, "")
            chars, owner = [], []
            for i in self.nonempty:
                s = self.normed[i] if mode == "exact" else fn(self.lines[i])
                if not s:
                    continue
                if chars and sep:
                    chars.append(sep)
                    owner.append(i)
                chars.extend(s)
                owner.extend([i] * len(s))
            self._idx[mode] = ("".join(chars), owner)
        return self._idx[mode]

    def where(self, a, b):
        if self.labels is None:
            return f"L{a + 1}" + (f"-{b + 1}" if b > a else "")
        la, lb = self.labels[a], self.labels[b]
        return la if la == lb else f"{la} – {lb}"

    def context_hash(self, a, b):
        pa = bisect.bisect_left(self.nonempty, a)
        pb = bisect.bisect_right(self.nonempty, b) - 1
        idx = self.nonempty[max(0, pa - CONTEXT): pb + CONTEXT + 1]
        if self.labels is not None:  # stay inside the page/slide/section of the quote
            idx = [i for i in idx if self.labels[i] in (self.labels[a], self.labels[b])]
        return _h(" ".join(self.normed[i] for i in idx))

    def locate(self, quote: str):
        for mode, fn in (("exact", norm), ("loose", loose)):
            q = fn(quote)
            if len(q) < 4:
                continue
            key, owner = self.index(mode)
            i = key.find(q)
            if i < 0:
                continue
            a, b = owner[i], owner[i + len(q) - 1]
            return {"found": True, "match": mode, "count": key.count(q), "at": self.where(a, b),
                    "ctx": self.context_hash(a, b)}
        return {"found": False}


def load_sources(path: Path) -> list:
    """Ways to read a file, tried in order: extracted document text (pdf, docx, html, ipynb …),
    then the raw text (code, markdown, or the HTML/notebook source). Empty if missing/unreadable."""
    if not path.is_file():
        return []
    out = []
    ext = path.suffix.lower()
    if ext in df.SUPPORTED and ext not in df.TEXT_EXT:
        d = df.extract(path)
        lines, labels = [], []
        for u in d["units"]:
            for l in u["text"].splitlines():
                lines.append(l)
                labels.append(u["label"])
        if lines:
            out.append(Source(lines, labels))
    if ext not in BINARY_DOC:
        try:
            if path.stat().st_size <= MAX_FILE_BYTES:
                out.append(Source(path.read_text(encoding="utf-8", errors="replace").splitlines()))
        except OSError:
            pass
    return out


def locate(sources: list, quote: str) -> dict:
    if not sources:
        return {"found": False, "missing_file": True}
    for s in sources:
        res = s.locate(quote)
        if res["found"]:
            return res
    return {"found": False}


def claim_sig(node: dict, ev: dict) -> str:
    return _h(json.dumps([node.get("summary", ""), node.get("details", ""), ev.get("path", ""),
                          norm(ev.get("quote", ""))], ensure_ascii=False))


def walk(node):
    yield node
    for c in node.get("children") or []:
        yield from walk(c)


def verify_graph(g: dict, root_dir: Path, mutate: bool = False, accept=None) -> dict:
    """Check every node's evidence against the files under root_dir.

    mutate=True stores baselines for new/edited/accepted evidence and writes status fields
    (`evidence_status` on nodes, `status`/`at` on evidence items, `meta.evidence`).
    accept: set of node ids, or {"all"}, whose current state should become the new baseline.
    """
    accept = accept or set()
    cache: dict = {}
    rep = {"errors": [], "warnings": [], "nodes": {}, "counts": {"ok": 0, "changed": 0, "stale": 0, "new": 0},
           "with_evidence": 0, "total": 0}
    today = time.strftime("%Y-%m-%d")

    def src(p):
        if p not in cache:
            cache[p] = load_sources(root_dir / p)
        return cache[p]

    for node in walk(g.get("root") or {}):
        rep["total"] += 1
        nid = node.get("id", "?")
        evs = node.get("evidence") or []
        if not evs:
            if mutate:
                node.pop("evidence_status", None)
            continue
        rep["with_evidence"] += 1
        worst, reasons = "ok", []
        for k, ev in enumerate(evs):
            where = f"node '{nid}' evidence[{k}]"
            if not isinstance(ev, dict) or not ev.get("path") or not ev.get("quote"):
                rep["errors"].append(f"{where}: needs both 'path' and 'quote'")
                worst = "error"
                continue
            path, quote = str(ev["path"]).replace("\\", "/"), str(ev["quote"])
            if re.match(r"^https?://", path):
                rep["warnings"].append(f"{where}: URLs can't be verified; cite a local file")
                continue
            if len(norm(quote)) < 12:
                rep["warnings"].append(f"{where}: quote is very short; pick 20–200 chars that are distinctive")
            elif len(quote) > 400:
                rep["warnings"].append(f"{where}: quote is {len(quote)} chars; 20–200 is enough")
            sig = claim_sig(node, ev)
            has_base = bool(ev.get("ctx")) and ev.get("sig") == sig and nid not in accept and "all" not in accept
            res = locate(src(path), quote)
            if not has_base:
                # new, edited, or accepted evidence must be verifiable right now
                if not res["found"]:
                    why = "file not found or unreadable" if res.get("missing_file") else "quote not found in the file"
                    rep["errors"].append(f"{where}: {why} ({path}). Copy the quote verbatim from the source "
                                         "(whitespace and line breaks may differ).")
                    worst = "error"
                    continue
                if res["count"] > 1:
                    rep["warnings"].append(f"{where}: quote occurs {res['count']}× in {path}; lengthen it so it is unique")
                rep["counts"]["new"] += 1
                status = "ok"
                if mutate:
                    ev.update({"ctx": res["ctx"], "sig": sig, "verified": today, "at": res["at"], "status": "ok"})
            else:
                if not res["found"]:
                    status = "stale"
                    reasons.append(f"{'file missing' if res.get('missing_file') else 'quote no longer found'}: "
                                   f"{path}" + (f" (was {ev['at']})" if ev.get("at") else "")
                                   + f" — “{_short(quote)}”")
                elif res["ctx"] != ev["ctx"]:
                    status = "changed"
                    reasons.append(f"text around the quote changed: {path} {res['at']} — “{_short(quote)}”")
                else:
                    status = "ok"
                if mutate:
                    ev["status"] = status
                    if res.get("found"):
                        ev["at"] = res["at"]
            rep["counts"][status] += 1
            if RANK[status] > RANK[worst]:
                worst = status
        if worst != "error":
            rep["nodes"][nid] = {"label": node.get("label", nid), "status": worst, "reasons": reasons}
            if mutate:
                node["evidence_status"] = worst
    if mutate:
        g.setdefault("meta", {})["evidence"] = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes_with_evidence": rep["with_evidence"],
            "ok": sum(1 for n in rep["nodes"].values() if n["status"] == "ok"),
            "changed": sum(1 for n in rep["nodes"].values() if n["status"] == "changed"),
            "stale": sum(1 for n in rep["nodes"].values() if n["status"] == "stale"),
        }
    return rep


def _short(s: str, n: int = 60) -> str:
    s = norm(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def flagged(rep: dict):
    """[(id, info)] for nodes that need re-checking, stale first."""
    out = [(nid, n) for nid, n in rep["nodes"].items() if n["status"] in ("stale", "changed")]
    return sorted(out, key=lambda kv: -RANK[kv[1]["status"]])


def report_lines(rep: dict, limit: int = 20) -> list[str]:
    nodes = rep["nodes"].values()
    ok = sum(1 for n in nodes if n["status"] == "ok")
    ch = sum(1 for n in nodes if n["status"] == "changed")
    st = sum(1 for n in nodes if n["status"] == "stale")
    L = [f"Evidence: {rep['with_evidence']}/{rep['total']} nodes have evidence · {ok} ok · {ch} changed · {st} stale"
         + (f" · {rep['counts']['new']} quotes newly verified" if rep["counts"]["new"] else "")]
    fl = flagged(rep)
    for nid, n in fl[:limit]:
        L.append(f"  ⚠ {n['status']:<7} {nid} ({n['label']}): " + "; ".join(n["reasons"][:2]))
    if len(fl) > limit:
        L.append(f"  … {len(fl) - limit} more")
    return L
