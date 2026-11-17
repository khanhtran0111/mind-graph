#!/usr/bin/env python3
"""Text and structure extraction for documents, standard library only.

Shared by scan_project.py (cheap probe of every document) and extract_text.py (reading one).

Every format is turned into the same shape:
  {"format", "unit", "title", "units": [{"label", "text"}], "outline": [(level, title, unit_idx)],
   "links": [raw targets], "meta": {...}, "note": str | None}
A *unit* is the natural reading chunk: a page (pdf), a slide (pptx/odp), a sheet (xlsx/ods/csv),
or a section between headings (everything else). Units are what `extract_text.py --units` selects.

Supported: md/mdx/markdown/txt/rst/tex/org/adoc, pdf, docx, pptx, xlsx/xlsm, odt/odp/ods, epub,
html/htm, ipynb, csv/tsv, rtf. PDF text needs one of: PyMuPDF (fitz), pypdf, or poppler's
`pdftotext` on PATH; without any of them the PDF is still listed, just without text.
"""
from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

TEXT_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst", ".tex", ".org", ".adoc", ".asciidoc"}
RICH_EXT = {".pdf", ".docx", ".pptx", ".odt", ".odp", ".epub", ".html", ".htm", ".rtf"}
DATA_EXT = {".csv", ".tsv", ".xlsx", ".xlsm", ".ods"}
NOTEBOOK_EXT = {".ipynb"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff",
             ".drawio", ".excalidraw"}
LEGACY_EXT = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx"}
DOC_EXT = TEXT_EXT | RICH_EXT
SUPPORTED = DOC_EXT | DATA_EXT | NOTEBOOK_EXT

MAX_TEXT_BYTES = 4_000_000      # plain-text files bigger than this are not read
MAX_ZIP_MEMBER = 80_000_000     # uncompressed size guard for docx/pptx/xlsx/epub parts
CHUNK_LINES = 250               # long docs without headings are split into chunks of this many lines

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "opf": "http://www.idpf.org/2007/opf",
    "cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def q(tag: str) -> str:
    """'w:p' -> '{namespace}p'"""
    pfx, local = tag.split(":")
    return "{%s}%s" % (NS[pfx], local)


# ---------------------------------------------------------------- shared helpers

def new_doc(fmt: str, unit: str = "section") -> dict:
    return {"format": fmt, "unit": unit, "title": None, "units": [], "outline": [], "links": [],
            "meta": {}, "note": None}


def read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace")


def zip_read(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_ZIP_MEMBER:
        return None
    return z.read(info)


def zip_xml(z: zipfile.ZipFile, name: str):
    data = zip_read(z, name)
    if data is None:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def rels_targets(z: zipfile.ZipFile, rels_name: str, base_dir: str) -> dict:
    """Relationship id -> normalized zip member path."""
    root = zip_xml(z, rels_name)
    out = {}
    if root is None:
        return out
    for r in root.iter(q("rel:Relationship")):
        tgt = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            continue
        path = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join(base_dir, tgt))
        out[r.get("Id")] = (path, r.get("Type", ""))
    return out


def core_title(z: zipfile.ZipFile) -> str | None:
    root = zip_xml(z, "docProps/core.xml")
    if root is not None:
        t = root.find(q("dc:title"))
        if t is not None and t.text and t.text.strip():
            return t.text.strip()
    return None


def clean_title(t: str | None) -> str | None:
    if not t:
        return None
    t = re.sub(r"\s+", " ", str(t)).strip()
    if len(t) < 3 or re.match(r"(?i)^(microsoft (word|powerpoint)|untitled|presentation\d*|slide \d+)\b", t):
        return None
    if re.search(r"\.(docx?|pptx?|pdf|tex|dvi)$", t, re.I):
        return None
    return t[:150]


