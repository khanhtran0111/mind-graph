#!/usr/bin/env python3
"""Paper ↔ code mapping helper for research repositories.

Builds an inventory of what a paper specifies and finds where the code implements it, so the agent
can link every section / equation / algorithm step to the function that implements it and record
where the code departs from the paper.

Usage:
  python paper_code.py PAPER [PAPER ...] --code ROOT [--out ROOT/.mindgraph/paper_map.json]
PAPER: a .pdf, a .tex file (\\input/\\include are followed), or a folder of LaTeX sources
(e.g. an extracted arXiv source tarball).

Prints a budgeted report:
  1. Paper inventory: sections, numbered equations, algorithms with their steps, tables and
     hyperparameters stated in the text or tables. Each item has an id (sec:3.2, eq:7, alg:1,
     alg:1.4, tab:2, hp:clip) and a page (pdf) or line (tex).
  2. Hyperparameters, paper vs code: each stated value against same-named settings in code,
     configs and scripts (=, ≠, or not found). A ≠ is a deviation candidate.
  3. Code that cites the paper: comments/docstrings mentioning Eq./Algorithm/Section/Table/arXiv.
  4. Candidate implementations for each equation/algorithm: functions ranked by shared vocabulary
     (heuristic; confirm by reading).
The JSON written with --out is also read by build_mindgraph.py to report unmapped paper items.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_formats as df  # noqa: E402
import scan_project as sp  # noqa: E402

CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".gin", ".conf"}
SCRIPT_EXT = {".sh", ".bash", ".ps1", ".bat", ".slurm"}
MAX_CODE_BYTES = 1_500_000

# ------------------------------------------------------------------ numbers

NUM = (r"(?<![\w.])[-−–]?(?:\d+(?:[.,]\d+)?|\.\d+)"
       r"(?:\s*(?:[×x✕*]|\\times|\\cdot)\s*10\s*(?:\^|\*\*)?\s*\{?\s*[−–-]?\s*\d+\s*\}?|\s*[eE]\s*[−–-]?\s*\d+)?(?:\s*\\?%)?")
NUM_CODE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def parse_num(raw: str):
    s = re.sub(r"\s+", "", raw).replace("−", "-").replace("–", "-").replace("\\%", "%")
    pct = s.endswith("%")
    s = s.rstrip("%")
    m = re.match(r"^(-?[\d.,]+)(?:(?:[×x✕*]|\\times|\\cdot)10(?:\^|\*\*)?\{?(-?\d+)\}?|[eE](-?\d+))?$", s)
    if not m:
        return None
    mant = m.group(1)
    mant = mant.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", mant) else mant.replace(",", ".")
    try:
        v = float(mant)
    except ValueError:
        return None
    exp = m.group(2) or m.group(3)
    if exp:
        v *= 10 ** int(exp)
    return v / 100 if pct else v


def same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-12)


# ------------------------------------------------------------------ hyperparameter vocabulary
# key: (paper phrases, code identifier alternatives). Order matters: specific keys first, and a
# span of paper text is claimed by the first key that matches it.

HP = [
    ("adam_eps", r"adam\s*(?:epsilon|ε)", r"adam_eps(?:ilon)?|eps(?:ilon)?_adam|optim_eps"),
    ("target_kl", r"(?:target|desired)\s+kl|kl\s+target|d_?targ", r"target_kl|kl_target|desired_kl"),
    ("kl_coef", r"kl\s+(?:penalty|coefficient|weight)", r"kl_coef|kl_beta|beta_kl|kl_weight"),
    ("max_grad_norm", r"(?:max(?:imum)?\s+)?grad(?:ient)?\s+(?:norm|clip(?:ping)?)(?:\s+(?:threshold|value))?",
     r"max_grad_norm|grad_clip|clip_grad(?:_norm)?|max_norm|grad_norm_clip"),
    ("ent_coef", r"entropy\s+(?:coefficient|bonus|weight|regulari[sz]ation)|c_?2(?=\s*=)",
     r"ent_coef|entropy_coef|entropy_weight|ent_weight|entropy_beta|entropy_cost"),
    ("vf_coef", r"value\s+(?:function\s+)?(?:loss\s+)?(?:coefficient|weight)|vf\s+coef|c_?1(?=\s*=)",
     r"vf_coef|value_coef|value_loss_coef|critic_coef|value_weight|vf_weight"),
    ("learning_rate", r"learning[\s-]rate|step[\s-]?size|\blr\b|η(?=\s*=)",
     r"lr|learning_rate|learningrate|step_size|stepsize"),
    ("gamma", r"discount(?:\s+factor)?|γ(?=\s*=)", r"gamma|discount|discount_factor"),
    ("gae_lambda", r"gae\s*(?:parameter|lambda|λ)?|λ(?=\s*=)", r"gae_lambda|lam|lmbda|lambda_?|gae_lam|tau_gae"),
    ("clip", r"clip(?:ping)?\s*(?:range|parameter|coefficient|ratio|epsilon|threshold)|ε(?=\s*=)",
     r"clip_range|clip_param|clip_eps(?:ilon)?|eps(?:ilon)?_clip|clip_ratio|clip_coef|cliprange|clip"),
    ("batch_size", r"(?:mini-?)?batch[\s-]?size|minibatch\s+size", r"batch_size|minibatch_size|mini_batch_size|batchsize|bs"),
    ("epochs", r"(?:num(?:ber)?\.?\s*(?:of\s+)?)?(?:ppo\s+|optimi[sz]ation\s+)?epochs", r"n_epochs|epochs|ppo_epochs|noptepochs|update_epochs|num_epochs"),
    ("horizon", r"horizon|rollout\s+length|timesteps\s+per\s+(?:batch|update|actor)", r"n_steps|nsteps|horizon|rollout_length|num_steps|rollout_steps"),
    ("weight_decay", r"weight[\s-]decay|l2\s+(?:regulari[sz]ation|penalty)", r"weight_decay|wd|l2_reg|l2"),
    ("momentum", r"momentum", r"momentum"),
    ("dropout", r"dropout(?:\s+(?:rate|probability))?", r"dropout|drop_rate|p_drop|dropout_rate"),
    ("hidden_size", r"hidden\s+(?:size|units|dimension|dim|layer\s+size)|model\s+dimension|d_model",
     r"hidden_size|hidden_dim|d_model|n_hidden|hidden_units|hid_dim|embed_dim|dim_model"),
    ("num_layers", r"(?:number\s+of\s+)?(?:encoder\s+|decoder\s+|transformer\s+|hidden\s+)?layers", r"num_layers|n_layers|nlayers|depth|num_blocks"),
    ("num_heads", r"(?:number\s+of\s+)?(?:attention\s+)?heads", r"num_heads|n_heads|nhead|nheads|heads"),
    ("warmup", r"warm-?up(?:\s+steps)?", r"warmup|warmup_steps|warmup_iters|lr_warmup"),
    ("temperature", r"temperature", r"temperature|temp|tau_softmax"),
    ("tau", r"soft\s+(?:target\s+)?update(?:\s+coefficient)?|polyak(?:\s+averaging)?|target\s+smoothing|τ(?=\s*=)", r"tau|polyak|soft_update|target_update_tau"),
    ("buffer_size", r"(?:replay\s+)?buffer\s+(?:size|capacity)", r"buffer_size|replay_size|memory_size|capacity|replay_buffer_size"),
    ("chunk_size", r"(?:action\s+)?chunk(?:ing)?\s+size|chunk\s+length", r"chunk_size|num_queries|action_horizon|chunk_len"),
    ("kl_weight", r"kl\s+weight|β(?=\s*=)", r"kl_weight|beta"),
    ("train_steps", r"total\s+(?:training\s+)?(?:timesteps|steps|iterations)|training\s+(?:steps|iterations)", r"total_timesteps|max_steps|train_steps|num_iterations|n_iters|max_iters"),
    ("seeds", r"random\s+seeds|seeds", r"n_seeds|num_seeds"),
]
HP_PAPER = [(k, re.compile(
    rf"(?P<name>{p})(?:\s*\((?P<sym>[^()\n]{{1,15}})\))?[\s:]*(?:(?:of|is|was|were|set\s+to|equal\s+to|to|=|≈)\s*)*"
    rf"(?:\$?(?:[γλεαβτηθ]|\\[a-z]+)\$?\s*=?\s*|\$?[a-zA-Z]{{1,3}}\$?\s*=\s*)?(?P<val>{NUM})(?!\d)(?!\.\d)", re.I))
    for k, p, _ in HP]
HP_CODE = [(k, re.compile(rf"^(?:[a-z0-9]+_)*(?:{c})$", re.I)) for k, _, c in HP]

RE_ASSIGN = re.compile(rf"""(?P<name>[A-Za-z_][\w.\-]*)["']?\s*(?::|=|:=)\s*["']?(?P<val>{NUM_CODE})(?![\w.])""")
RE_ARGP = re.compile(rf"""add_argument\(\s*["'](?:--?)?(?P<name>[\w-]+)["'][^)\n]*?default\s*=\s*(?P<val>{NUM_CODE})""")
RE_CLI = re.compile(rf"""--(?P<name>[A-Za-z][\w.-]*)[= ](?P<val>{NUM_CODE})(?![\w.])""")


def hp_from_text(pages, labels):
    """pages: list[str]; labels: page/line labels. Returns hyperparameter mentions."""
    out = []
    for txt, lab in zip(pages, labels):
        flat = re.sub(r"\s+", " ", txt)
        taken = []
        for key, rx in HP_PAPER:
            for m in rx.finditer(flat):
                a, b = m.span()
                if any(a < y and x < b for x, y in taken):
                    continue
                v = parse_num(m.group("val"))
                tail = flat[b: b + 8]
                # "3 × 10?4" with a mangled exponent, or "3:" (a numbered step) → not a value
                if v is None or re.match(r"\s*(?:[×x✕*]|\\times)\s*10", tail) or re.match(r"\s*:", tail):
                    continue
                taken.append((a, b))
                snip = flat[max(0, a - 30): min(len(flat), b + 25)].strip()
                out.append({"key": key, "value": v, "raw": m.group("val").strip(), "where": lab, "snippet": snip})
    seen, uniq = set(), []
    for h in out:
        sig = (h["key"], round(h["value"], 12))
        if sig not in seen:
            seen.add(sig)
            uniq.append(h)
    return uniq


def hp_from_code(root: Path, files):
    found = defaultdict(list)
    for rel in files:
        ext = Path(rel).suffix.lower()
        if ext not in sp.CODE_EXT and ext not in CONFIG_EXT and ext not in SCRIPT_EXT:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_CODE_BYTES:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if len(line) > 400:
                continue
            hits = list(RE_ARGP.finditer(line)) + list(RE_CLI.finditer(line)) + list(RE_ASSIGN.finditer(line))
            for m in hits:
                name = re.split(r"[.]", m.group("name"))[-1].replace("-", "_").lower()
                for key, rx in HP_CODE:
                    if rx.match(name):
                        try:
                            v = float(m.group("val"))
                        except ValueError:
                            break
                        found[key].append({"value": v, "name": m.group("name"), "path": rel, "line": i,
                                           "text": line.strip()[:120]})
                        break
    for k in found:  # dedupe identical (file, line, value)
        seen, uniq = set(), []
        for h in found[k]:
            s = (h["path"], h["line"], h["value"])
            if s not in seen:
                seen.add(s)
                uniq.append(h)
        found[k] = uniq
    return found


# ------------------------------------------------------------------ paper parsing

MATHY = re.compile(r"[=≤≥<>∑∏∫∇∂±×·∈∼≈|^_√∞]|\b(?:log|exp|min|max|clip|arg|softmax|KL|mean|sum)\b|[α-ωΑ-Ω]")


def is_prose(t: str) -> bool:
    words = t.split()
    return len(words) >= 8 and sum(c.isalpha() for c in t) > 0.7 * len(t.replace(" ", "")) \
        and len(MATHY.findall(t)) <= 1


def pdf_items(path: Path):
    d = df.extract(path)
    pages = [u["text"] for u in d["units"]]
    items, eqs, algs = [], {}, {}
    for pi, text in enumerate(pages):
        lines = text.splitlines()
        for i, raw in enumerate(lines):
            s = raw.strip()
            # equations: "(7)" alone on a line, or "<math> (7)" at line end
            num, body, bstart = None, None, i   # bstart: first line of the equation body
            m = re.fullmatch(r"\((\d{1,3})\)", s)
            if m:
                num, prev, j = m.group(1), [], i - 1
                while j >= 0 and len(prev) < 3:
                    t = lines[j].strip()
                    if t:
                        if is_prose(t) or re.search(r"\(\d{1,3}\)$", t):
                            break
                        prev.insert(0, t)
                    j -= 1
                body, bstart = " ".join(prev), j + 1
            else:
                m = re.match(r"^(.*\S)\s+\((\d{1,3})\)$", s)
                if m and MATHY.search(m.group(1)) and not is_prose(m.group(1)):
                    num, body = m.group(2), m.group(1)
            if num and body and int(num) <= 300 and num not in eqs:
                ctx = []
                for t in reversed(lines[max(0, bstart - 6): bstart]):
                    if re.search(r"\(\d{1,3}\)\s*$", t.strip()):  # previous equation: stop here
                        break
                    if is_prose(t.strip()):
                        ctx.insert(0, t.strip())
                        if len(" ".join(ctx)) > 160:
                            break
                eqs[num] = {"id": f"eq:{num}", "kind": "equation", "label": f"Eq. {num}", "where": f"page {pi + 1}",
                            "page": pi + 1, "text": body[:220], "context": " ".join(ctx)[-240:]}
            # algorithms: "Algorithm 1 <title>" followed by numbered or keyword steps
            m = re.match(r"^Algorithm\s+(\d{1,2})\b\s*[:.]?\s*(.*)$", s)
            if m:
                n, title = m.group(1), m.group(2).strip()
                if not title and i + 1 < len(lines):
                    title = lines[i + 1].strip()
                steps = []
                for t in lines[i + 1: i + 60]:
                    t = t.strip()
                    if not t:
                        continue
                    if re.match(r"^(Algorithm\s+\d+|\d+(\.\d+)*\s+[A-Z][a-z]+)", t) and steps:
                        break
                    ms = re.match(r"^(\d{1,2})[:.)]\s*(.+)$", t)
                    if ms:
                        steps.append({"n": int(ms.group(1)), "text": ms.group(2)[:160]})
                    elif re.match(r"^(for|while|end|if|else|repeat|until|initiali[sz]e|input|output|require|ensure|"
                                  r"compute|update|sample|run|collect|optimi[sz]e|return|set|let|perform)\b", t, re.I):
                        steps.append({"n": len(steps) + 1, "text": t[:160]})
                    elif steps and is_prose(t):
                        break
                    elif steps and len(steps[-1]["text"]) < 160:
                        steps[-1]["text"] = (steps[-1]["text"] + " " + t)[:200]
                if len(steps) >= 2 and len(steps) > len(algs.get(n, {}).get("steps", [])):
                    algs[n] = {"id": f"alg:{n}", "kind": "algorithm", "label": f"Algorithm {n}" + (f": {title[:70]}" if title else ""),
                               "where": f"page {pi + 1}", "page": pi + 1, "text": title[:160], "steps": steps[:30]}
            m = re.match(r"^Table\s+(\d{1,2})\s*[:.]\s*(.+)$", s)
            if m and not any(it["id"] == f"tab:{m.group(1)}" for it in items):
                items.append({"id": f"tab:{m.group(1)}", "kind": "table", "label": f"Table {m.group(1)}",
                              "where": f"page {pi + 1}", "page": pi + 1, "text": m.group(2)[:160]})
    secs = []
    for lv, t, ui in d["outline"]:
        m = re.match(r"^(\d+(?:\.\d+){0,2})\.?\s+(.+)$", t) or re.match(r"^([A-H](?:\.\d+){0,2})\s+(.+)$", t)
        if m:
            sid = ("sec:" if m.group(1)[0].isdigit() else "app:") + m.group(1).lower()
            if not any(s["id"] == sid for s in secs):
                secs.append({"id": sid, "kind": "section", "label": f"§{m.group(1)} {m.group(2)[:60]}",
                             "where": f"page {ui + 1}", "page": ui + 1, "text": m.group(2)[:120]})
    eq_list = sorted(eqs.values(), key=lambda e: int(e["id"][3:]))
    alg_list = sorted(algs.values(), key=lambda a: int(a["id"][4:]))
    hps = hp_from_text(pages, [f"page {i + 1}" for i in range(len(pages))])
    return {"title": d["title"], "format": "pdf", "pages": d["meta"].get("pages"), "note": d["note"],
            "items": secs + eq_list + alg_list + items, "hyperparams": hps}


# ---- LaTeX

def tex_source(path: Path):
    """Main .tex text with \\input/\\include inlined; returns (lines, origin labels)."""
    if path.is_dir():
        mains = [p for p in sorted(path.rglob("*.tex")) if "\\documentclass" in (df.read_text_file(p) or "")]
        if not mains:
            raise SystemExit(f"no .tex with \\documentclass under {path}")
        path = mains[0]
    lines, labels, seen = [], [], set()

    def load(p: Path, depth=0):
        if p in seen or depth > 6:
            return
        seen.add(p)
        text = df.read_text_file(p) or ""
        for i, line in enumerate(text.splitlines(), 1):
            line = re.sub(r"(?<!\\)%.*$", "", line)
            m = re.search(r"\\(?:input|include|subfile)\{([^}]+)\}", line)
            if m:
                name = m.group(1) if m.group(1).endswith(".tex") else m.group(1) + ".tex"
                q = p.parent / name
                if not q.exists():  # \input paths are relative to the main file's folder
                    q = path.parent / name
                if q.exists():
                    load(q, depth + 1)
                    continue
            lines.append(line)
            labels.append(f"{p.name}:L{i}")
    load(path)
    return path, lines, labels


GREEK = {"gamma": "γ", "lambda": "λ", "epsilon": "ε", "varepsilon": "ε", "alpha": "α", "beta": "β",
         "tau": "τ", "eta": "η", "theta": "θ", "pi": "π", "sigma": "σ", "mu": "μ", "nabla": "∇"}


def tex_plain(s: str) -> str:
    s = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\{[^}]*\})?", " ", s)
    s = re.sub(r"\\(" + "|".join(sorted(GREEK, key=len, reverse=True)) + r")(?![a-zA-Z])", lambda m: GREEK[m.group(1)], s)
    s = s.replace("\\times", "×").replace("~", " ").replace("\\%", "%")
    s = re.sub(r"\\(?:textbf|textit|emph|text|mathrm|mathbf|mathcal|operatorname)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", s)
    s = s.replace("$", " ").replace("{", "").replace("}", "").replace("&", " ").replace("\\\\", " ")
    return re.sub(r"\s+", " ", s).strip()


