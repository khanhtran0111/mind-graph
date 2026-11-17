#!/usr/bin/env python3
"""Read a document as plain text, a piece at a time.

Turns pdf, docx, pptx, xlsx, odt/odp/ods, epub, html, ipynb, csv, rtf and plain-text formats into
text with unit markers ([page 3], [slide 4: Title], [sheet Sales], [§ Method]) so an agent can read
the outline first and then only the parts it needs.

Usage:
  python extract_text.py FILE                       # whole text, or outline + head if too long
  python extract_text.py FILE --outline             # structure only: units, headings, sizes
  python extract_text.py FILE --units 1-3,7         # selected pages / slides / sheets / sections
  python extract_text.py FILE --section "Method"    # units whose label or heading matches
  python extract_text.py FILE --grep "loss|reward"  # matching lines with their unit
  python extract_text.py FILE --max-chars 60000 --out notes.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_formats as df  # noqa: E402


def parse_ranges(spec: str, n: int) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)?", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or n)
            out += list(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            sys.exit(f"bad --units value: {part!r} (use e.g. 1-3,7)")
    return [i - 1 for i in out if 1 <= i <= n]


def header(path: Path, d: dict) -> str:
    words = sum(len(u["text"].split()) for u in d["units"])
    bits = [d["format"], f"{len(d['units'])} {d['unit']}{'s' if len(d['units']) != 1 else ''}"]
    if d["meta"].get("pages") and d["meta"]["pages"] != len(d["units"]):
        bits.append(f"{d['meta']['pages']} pages in file")
    bits.append(f"~{words:,} words")
    if d["meta"].get("backend"):
        bits.append("via " + d["meta"]["backend"])
    out = f"# {path.name} — " + ", ".join(bits)
    if d["title"]:
        out += f"\nTitle: {d['title']}"
    if d["note"]:
        out += f"\nNote: {d['note']}"
    return out


def outline_text(d: dict) -> str:
    L = []
    if d["outline"]:
        L.append("## Outline (heading → unit #)")
        for lv, t, ui in d["outline"][:300]:
            L.append(f"{'  ' * (min(lv, 6) - 1)}- {t}  [{ui + 1}]")
        if len(d["outline"]) > 300:
            L.append(f"- … {len(d['outline']) - 300} more headings")
    L.append(f"\n## Units ({len(d['units'])})")
    for i, u in enumerate(d["units"], 1):
        L.append(f"{i}. {u['label']} — {len(u['text'].split()):,} words")
        if i >= 400:
            L.append(f"… {len(d['units']) - 400} more")
            break
    return "\n".join(L)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--outline", action="store_true", help="structure only")
    ap.add_argument("--units", help="1-based unit numbers/ranges, e.g. 1-3,7")
    ap.add_argument("--section", help="regex matched against unit labels and headings (case-insensitive)")
    ap.add_argument("--grep", help="regex; print matching lines with their unit")
    ap.add_argument("--context", type=int, default=1, help="lines of context for --grep")
    ap.add_argument("--max-chars", type=int, default=40_000, help="cap on printed text (0 = no cap)")
    ap.add_argument("--out", help="write the text here instead of stdout")
    a = ap.parse_args()

    path = Path(a.file)
    if not path.is_file():
        sys.exit(f"not a file: {a.file}")
    d = df.extract(path)
    out = [header(path, d)]

    if a.outline:
        out.append(outline_text(d))
    elif a.grep:
        rx = re.compile(a.grep, re.I)
        hits = 0
        for u in d["units"]:
            lines = u["text"].splitlines()
            shown = set()
            for i, line in enumerate(lines):
                if rx.search(line):
                    for j in range(max(0, i - a.context), min(len(lines), i + a.context + 1)):
                        if j not in shown:
                            out.append(f"[{u['label']}] {lines[j]}")
                            shown.add(j)
                    out.append("")
                    hits += 1
        out.append(f"({hits} matching lines)")
    else:
        idx = list(range(len(d["units"])))
        if a.units:
            idx = parse_ranges(a.units, len(d["units"]))
        if a.section:
            rx = re.compile(a.section, re.I)
            sel = {i for i, u in enumerate(d["units"]) if rx.search(u["label"])}
            sel |= {ui for _, t, ui in d["outline"] if rx.search(t)}
            idx = [i for i in idx if i in sel]
            if not idx:
                out.append(f"No unit matches {a.section!r}.\n" + outline_text(d))
        body, used, cut = [], 0, None
        for i in idx:
            u = d["units"][i]
            chunk = f"\n[{u['label']}]\n{u['text']}\n"
            if a.max_chars and used + len(chunk) > a.max_chars:
                if not body:  # a single huge unit: print its head
                    body.append(chunk[: a.max_chars])
                cut = i
                break
            body.append(chunk)
            used += len(chunk)
        if cut is not None:
            out.append(outline_text(d))
            out.append("\n## Text (truncated)")
        out += body
        if cut is not None:
            out.append(f"\n… stopped at unit {cut + 1} of {len(d['units'])} (--max-chars {a.max_chars}). "
                       f"Continue with --units {cut + 1}-, or pick sections from the outline above.")

    text = "\n".join(out).rstrip() + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(text):,} chars to {a.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