def from_blocks(doc: dict, blocks: list, chunk_long: bool = True) -> dict:
    """blocks: (level, text) with level 1..6 for headings and 0 for body text.

    Splits into section units at the top one or two heading levels and fills doc['outline'].
    """
    levels = [lv for lv, _ in blocks if lv]
    split_at = (min(levels) + 1) if levels else 0
    units, label, lines = [], None, []

    def flush():
        if label is not None or any(s.strip() for s in lines):
            units.append({"label": label or "(start)", "text": "\n".join(lines).strip()})

    for lv, t in blocks:
        t = t.rstrip()
        if not t:
            continue
        if lv and lv <= split_at:
            flush()
            label, lines = "§ " + t[:90], ["#" * lv + " " + t]
        else:
            lines.append(("#" * lv + " " + t) if lv else t)
        if lv:
            doc["outline"].append((lv, t[:120], len(units)))
    flush()
    units = [u for u in units if u["text"] or u["label"] != "(start)"]
    if chunk_long and len(units) == 1 and not levels:
        all_lines = units[0]["text"].splitlines()
        if len(all_lines) > CHUNK_LINES * 1.5:
            units = [{"label": f"lines {i + 1}-{min(i + CHUNK_LINES, len(all_lines))}",
                      "text": "\n".join(all_lines[i:i + CHUNK_LINES])}
                     for i in range(0, len(all_lines), CHUNK_LINES)]
            doc["unit"] = "chunk"
    doc["units"] = units
    return doc


# Plain-text heading heuristic (PDFs, .txt): numbered headings and well-known section names.
KNOWN_SECTIONS = re.compile(
    r"^(abstract|introduction|background|related work|preliminaries|method(s|ology)?|approach|"
    r"model|experiments?|evaluation|results?|discussion|limitations|conclusions?|future work|"
    r"references|bibliography|acknowledge?ments|appendix( [a-z])?|summary|overview|"
    r"tóm tắt|giới thiệu|mở đầu|tổng quan|phương pháp|thực nghiệm|kết quả|thảo luận|kết luận|"
    r"tài liệu tham khảo|phụ lục|lời cảm ơn|lời nói đầu)$", re.I)
RE_NUM_HEAD = re.compile(r"^(?:(?:chapter|chương|phần|part|section)\s+)?((?:\d{1,2}(?:\.\d{1,2}){0,3})|[IVX]{1,5}|[A-H])[.):]?\s+(\S.{1,78})$", re.I)
RE_CHAPTER = re.compile(r"^(chapter|chương|phần|part)\s+(\d+|[IVX]+)\b.{0,70}$", re.I)


def plain_heading(line: str):
    """Return (level, title) if a line of extracted plain text looks like a section heading."""
    s = line.strip()
    if not (3 <= len(s) <= 90) or s.endswith((",", ";")):
        return None
    if KNOWN_SECTIONS.match(s.rstrip(":").strip()):
        return 1, s.rstrip(":")
    if RE_CHAPTER.match(s):
        return 1, s
    m = RE_NUM_HEAD.match(s)
    if m:
        num, rest = m.group(1), m.group(2).strip()
        letters = sum(ch.isalpha() for ch in rest)
        words = rest.split()
        if (letters >= 0.6 * len(rest.replace(" ", "")) and len(words) <= 12 and rest[0].isalpha()
                and rest[0].isupper() and not rest.endswith(".")
                and not re.search(r"\d+[.,]\d+", rest)):
            if re.fullmatch(r"[A-H]", num) and len(words) > 6:
                return None
            level = num.count(".") + 1 if num[0].isdigit() else 1
            return min(level, 3), s
    return None


def plain_outline(pages: list[str], limit_repeat: int = 2):
    """Headings found in page texts → [(level, title, page_idx)], ignoring running headers."""
    cands, counts = [], {}
    for i, txt in enumerate(pages):
        for line in txt.splitlines():
            h = plain_heading(line)
            if h:
                cands.append((h[0], h[1], i))
                counts[h[1].lower()] = counts.get(h[1].lower(), 0) + 1
    out, seen = [], set()
    for lv, t, i in cands:
        key = t.lower()
        if counts[key] > limit_repeat or key in seen:
            continue
        seen.add(key)
        out.append((lv, t, i))
    return out


RE_MD_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
RE_WIKI = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
RE_TICK_PATH = re.compile(r"`([\w./\\-]+\.[A-Za-z0-9]{1,6})`")


def text_links(text: str) -> list[str]:
    """Raw link targets: Markdown links, [[wiki links]], and `path/like.ext` mentions."""
    out = []
    for m in RE_MD_LINK.finditer(text):
        t = m.group(1)
        if not re.match(r"^(https?:|mailto:|#|data:)", t):
            out.append(t.split("#")[0])
    out += ["[[" + m.group(1).strip() for m in RE_WIKI.finditer(text)]
    out += [m.group(1) for m in RE_TICK_PATH.finditer(text) if "/" in m.group(1)]
    return [t for t in out if t]


# ---------------------------------------------------------------- plain text formats

RE_RST_UNDER = re.compile(r"^([=\-~^\"'`#*+]){3,}\s*$")