def tex_items(path: Path):
    main, lines, labels = tex_source(path)
    items, secs = [], []
    counters = [0, 0, 0]
    appendix = False
    eqn = 0
    algn = 0
    tabn = 0
    title = None
    env, env_lines, env_start = None, [], 0
    for i, line in enumerate(lines):
        m = re.search(r"\\title\{(.+?)\}", line)
        if m and not title:
            title = tex_plain(m.group(1))
        if "\\appendix" in line:
            appendix, counters = True, [0, 0, 0]
        m = re.search(r"\\(section|subsection|subsubsection)(\*?)\{(.+)\}", line)
        if m and not m.group(2):
            lv = {"section": 0, "subsection": 1, "subsubsection": 2}[m.group(1)]
            counters[lv] += 1
            for k in range(lv + 1, 3):
                counters[k] = 0
            num = ".".join(str(c) for c in counters[: lv + 1])
            if appendix:
                num = chr(64 + counters[0]) + num[len(str(counters[0])):]
            sid = ("app:" if appendix else "sec:") + num.lower()
            secs.append({"id": sid, "kind": "section", "label": f"§{num} {tex_plain(m.group(3))[:60]}",
                         "where": labels[i], "text": tex_plain(m.group(3))[:120]})
        mb = re.search(r"\\begin\{(equation|align|gather|multline|eqnarray|algorithm|algorithm\*|table|table\*)\}", line)
        if mb and env is None:
            env, env_lines, env_start = mb.group(1), [], i
        if env:
            env_lines.append(line)
            if re.search(r"\\end\{" + re.escape(env) + r"\}", line):
                body = "\n".join(env_lines)
                lab = re.search(r"\\label\{([^}]+)\}", body)
                if env in ("equation", "align", "gather", "multline", "eqnarray"):
                    rows = [r for r in re.split(r"\\\\", body) if r.strip()] if env != "equation" else [body]
                    for r in rows:
                        if re.search(r"\\(nonumber|notag)", r):
                            continue
                        eqn += 1
                        prev = []
                        for l in reversed(lines[max(0, env_start - 6): env_start]):
                            if re.search(r"\\(end|begin)\{", l):  # stop at the previous environment
                                break
                            prev.insert(0, tex_plain(l))
                        ctx = " ".join(prev)[-240:]
                        items.append({"id": f"eq:{eqn}", "kind": "equation", "label": f"Eq. {eqn}", "where": labels[env_start],
                                      "text": re.sub(r"\s+", " ", re.sub(r"\\(begin|end)\{[^}]+\}|\\label\{[^}]+\}", "", r)).strip()[:220],
                                      "context": ctx, "tex_label": lab.group(1) if lab and len(rows) == 1 else None})
                elif env.startswith("algorithm"):
                    algn += 1
                    cap = re.search(r"\\caption\{(.+?)\}", body)
                    steps = []
                    for l in env_lines:
                        ms = re.match(r"\s*\\(State|STATE|For|FOR|ForAll|While|WHILE|If|IF|ElsIf|Else|Return|RETURN|Require|REQUIRE|Ensure|ENSURE|Repeat|Until|Function|Procedure)\b\*?(.*)$", l)
                        if ms:
                            kw = "" if ms.group(1).lower() == "state" else ms.group(1).lower() + " "
                            steps.append({"n": len(steps) + 1, "text": (kw + tex_plain(ms.group(2))).strip()[:160]})
                    items.append({"id": f"alg:{algn}", "kind": "algorithm",
                                  "label": f"Algorithm {algn}" + (f": {tex_plain(cap.group(1))[:70]}" if cap else ""),
                                  "where": labels[env_start], "text": tex_plain(cap.group(1))[:160] if cap else "",
                                  "steps": steps[:30], "tex_label": lab.group(1) if lab else None})
                else:
                    tabn += 1
                    cap = re.search(r"\\caption\{(.+?)\}", body)
                    items.append({"id": f"tab:{tabn}", "kind": "table", "label": f"Table {tabn}", "where": labels[env_start],
                                  "text": tex_plain(cap.group(1))[:160] if cap else ""})
                env = None
    # hyperparameters: prose + table rows, chunked by ~40 lines so locations stay useful
    chunks, chunk_labels = [], []
    plain = [tex_plain(l) for l in lines]
    # single lines first (exact locations), then 3-line windows for values split across lines;
    # hp_from_text keeps the first mention of each (key, value)
    for width in (1, 3):
        for k in range(len(lines)):
            if plain[k]:
                chunks.append(" ".join(plain[k:k + width]))
                chunk_labels.append(labels[k])
    hps = hp_from_text(chunks, chunk_labels)
    return {"title": title, "format": "tex", "main": str(main), "items": secs + items, "hyperparams": hps}