def text_blocks(ext: str, text: str) -> list:
    blocks = []
    lines = text.splitlines()
    in_code = False
    rst_levels: list[str] = []
    for i, line in enumerate(lines):
        if ext in (".md", ".mdx", ".markdown", ".ipynb"):
            if line.strip().startswith(("```", "~~~")):
                in_code = not in_code
                blocks.append((0, line))
                continue
            m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if m:
                blocks.append((len(m.group(1)), m.group(2)))
                continue
        elif ext == ".rst":
            if i + 1 < len(lines) and line.strip() and RE_RST_UNDER.match(lines[i + 1]) \
                    and len(lines[i + 1].strip()) >= len(line.strip()) - 1:
                ch = lines[i + 1].strip()[0]
                if ch not in rst_levels:
                    rst_levels.append(ch)
                blocks.append((min(rst_levels.index(ch) + 1, 6), line.strip()))
                continue
            if RE_RST_UNDER.match(line) and blocks and blocks[-1][0]:
                continue
        elif ext == ".tex":
            m = re.match(r"^\s*\\(part|chapter|section|subsection|subsubsection)\*?\{(.+)\}", line)
            if m:
                lv = {"part": 1, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}[m.group(1)]
                blocks.append((lv, re.sub(r"\\[a-zA-Z]+\{?|[{}]", "", m.group(2)).strip()))
                continue
            m = re.match(r"^\s*\\title\{(.+)\}", line)
            if m:
                blocks.append((0, "Title: " + m.group(1)))
                continue
        elif ext == ".org":
            m = re.match(r"^(\*{1,6})\s+(.+)$", line)
            if m:
                blocks.append((len(m.group(1)), m.group(2)))
                continue
        elif ext in (".adoc", ".asciidoc"):
            m = re.match(r"^(={1,6})\s+(.+)$", line)
            if m:
                blocks.append((len(m.group(1)), m.group(2)))
                continue
        blocks.append((0, line))
    if ext == ".txt" and not any(lv for lv, _ in blocks):
        # plain notes: use the numbered-heading heuristic, but only if it finds a real structure
        found = [(i, plain_heading(t)) for i, (_, t) in enumerate(blocks)]
        hits = [(i, h) for i, h in found if h]
        if 2 <= len(hits) <= max(3, len(blocks) // 4):
            for i, h in hits:
                blocks[i] = (h[0], h[1])
    return blocks


def extract_textfile(path: Path, ext: str) -> dict:
    doc = new_doc(ext.lstrip("."))
    text = read_text_file(path)
    if text is None:
        doc["note"] = "too large or unreadable as text"
        return doc
    if ext in (".md", ".mdx", ".markdown"):
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if fm:
            m = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", fm.group(1), re.M)
            if m:
                doc["title"] = m.group(1)
            doc["meta"]["frontmatter"] = True
    from_blocks(doc, text_blocks(ext, text))
    if not doc["title"] and ext != ".txt" and doc["outline"] and doc["outline"][0][0] == 1             and doc["outline"][0][2] <= 1:
        doc["title"] = doc["outline"][0][1]
    doc["links"] = text_links(text)
    return doc


# ---------------------------------------------------------------- PDF

def _pdf_pages_fitz(path: Path, limit: int | None):
    import fitz  # PyMuPDF
    d = fitz.open(str(path))
    n = d.page_count
    lim = n if limit is None else min(n, limit)
    pages = [d[i].get_text() for i in range(lim)]
    toc = [(lv, t, max(p - 1, 0)) for lv, t, p in d.get_toc(simple=True)]
    return n, pages, (d.metadata or {}).get("title"), toc, "pymupdf"


def _pdf_pages_pypdf(path: Path, limit: int | None):
    from pypdf import PdfReader
    r = PdfReader(str(path))
    n = len(r.pages)
    lim = n if limit is None else min(n, limit)
    pages = []
    for i in range(lim):
        try:
            pages.append(r.pages[i].extract_text() or "")
        except Exception:
            pages.append("")
    toc = []

    def walk(items, depth=1):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
            elif hasattr(it, "title"):
                try:
                    pg = r.get_destination_page_number(it)
                except Exception:
                    pg = 0
                toc.append((depth, str(it.title), pg))
    try:
        walk(r.outline)
    except Exception:
        pass
    title = None
    try:
        title = (r.metadata or {}).get("/Title")
    except Exception:
        pass
    return n, pages, title, toc, "pypdf"


def _pdf_pages_poppler(path: Path, limit: int | None):
    exe = shutil.which("pdftotext")
    if not exe:
        raise ImportError("pdftotext not found")
    n, title = None, None
    info_exe = shutil.which("pdfinfo")
    if info_exe:
        try:
            r = subprocess.run([info_exe, "-enc", "UTF-8", str(path)], capture_output=True, timeout=30)
            info = r.stdout.decode("utf-8", errors="replace")
            m = re.search(r"^Pages:\s+(\d+)", info, re.M)
            n = int(m.group(1)) if m else None
            m = re.search(r"^Title:\s+(.+)$", info, re.M)
            title = m.group(1).strip() if m else None
        except Exception:
            pass
    cmd = [exe, "-enc", "UTF-8", "-q"]
    if limit is not None:
        cmd += ["-f", "1", "-l", str(limit)]
    r = subprocess.run(cmd + [str(path), "-"], capture_output=True, timeout=180)
    pages = r.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return n or len(pages), pages, title, [], "pdftotext"


def pdf_backend_hint() -> str:
    return ("no PDF text backend: `pip install pymupdf` or `pip install pypdf`, or install poppler "
            "(pdftotext). An agent that can read PDFs natively can open the file directly instead.")


def extract_pdf(path: Path, quick: bool = False) -> dict:
    doc = new_doc("pdf", "page")
    limit = None
    errors = []
    for fn in (_pdf_pages_fitz, _pdf_pages_pypdf, _pdf_pages_poppler):
        try:
            n, pages, title, toc, backend = fn(path, 12 if quick else None)
            if quick and n <= 80 and len(pages) < n:   # small enough: read it all for a real outline
                n, pages, title, toc, backend = fn(path, None)
            break
        except ImportError:
            continue
        except Exception as e:  # damaged/encrypted file: try the next backend
            errors.append(f"{fn.__name__[11:]}: {type(e).__name__}")
            continue
    else:
        doc["note"] = ("unreadable PDF (" + "; ".join(errors) + ")") if errors else pdf_backend_hint()
        size = path.stat().st_size if path.exists() else 0
        doc["meta"]["pages_guess"] = len(re.findall(rb"/Type\s*/Page[^s]", path.read_bytes())) if size < 30_000_000 else None
        return doc
    doc["meta"].update({"pages": n, "backend": backend, "pages_read": len(pages)})
    doc["units"] = [{"label": f"page {i + 1}", "text": t.strip()} for i, t in enumerate(pages)]
    if toc:
        doc["outline"] = [(lv, re.sub(r"\s+", " ", t).strip()[:120], p) for lv, t, p in toc if t.strip()]
        doc["meta"]["outline_from"] = "bookmarks"
    else:
        doc["outline"] = plain_outline(pages)
        doc["meta"]["outline_from"] = "heading heuristic"
    words = sum(len(t.split()) for t in pages)
    if not words and n:
        doc["note"] = "no extractable text (scanned images?) — needs OCR or visual reading"
    title = clean_title(title)
    if not title and pages:
        for line in pages[0].splitlines():
            line = line.strip()
            if len(line) >= 8 and sum(c.isalpha() for c in line) > len(line) * 0.6 and not plain_heading(line):
                title = line[:150]
                break
    doc["title"] = title
    return doc


# ---------------------------------------------------------------- Office Open XML

def extract_docx(path: Path) -> dict:
    doc = new_doc("docx")
    with zipfile.ZipFile(path) as z:
        styles = {}
        st_root = zip_xml(z, "word/styles.xml")
        if st_root is not None:
            for st in st_root.iter(q("w:style")):
                sid = st.get(q("w:styleId"))
                name_el = st.find(q("w:name"))
                name = (name_el.get(q("w:val")) if name_el is not None else "") or ""
                lvl_el = st.find(f"{q('w:pPr')}/{q('w:outlineLvl')}")
                m = re.match(r"(?i)^heading\s*(\d)$", name)
                if m:
                    styles[sid] = int(m.group(1))
                elif name.lower() == "title":
                    styles[sid] = "title"
                elif lvl_el is not None and (lvl_el.get(q("w:val")) or "9").isdigit() and int(lvl_el.get(q("w:val"))) < 9:
                    styles[sid] = int(lvl_el.get(q("w:val"))) + 1
        root = zip_xml(z, "word/document.xml")
        if root is None:
            doc["note"] = "word/document.xml missing or unreadable"
            return doc
        body = root.find(q("w:body"))
        blocks = []

        def para_text(p):
            out = []
            for el in p.iter():
                if el.tag == q("w:t") and el.text:
                    out.append(el.text)
                elif el.tag == q("w:tab"):
                    out.append("\t")
                elif el.tag in (q("w:br"), q("w:cr")):
                    out.append("\n")
            return "".join(out).strip()

        for el in list(body) if body is not None else []:
            if el.tag == q("w:p"):
                txt = para_text(el)
                if not txt:
                    continue
                ppr = el.find(q("w:pPr"))
                level = 0
                if ppr is not None:
                    ps = ppr.find(q("w:pStyle"))
                    lv = styles.get(ps.get(q("w:val"))) if ps is not None else None
                    ol = ppr.find(q("w:outlineLvl"))
                    if lv == "title":
                        doc["title"] = doc["title"] or txt
                    elif lv:
                        level = lv
                    elif ol is not None and (ol.get(q("w:val")) or "9").isdigit() and int(ol.get(q("w:val"))) < 9:
                        level = int(ol.get(q("w:val"))) + 1
                    if not level and ppr.find(q("w:numPr")) is not None:
                        txt = "- " + txt
                blocks.append((min(level, 6), txt.replace("\n", " ") if level else txt))
            elif el.tag == q("w:tbl"):
                for tr in el.iter(q("w:tr")):
                    cells = [" ".join(para_text(p) for p in tc.iter(q("w:p"))).strip() for tc in tr.iter(q("w:tc"))]
                    if any(cells):
                        blocks.append((0, "| " + " | ".join(cells) + " |"))
        doc["title"] = clean_title(doc["title"]) or clean_title(core_title(z))
    from_blocks(doc, blocks)
    doc["links"] = text_links("\n".join(t for _, t in blocks))
    return doc


def extract_pptx(path: Path) -> dict:
    doc = new_doc("pptx", "slide")
    with zipfile.ZipFile(path) as z:
        rels = rels_targets(z, "ppt/_rels/presentation.xml.rels", "ppt")
        pres = zip_xml(z, "ppt/presentation.xml")
        order = []
        if pres is not None:
            for s in pres.iter(q("p:sldId")):
                rid = s.get(q("r:id"))
                if rid in rels:
                    order.append(rels[rid][0])
        if not order:
            order = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, name in enumerate(order, 1):
            root = zip_xml(z, name)
            if root is None:
                continue
            title, lines = None, []
            for sp in root.iter(q("p:sp")):
                ph = sp.find(f".//{q('p:nvPr')}/{q('p:ph')}")
                ph_type = ph.get("type") if ph is not None else None
                paras = []
                for para in sp.iter(q("a:p")):
                    t = "".join(x.text or "" for x in para.iter(q("a:t"))).strip()
                    if t:
                        lvl = para.find(q("a:pPr"))
                        indent = int(lvl.get("lvl", "0")) if lvl is not None and (lvl.get("lvl") or "0").isdigit() else 0
                        paras.append(t if ph_type in ("title", "ctrTitle") else "  " * indent + "- " + t)
                if ph_type in ("title", "ctrTitle") and paras:
                    title = title or " ".join(paras)
                elif ph_type not in ("sldNum", "dt", "ftr"):
                    lines += paras
            for tbl in root.iter(q("a:tbl")):
                for tr in tbl.iter(q("a:tr")):
                    cells = ["".join(x.text or "" for x in tc.iter(q("a:t"))).strip() for tc in tr.iter(q("a:tc"))]
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
            base = posixpath.dirname(name)
            srels = rels_targets(z, f"{base}/_rels/{posixpath.basename(name)}.rels", base)
            notes = []
            for tgt, typ in srels.values():
                if typ.endswith("/notesSlide"):
                    nroot = zip_xml(z, tgt)
                    if nroot is not None:
                        for sp in nroot.iter(q("p:sp")):
                            ph = sp.find(f".//{q('p:nvPr')}/{q('p:ph')}")
                            if ph is not None and ph.get("type") == "body":
                                notes += ["".join(x.text or "" for x in p.iter(q("a:t"))).strip() for p in sp.iter(q("a:p"))]
            notes = [n for n in notes if n]
            text = (f"# {title}\n" if title else "") + "\n".join(lines)
            if notes:
                text += "\nNotes: " + " ".join(notes)
            doc["units"].append({"label": f"slide {i}" + (f": {title[:70]}" if title else ""), "text": text.strip()})
            if title:
                doc["outline"].append((1, title[:120], i - 1))
        doc["title"] = clean_title(core_title(z)) or (doc["outline"][0][1] if doc["outline"] else None)
        doc["meta"]["slides"] = len(doc["units"])
    return doc


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def extract_xlsx(path: Path, max_rows: int = 200) -> dict:
    doc = new_doc("xlsx", "sheet")
    with zipfile.ZipFile(path) as z:
        shared = []
        ss = zip_read(z, "xl/sharedStrings.xml")
        if ss is not None:
            for _, el in ET.iterparse(io.BytesIO(ss)):
                if el.tag == q("s:si"):
                    shared.append("".join(t.text or "" for t in el.iter(q("s:t"))))
                    el.clear()
        rels = rels_targets(z, "xl/_rels/workbook.xml.rels", "xl")
        wb = zip_xml(z, "xl/workbook.xml")
        sheets = []
        if wb is not None:
            for s in wb.iter(q("s:sheet")):
                rid = s.get(q("r:id"))
                if rid in rels:
                    sheets.append((s.get("name"), rels[rid][0]))
        for name, member in sheets:
            data = zip_read(z, member)
            if data is None:
                doc["units"].append({"label": f"sheet {name}", "text": "(too large to read)"})
                continue
            rows, n_rows, dim = [], 0, None
            for _, el in ET.iterparse(io.BytesIO(data)):
                if el.tag == q("s:dimension"):
                    dim = el.get("ref")
                elif el.tag == q("s:row"):
                    n_rows += 1
                    if len(rows) < max_rows:
                        vals = {}
                        for c in el.iter(q("s:c")):
                            t, v = c.get("t"), c.find(q("s:v"))
                            if t == "s" and v is not None and (v.text or "").isdigit():
                                val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                            elif t == "inlineStr":
                                val = "".join(x.text or "" for x in c.iter(q("s:t")))
                            else:
                                val = v.text if v is not None and v.text is not None else ""
                            vals[_col_index(c.get("r", "A"))] = val.replace("\t", " ").replace("\n", " ")
                        if vals and any(x.strip() for x in vals.values()):
                            rows.append("\t".join(vals.get(i, "") for i in range(min(max(vals) + 1, 60))))
                    el.clear()
            more = f"\n… {n_rows - len(rows)} more rows" if n_rows > len(rows) else ""
            doc["units"].append({"label": f"sheet {name}", "text": "\n".join(rows) + more})
            doc["outline"].append((1, f"{name} ({dim or '?'}, {n_rows} rows)", len(doc["units"]) - 1))
        doc["title"] = clean_title(core_title(z))
        doc["meta"]["sheets"] = len(sheets)
    return doc


# ---------------------------------------------------------------- OpenDocument, EPUB, HTML

def extract_odf(path: Path, ext: str) -> dict:
    kind = {".odt": "section", ".odp": "slide", ".ods": "sheet"}[ext]
    doc = new_doc(ext.lstrip("."), kind)
    with zipfile.ZipFile(path) as z:
        root = zip_xml(z, "content.xml")
        meta = zip_xml(z, "meta.xml")
    if root is None:
        doc["note"] = "content.xml missing"
        return doc
    if meta is not None:
        t = meta.find(f".//{q('dc:title')}")
        doc["title"] = clean_title(t.text if t is not None else None)
    if ext == ".odt":
        blocks = []
        for el in root.iter():
            if el.tag == q("text:h"):
                blocks.append((int(el.get(q("text:outline-level"), "1") or 1), "".join(el.itertext()).strip()))
            elif el.tag == q("text:p"):
                blocks.append((0, "".join(el.itertext()).strip()))
        from_blocks(doc, blocks)
    elif ext == ".odp":
        for i, page in enumerate(root.iter(q("draw:page")), 1):
            lines = ["".join(p.itertext()).strip() for p in page.iter(q("text:p"))]
            lines = [l for l in lines if l]
            title = lines[0] if lines else None
            doc["units"].append({"label": f"slide {i}" + (f": {title[:70]}" if title else ""), "text": "\n".join(lines)})
            if title:
                doc["outline"].append((1, title[:120], i - 1))
    else:
        for tbl in root.iter(q("table:table")):
            rows = []
            for tr in tbl.iter(q("table:table-row")):
                cells = ["".join(c.itertext()).strip() for c in tr.iter(q("table:table-cell"))]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    rows.append("\t".join(cells))
            name = tbl.get(q("table:name"), "?")
            doc["units"].append({"label": f"sheet {name}", "text": "\n".join(rows[:200])})
            doc["outline"].append((1, f"{name} ({len(rows)} rows)", len(doc["units"]) - 1))
    return doc


class _HTMLBlocks(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "blockquote", "pre", "dd", "dt",
             "table", "ul", "ol", "header", "footer", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"script", "style", "noscript", "svg", "template", "nav"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf, self.level, self.skip = [], [], 0, 0
        self.title, self.in_title, self.links = None, False, []

    def flush(self):
        t = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.buf)).strip()
        if t:
            self.blocks.append((self.level, t.replace("\n", " ") if self.level else t))
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.flush()
            if re.fullmatch(r"h[1-6]", tag):
                self.level = int(tag[1])
            elif tag == "li":
                self.buf.append("- ")
        elif tag in ("td", "th"):
            self.buf.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not re.match(r"^(https?:|mailto:|#|javascript:|data:)", href):
                self.links.append(href.split("#")[0])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in self.BLOCK:
            self.flush()
            self.level = 0

    def handle_data(self, data):
        if self.in_title:
            self.title = (self.title or "") + data.strip()
        elif not self.skip:
            self.buf.append(data)