# ------------------------------------------------------------------ code side

RE_REF = re.compile(
    r"\b(?:(?:eq(?:uation)?s?|eqn)\.?\s*\(?(?P<eq>\d{1,3})\)?"
    r"|alg(?:orithm)?\.?\s*(?P<alg>\d{1,2})(?:[,\s]*(?:line|l\.?|step)\s*(?P<step>\d{1,2}))?"
    r"|sec(?:tion)?\.?\s*(?P<sec>\d+(?:\.\d+){0,2})"
    r"|table\s+(?P<tab>\d{1,2})"
    r"|appendix\s+(?P<app>[A-H](?:\.\d+)?)"
    r"|arxiv[:\s/]*(?P<arxiv>\d{4}\.\d{4,5}))"
    r"|§\s*(?P<sec2>\d+(?:\.\d+){0,2})", re.I)
COMMENT_START = re.compile(r"#|//|/\*|^\s*\*|^\s*--|^\s*%")


def ref_id(m) -> str | None:
    g = m.groupdict()
    if g["eq"]:
        return f"eq:{g['eq']}"
    if g["alg"]:
        return f"alg:{g['alg']}" + (f".{g['step']}" if g["step"] else "")
    if g["sec"] or g["sec2"]:
        return f"sec:{g['sec'] or g['sec2']}"
    if g["tab"]:
        return f"tab:{g['tab']}"
    if g["app"]:
        return f"app:{g['app'].lower()}"
    if g["arxiv"]:
        return f"arxiv:{g['arxiv']}"
    return None


def code_refs(root: Path, files):
    refs = defaultdict(list)
    for rel in files:
        ext = Path(rel).suffix.lower()
        if ext not in sp.CODE_EXT:
            continue
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_doc = False
        for i, line in enumerate(lines, 1):
            region = None
            if ext == ".py":
                q = line.count('"""') + line.count("'''")
                if in_doc or q:
                    region = line
                if q % 2 == 1:
                    in_doc = not in_doc
            if region is None:
                c = COMMENT_START.search(line)
                if c:
                    region = line[c.start():]
            if not region:
                continue
            for m in RE_REF.finditer(region):
                rid = ref_id(m)
                if rid:
                    refs[rid].append({"path": rel, "line": i, "text": line.strip()[:140]})
    return refs


STOP = set("""the and for with that this from where which are is we our use using each all be by of to in on as
at an it its can has have been than then also such these those into over under between both more most only other
same some when while how what eq equation algorithm section figure table follows given defined define denote let
step steps used time thus hence where let us one two new set see shown show note use uses via per here there
self return def none true false import torch np numpy tf float int range len print if else not or args kwargs
config cfg shape dim device cuda item append list dict data tensor tensors value_ get forward module nn super
init class pass raise assert lambda yield with as try except finally elif while break continue global""".split())
SYN = {"advantage": ["adv"], "probability": ["prob", "logp"], "value": ["val", "vf", "critic"], "policy": ["pi", "actor"],
       "gradient": ["grad"], "entropy": ["ent"], "reward": ["rew"], "return": ["ret"], "target": ["targ", "tgt"],
       "observation": ["obs"], "action": ["act"], "parameter": ["param"], "discount": ["gamma"], "normalize": ["norm"],
       "average": ["mean", "avg"], "expectation": ["mean"], "objective": ["loss", "obj"], "surrogate": ["surr"],
       "divergence": ["kl"], "clipped": ["clip"], "estimate": ["est"], "minibatch": ["batch"], "attention": ["attn"],
       "embedding": ["emb", "embed"], "temperature": ["temp"], "learning": ["lr"], "gaussian": ["normal", "gauss"],
       "logarithm": ["log"], "softmax": ["softmax"], "encoder": ["enc"], "decoder": ["dec"], "previous": ["prev"]}