def html_blocks(text: str):
    p = _HTMLBlocks()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    p.flush()
    return p


def extract_html(path: Path) -> dict:
    doc = new_doc("html")
    text = read_text_file(path)
    if text is None:
        doc["note"] = "too large or unreadable"
        return doc
    p = html_blocks(text)
    doc["title"] = clean_title(p.title)
    from_blocks(doc, p.blocks)
    doc["links"] = [l for l in p.links if l]
    return doc


def extract_epub(path: Path) -> dict:
    doc = new_doc("epub")
    blocks = []
    with zipfile.ZipFile(path) as z:
        cont = zip_xml(z, "META-INF/container.xml")
        rf = cont.find(f".//{q('cnt:rootfile')}") if cont is not None else None
        opf_path = rf.get("full-path") if rf is not None else None
        opf = zip_xml(z, opf_path) if opf_path else None
        if opf is None:
            doc["note"] = "no OPF package found"
            return doc
        base = posixpath.dirname(opf_path)
        t = opf.find(f".//{q('dc:title')}")
        doc["title"] = clean_title(t.text if t is not None else None)
        manifest = {it.get("id"): it.get("href") for it in opf.iter(q("opf:item"))}
        for ref in opf.iter(q("opf:itemref")):
            href = manifest.get(ref.get("idref"))
            if not href:
                continue
            data = zip_read(z, posixpath.normpath(posixpath.join(base, href)))
            if data:
                blocks += html_blocks(data.decode("utf-8", errors="replace")).blocks
    from_blocks(doc, blocks)
    return doc


# ---------------------------------------------------------------- notebooks, tables, rtf

def extract_ipynb(path: Path) -> dict:
    doc = new_doc("ipynb")
    raw = read_text_file(path)
    try:
        nb = json.loads(raw or "")
    except Exception:
        doc["note"] = "invalid notebook JSON"
        return doc
    parts, n_code = [], 0
    for cell in nb.get("cells", []):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else str(src)
        if cell.get("cell_type") == "markdown":
            parts.append(src)
        elif cell.get("cell_type") == "code" and src.strip():
            n_code += 1
            lines = src.splitlines()
            parts.append("```\n" + "\n".join(lines[:40]) + ("\n# … %d more lines" % (len(lines) - 40) if len(lines) > 40 else "") + "\n```")
    text = "\n\n".join(parts)
    from_blocks(doc, text_blocks(".ipynb", text))
    doc["meta"]["code_cells"] = n_code
    doc["links"] = text_links(text)
    if doc["outline"] and doc["outline"][0][0] == 1:
        doc["title"] = doc["outline"][0][1]
    return doc


def extract_csv(path: Path, max_rows: int = 200) -> dict:
    doc = new_doc(path.suffix.lower().lstrip("."), "sheet")
    text = read_text_file(path)
    if text is None:
        # big CSV: still count rows and show the head
        head, n = [], 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if n < max_rows:
                        head.append(line)
                    n += 1
        except OSError:
            doc["note"] = "unreadable"
            return doc
        text = "".join(head)
    else:
        n = text.count("\n")
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        delim = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|").delimiter
    except Exception:
        pass
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if i >= max_rows:
            break
        rows.append("\t".join(c.strip() for c in row))
    header = rows[0].split("\t") if rows else []
    doc["units"] = [{"label": "table", "text": "\n".join(rows) + (f"\n… {n - len(rows)} more rows" if n > len(rows) else "")}]
    doc["outline"] = [(1, f"{len(header)} columns, ~{n} rows: " + ", ".join(header[:12]), 0)]
    doc["meta"].update({"rows": n, "columns": header[:40]})
    return doc