SYMBOLS = {"γ": "gamma discount", "λ": "lambda lam gae", "ε": "epsilon eps clip", "π": "pi policy", "θ": "theta param",
           "σ": "sigma std", "μ": "mu mean", "τ": "tau", "α": "alpha", "β": "beta", "∇": "grad", "∑": "sum", "Σ": "sum",
           "Â": "adv advantage", "∥": "norm", "η": "lr", "ρ": "ratio rho", "δ": "delta td"}


def stem(w: str) -> str:
    """Crude: losses→loss, values→value, clipped→clip, estimates→estimate, policies→policy."""
    if w.endswith("ies") and len(w) >= 5:
        return w[:-3] + "y"
    if w.endswith("sses"):
        return w[:-2]
    for suf, mn in (("ing", 6), ("ed", 5)):
        if w.endswith(suf) and len(w) >= mn:
            w = w[: -len(suf)]
            return w[:-1] if len(w) >= 3 and w[-1] == w[-2] and w[-1] not in "lsz" else w
    if w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) >= 4:
        return w[:-1]
    return w


def tokens(s: str, expand=False) -> list[str]:
    for k, v in SYMBOLS.items():
        s = s.replace(k, f" {v} ")
    s = re.sub(r"\\([a-zA-Z]+)", r" \1 ", s)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", s):
        for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", w):
            p = p.lower()
            if len(p) >= 2 and p not in STOP and not p.isdigit():
                p = stem(p)
                out.append(p)
                if expand:
                    out += SYN.get(p, [])
    return out