def extract_rtf(path: Path) -> dict:
    doc = new_doc("rtf")
    raw = read_text_file(path)
    if raw is None:
        doc["note"] = "too large or unreadable"
        return doc
    t = re.sub(r"\{\\\*[^{}]*\}", "", raw)
    t = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), t)
    t = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), t)
    t = re.sub(r"\\(par|line)\b ?", "\n", t)
    t = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", t)
    from_blocks(doc, text_blocks(".txt", t))
    return doc


# ---------------------------------------------------------------- entry points

def extract(path: Path, quick: bool = False) -> dict:
    """Extract one document. quick=True caps work on big PDFs (used by the scanner)."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return extract_textfile(path, ext)
        if ext == ".pdf":
            return extract_pdf(path, quick)
        if ext == ".docx":
            return extract_docx(path)
        if ext == ".pptx":
            return extract_pptx(path)
        if ext in (".xlsx", ".xlsm"):
            return extract_xlsx(path, 6 if quick else 200)
        if ext in (".odt", ".odp", ".ods"):
            return extract_odf(path, ext)
        if ext == ".epub":
            return extract_epub(path)
        if ext in (".html", ".htm"):
            return extract_html(path)
        if ext == ".ipynb":
            return extract_ipynb(path)
        if ext in (".csv", ".tsv"):
            return extract_csv(path, 6 if quick else 200)
        if ext == ".rtf":
            return extract_rtf(path)
    except zipfile.BadZipFile:
        d = new_doc(ext.lstrip("."))
        d["note"] = "not a valid zip container (corrupt, encrypted, or an old binary format renamed)"
        return d
    except Exception as e:
        d = new_doc(ext.lstrip("."))
        d["note"] = f"extraction failed: {type(e).__name__}: {str(e)[:120]}"
        return d
    d = new_doc(ext.lstrip(".") or "unknown")
    if ext in LEGACY_EXT:
        d["note"] = (f"legacy binary format; convert first, e.g. `soffice --headless --convert-to "
                     f"{LEGACY_EXT[ext].lstrip('.')} \"{path.name}\"`")
    else:
        d["note"] = "unsupported format"
    return d


def lead_sentence(doc: dict, n: int = 170) -> str:
    """First real prose line: tells the scanner what a document is about without reading it."""
    for u in doc["units"][:6]:
        for line in u["text"].splitlines():
            s = line.strip().lstrip("-*>| ").strip()
            if (len(s.split()) >= 7 and not s.startswith(("#", "```", "<", "!", "[", "\\", "{")) and
                    sum(c.isalpha() for c in s) > len(s) * 0.55 and not re.match(r"^(title|author|date|tags?):", s, re.I)):
                return s[: n - 1] + "…" if len(s) > n else s
    return ""


def probe(path: Path, quick: bool = True, outline_limit: int = 14) -> dict:
    """Compact facts for the scanner: format, title, size, outline, lead line, raw links."""
    d = extract(path, quick=quick)
    words = sum(len(u["text"].split()) for u in d["units"])
    rec = {"format": d["format"]}
    meta = d["meta"]
    if d["format"] == "pdf" and meta.get("pages"):
        rec["pages"] = meta["pages"]
        if meta.get("pages_read") and meta["pages_read"] < meta["pages"]:
            words = int(words * meta["pages"] / max(meta["pages_read"], 1))
            rec["words_estimated"] = True
    elif meta.get("pages_guess"):
        rec["pages"] = meta["pages_guess"]
    for k in ("slides", "sheets", "rows", "columns", "code_cells"):
        if meta.get(k):
            rec[k] = meta[k]
    if d["title"]:
        rec["title"] = d["title"]
    rec["words"] = words
    outline = [("  " * (min(lv, 4) - 1)) + t[:80] for lv, t, _ in d["outline"] if lv <= 3]
    rec["outline"] = outline[:outline_limit]
    if len(outline) > outline_limit:
        rec["outline_more"] = len(outline) - outline_limit
    lead = lead_sentence(d)
    if lead:
        rec["lead"] = lead
    if d["links"]:
        rec["links"] = d["links"][:200]
    if d["note"]:
        rec["note"] = d["note"]
    if d["format"] == "pdf" and meta.get("outline_from"):
        rec["outline_from"] = meta["outline_from"]
    return rec