def code_units(root: Path, files):
    units = []
    for rel in files:
        ext = Path(rel).suffix.lower()
        if ext not in sp.CODE_EXT:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_CODE_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if ext == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        a, b = node.lineno, getattr(node, "end_lineno", node.lineno + 30)
                        units.append({"path": rel, "symbol": node.name, "lines": f"{a}-{b}", "a": a, "b": b,
                                      "tok": set(tokens("\n".join(lines[a - 1:b])))})
                continue
        hdr = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|func|fn|(?:public|private|protected|static|inline|virtual|\w+\s*[*&]?)\s+)\s*([A-Za-z_]\w*)\s*\(")
        starts = [i for i, l in enumerate(lines) if hdr.match(l) and not l.strip().startswith(("if", "for", "while", "return", "switch"))]
        for k, s in enumerate(starts):
            e = min(starts[k + 1] if k + 1 < len(starts) else len(lines), s + 80)
            units.append({"path": rel, "symbol": hdr.match(lines[s]).group(1), "lines": f"{s + 1}-{e}", "a": s + 1, "b": e,
                          "tok": set(tokens("\n".join(lines[s:e])))})
    return units


def rank_candidates(items, units, refs):
    if not units:
        return {}
    dfreq = Counter(t for u in units for t in u["tok"])
    n = len(units)
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in dfreq.items()}
    for u in units:
        u["pre4"] = {t[:4]: t for t in u["tok"] if len(t) >= 4}
        u["short"] = {t for t in u["tok"] if len(t) <= 3}
        u["name_tok"] = set(tokens(u["symbol"]))  # words in the function name count double
    out = {}
    for it in items:
        if it["kind"] not in ("equation", "algorithm"):
            continue
        q = set(tokens(" ".join([it.get("text", ""), it.get("context", "")] + [s["text"] for s in it.get("steps", [])]), expand=True))
        if not q:
            continue
        scored = []
        for u in units:
            score, hit = 0.0, []
            for t in q:
                m = t if t in u["tok"] else (u["pre4"].get(t[:4]) if len(t) >= 4 else None)
                if m is None and len(t) >= 4 and t[:3] in u["short"]:
                    m = t[:3]
                if m:
                    score += idf.get(m, 1.0) * (2.0 if m in u["name_tok"] else 1.0)
                    hit.append(m)
            if len(hit) < 2:
                continue
            score /= math.sqrt(max(1.0, len(u["tok"]) / 40))
            if re.search(r"(^|/)tests?/|test_", u["path"]):
                score *= 0.6
            cited = [r for r in refs.get(it["id"], []) if r["path"] == u["path"] and u["a"] <= r["line"] <= u["b"]]
            if cited:
                score += 100
            scored.append((score, u, sorted(set(hit))[:8], bool(cited)))
        scored.sort(key=lambda x: -x[0])
        best = scored[0][0] if scored else 0
        out[it["id"]] = [{"path": u["path"], "symbol": u["symbol"], "lines": u["lines"], "score": round(s, 1),
                          "shared": hit, "cited": c} for s, u, hit, c in scored[:3] if s >= 0.35 * best]
    return out


# ------------------------------------------------------------------ report

def hp_check(papers, code_hp):
    rows = []
    for pi, p in enumerate(papers):
        by_key = defaultdict(list)
        for h in p["hyperparams"]:
            by_key[h["key"]].append(h)
        for key, ph in by_key.items():
            ch = code_hp.get(key, [])
            pv = [h["value"] for h in ph]
            if not ch:
                status = "not found in code"
            elif all(any(same(c["value"], v) for v in pv) for c in ch):
                status = "="
            elif any(any(same(c["value"], v) for v in pv) for c in ch):
                status = "≈ some differ"
            else:
                status = "≠"
            rows.append({"key": key, "paper_index": pi, "paper": ph, "code": ch[:8], "status": status})
    order = {"≠": 0, "≈ some differ": 1, "=": 2, "not found in code": 3}
    return sorted(rows, key=lambda r: (order[r["status"]], r["key"]))


def fmt(v: float) -> str:
    return f"{v:g}"


def render(res, max_lines=240):
    L = []
    for p in res["papers"]:
        kinds = Counter(it["kind"] for it in p["items"])
        L.append(f"# Paper ↔ code: {p.get('title') or p['path']}")
        L.append(f"{p['path']} ({p['format']}{', ' + str(p['pages']) + ' pages' if p.get('pages') else ''}) · "
                 + " · ".join(f"{v} {k}s" for k, v in kinds.items()) + f" · {len(p['hyperparams'])} hyperparameter mentions")
        if p.get("note"):
            L.append(f"⚠ {p['note']}")
    L.append(f"Code: {res['code_files']} files, {res['code_units']} functions indexed.")

    rows = res["hp_check"]
    if rows:
        L.append("\n## Hyperparameters: paper vs code (≠ rows are deviation candidates)")
        for r in rows[:30]:
            pv = ", ".join(sorted({fmt(h['value']) for h in r["paper"]}))
            where = r["paper"][0]["where"]
            cv = "; ".join(f"{fmt(c['value'])} {c['path']}:{c['line']}" for c in r["code"][:3]) or "—"
            if len(r["code"]) > 3:
                cv += f"; +{len(r['code']) - 3} more"
            L.append(f"- {r['status']:<3} hp:{r['key']}: paper {pv} ({where}: “{r['paper'][0]['snippet'][:70]}”) | code {cv}")

    for p in res["papers"]:
        eqs = [it for it in p["items"] if it["kind"] == "equation"]
        algs = [it for it in p["items"] if it["kind"] == "algorithm"]
        cand = res["candidates"]
        if eqs:
            L.append(f"\n## Equations ({len(eqs)})")
            for e in eqs[:40]:
                L.append(f"- {e['id']} ({e['where']}): {e['text'][:110]}")
                if e.get("context"):
                    L.append(f"    context: …{e['context'][-110:]}")
                cited = res["code_refs"].get(e["id"], [])
                if cited:
                    L.append("    cited in code: " + ", ".join(f"{c['path']}:{c['line']}" for c in cited[:4]))
                for c in cand.get(e["id"], [])[:3]:
                    L.append(f"    → {c['path']}:{c['lines']} {c['symbol']}() score {c['score']}" + (" [cites it]" if c["cited"] else "")
                             + f" shared: {', '.join(c['shared'][:6])}")
        if algs:
            L.append(f"\n## Algorithms ({len(algs)})")
            for a in algs:
                L.append(f"- {a['id']} ({a['where']}): {a['label']}")
                for s in a.get("steps", [])[:14]:
                    L.append(f"    {s['n']:>2}. {s['text'][:100]}")
                if len(a.get("steps", [])) > 14:
                    L.append(f"    … {len(a['steps']) - 14} more steps")
                for c in cand.get(a["id"], [])[:3]:
                    L.append(f"    → {c['path']}:{c['lines']} {c['symbol']}() score {c['score']}" + (" [cites it]" if c["cited"] else ""))
        secs = [it for it in p["items"] if it["kind"] == "section"]
        if secs:
            L.append(f"\n## Sections ({len(secs)}): " + " · ".join(s["label"][:40] for s in secs[:30]))
        tabs = [it for it in p["items"] if it["kind"] == "table"]
        if tabs:
            L.append("## Tables: " + " · ".join(f"{t['label']} {t['text'][:50]}" for t in tabs[:12]))

    if res["code_refs"]:
        L.append("\n## Code that cites the paper")
        for rid, hits in sorted(res["code_refs"].items()):
            for h in hits[:3]:
                L.append(f"- {rid:<10} {h['path']}:{h['line']}  {h['text'][:90]}")
    known = {it["id"] for p in res["papers"] for it in p["items"]}
    dangling = [rid for rid in res["code_refs"]
                if not rid.startswith("arxiv") and rid not in known and rid.split(".")[0] not in known]
    if dangling:
        L.append("Cited in code but not found in the paper inventory (other paper, different numbering, or "
                 "missed by extraction): " + ", ".join(sorted(dangling)[:15]))

    L.append("\n## Next")
    L.append("Read each candidate before linking it. In graph.json: paper-side nodes get `paper_ref` "
             "(Eq. 7, Alg. 1 L4, §3.2, hp:clip); code nodes link to them with `implements` + `fidelity`; "
             "every ≠, missing step or code-only trick becomes a `question` node with `deviation` "
             "(see references/graph_schema.md › Paper ↔ code).")
    if len(L) > max_lines:
        L = L[: max_lines - 1] + [f"… truncated; full detail in the JSON (--out)"]
    return "\n".join(L)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers", nargs="+", help="paper .pdf / .tex / LaTeX folder")
    ap.add_argument("--code", required=True, help="code root (usually the repo root)")
    ap.add_argument("--out", help="write full detail as JSON (default: <code>/.mindgraph/paper_map.json)")
    ap.add_argument("--max-lines", type=int, default=260)
    a = ap.parse_args()
    root = Path(a.code).resolve()
    files, _ = sp.list_files(root)
    paper_paths = []
    papers = []
    for raw in a.papers:
        pth = Path(raw).resolve()
        if not pth.exists():
            sys.exit(f"not found: {raw}")
        info = pdf_items(pth) if pth.suffix.lower() == ".pdf" else tex_items(pth)
        try:
            info["path"] = pth.relative_to(root).as_posix()
        except ValueError:
            info["path"] = str(pth)
        papers.append(info)
        paper_paths.append(pth)
    skip = set()
    for pth in paper_paths:
        try:
            rel = pth.relative_to(root).as_posix()
            skip |= {f for f in files if f == rel or f.startswith(rel.rstrip("/") + "/")}
        except ValueError:
            pass
    files = [f for f in files if f not in skip]
    refs = code_refs(root, files)
    units = code_units(root, files)
    all_items = [it for p in papers for it in p["items"]]
    res = {
        "papers": papers,
        "code_root": str(root),
        "code_files": sum(1 for f in files if Path(f).suffix.lower() in sp.CODE_EXT),
        "code_units": len(units),
        "hp_check": hp_check(papers, hp_from_code(root, files)),
        "code_refs": dict(refs),
        "candidates": rank_candidates(all_items, units, refs),
    }
    out = Path(a.out) if a.out else root / ".mindgraph" / "paper_map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(render(res, a.max_lines))
    print(f"\n(full detail: {out})")


if __name__ == "__main__":
    main()
