---
name: mind-graph
description: Read a codebase, a folder of documents (PDF, Word, PowerPoint, Excel, Markdown/Obsidian notes, HTML, EPUB, notebooks), papers, or any mix of them, and turn the understanding into an interactive mind graph (HTML) plus a compact MINDGRAPH.md that later sessions load instead of re-reading everything. Use this whenever someone wants to understand, learn, onboard onto, summarize, map, visualize or get an overview of a repo, project, library, documentation set, course materials, research/paper collection, meeting notes or any folder of files — "explain this project's structure", "help me learn this codebase", "summarize everything in this folder", "draw a mind map / concept map / knowledge graph of these docs", "sơ đồ tư duy", "mind map cho project này", "tóm tắt các tài liệu trong thư mục này" — and whenever a .mindgraph/ folder already exists in the project, even if the user doesn't say "mind graph".
---

# Mind Graph

Turn a project — code, documents, papers, notes, or a mix — into two things built from one
source of truth:

1. **For the human** – `mindgraph.html`: an interactive, collapsible mind graph with summaries,
   clickable source locations (file, line, page, slide, sheet), cross-links and a suggested
   learning path. Works offline, one file.
2. **For future agents** – `MINDGRAPH.md`: a compact outline that a
   new session reads *instead of* re-exploring the project (typically 2–4k tokens).

Both come from `.mindgraph/graph.json`, which you write. The scripts do the deterministic parts
(scanning, text extraction, validating, rendering) so your tokens go into understanding.

```
mind-graph/
├── scripts/scan_project.py     # cheap inventory: tree, document outlines, symbols, imports, links, hubs
├── scripts/extract_text.py     # read one document by page / slide / sheet / section (pdf, docx, pptx, …)
├── scripts/doc_formats.py      # shared extractors (standard library only; used by the two above)
├── scripts/build_mindgraph.py  # validate graph.json → mindgraph.html, MINDGRAPH.md, mindmap.mmd
├── assets/viewer.html          # interactive viewer template (no external dependencies)
└── references/graph_schema.md  # graph.json format + what makes a good mind graph — read before writing
```

Requirements: Python 3.9+. Everything works with the standard library. For PDF text, one of
`pymupdf`, `pypdf` or poppler's `pdftotext` is used if present; without them PDFs are still
listed (and you can open them with your own file-reading tool if it reads PDFs).

## Step 0 — Reuse before you read

If `<project>/.mindgraph/MINDGRAPH.md` exists, read it first. That is the point of this skill.
Then run the scan with `--since-graph` (below): it lists files changed since the graph was
built. If nothing relevant changed, answer from the existing graph. If some areas changed,
update only those branches of `graph.json` and rebuild — don't regenerate the whole thing.

## Step 1 — Scan (cheap, no reading yet)

```bash
python <skill>/scripts/scan_project.py <path> [<path> ...] --out <root>/.mindgraph/scan.json [--since-graph] [--exclude 'archive/*']
```

`<path>` can be a repo, any folder, one file, or several files/folders (their common parent
becomes the root, and `.mindgraph/` goes there). It prints a budgeted markdown summary and writes
full detail to `scan.json`. The summary starts with a **mode**:

- **CODE** – tree, entry points, hub files (high import in-degree), external deps, symbols.
- **DOCUMENTS** – documents grouped by folder with format, size (pages/slides/sheets/words), title,
  a lead sentence and the heading outline; links between notes (`[[wiki]]`, relative links) with
  the most linked-to notes; data files with their columns; images/diagrams; a total word count so
  you can tell whether reading everything fits your budget.
- **MIXED** – both, plus which docs point into which code files.

Read the ⚠ lines: legacy `.doc/.ppt/.xls` must be converted
(`soffice --headless --convert-to docx FILE`), scanned PDFs have no text (OCR or view the pages),
and a missing PDF backend means outlines are absent.

## Step 2 — Read selectively

The mind graph is a learning aid, not a full index. Read what explains the project and stop
when each pillar has enough substance to summarize truthfully.

**Code**: 1) README / top-level docs (intent) → 2) entry points (main flow) → 3) hub files (core
abstractions) → 4) one representative file per remaining area; the scan's symbol list often
suffices for a leaf.

**Documents** (pdf, docx, pptx, xlsx, odt, epub, html, ipynb, csv, md, txt…): read *outline first,
then the parts that define the ideas*, with `extract_text.py`:

```bash
python <skill>/scripts/extract_text.py FILE --outline            # headings → unit numbers, sizes
python <skill>/scripts/extract_text.py FILE --units 1-3          # pages / slides / sheets / sections
python <skill>/scripts/extract_text.py FILE --section "Method"   # units whose heading matches
python <skill>/scripts/extract_text.py FILE --grep "reward|loss" # find where a term is discussed
```

Output is capped at `--max-chars` (default 40k) and tells you where it stopped. Plain Markdown
and text can also be read directly. Per document type:

- **Papers / reports**: abstract, introduction, the method's core sections, conclusion; skim
  experiments for the headline result.
- **Slide decks**: the outline (slide titles) is usually the skeleton; read the slides behind
  each section title, plus speaker notes.
- **Spreadsheets / CSV**: sheet names, header row and a few rows tell you what the data is; don't
  read every row.
- **Note vaults** (Obsidian, wiki, Markdown folders): start at index/MOC/README and the most
  linked-to notes from the scan; the link structure often *is* the concept structure.
- **Meeting notes, specs, plans**: decisions, owners, open questions, dates.
- **Images/diagrams**: view the ones that look like architecture or overview figures.

**Budget**: total under ~30k words → read nearly everything; larger → outlines for all, full
reading for core documents, samples for the rest; very large (hundreds of documents or >300k
words) → work theme by theme and say which areas are only shallowly mapped. Keep a list of what
you actually read; it goes into `meta.sources_read` so readers know how deep the map is.

## Step 3 — Design the graph, then write graph.json

Read `references/graph_schema.md` now — it has the exact format, the quality rules and patterns
for each kind of project or document collection. The key decisions, briefly:

- **Organize by concepts a learner needs, not by folder or file.** Pillars like "Data pipeline",
  "Model", "Evaluation" — or for documents "Core ideas", "Methods", "Decisions", "Open
  questions" — teach better than `src/`, `docs/` or one node per file. Point to files from inside
  those concepts through `sources`, with `page` / `slide` / `sheet` / `section` / `lines` locators.
- **Root → 3–8 pillars → topics → leaves**, usually 3–4 levels. 3–9 children per node keeps it
  scannable; group further if a node has more.
- **Every node earns a 1–2 sentence summary** that says what it is *and why it matters*. Labels
  stay short (≤ 4–5 words) because they are drawn on the canvas.
- **Cross-links carry the relationships the tree can't**: data flow, calls, depends-on,
  explains (doc → code), implements (code → paper), contrasts (two approaches/documents that
  disagree). These turn a tree into a mental model — add the important ones, not every import.
- **Learning path**: 5–12 ordered stops through the graph with a one-line "why now".
- **Only claim what you read.** Every `sources` path must exist; the build checks this. Mark
  inferred or uncertain points in the summary ("likely", "not verified") rather than guessing.
- **For a document collection, every relevant document should be reachable** from some node's
  `sources` (or named as skipped in `meta.scope`); the build reports coverage against scan.json.

Write it to `<root>/.mindgraph/graph.json`. Write labels, summaries and notes in the language the
user is working in (ask only if unclear); keep identifiers, paths and document titles verbatim.

## Step 4 — Build and check

```bash
python <skill>/scripts/build_mindgraph.py <root>/.mindgraph/graph.json
```

This validates the graph (unique ids, valid links, source paths that exist, page/slide formats,
size warnings, document coverage) and writes `mindgraph.html`, `MINDGRAPH.md` and `mindmap.mmd`
next to it. Fix every error and reconsider the warnings, then rebuild. `--check` validates
without writing.

## Step 5 — Hand it over

- Give the user `mindgraph.html` (send/present the file, or open it if a browser is available).
  Source links open the local files (PDFs at the cited page) as long as the HTML stays in
  `.mindgraph/`; with `meta.repo_url` they open on the code host instead.
- In chat, keep it short: what the project or collection is in 2–3 sentences, the pillars, and
  the first 3 stops of the learning path. The graph holds the rest.
- Mention that future sessions can start from `.mindgraph/MINDGRAPH.md`, and offer to add one
  line to the project's `CLAUDE.md` / `AGENTS.md` pointing there (edit it only if they agree —
  it's their file). Suggested line:
  `Project map: read .mindgraph/MINDGRAPH.md before exploring; it lists pillars, key files and links.`
- `mindmap.mmd` is a Mermaid mindmap for READMEs, Notion, or GitHub — mention it only if useful.

## Updating an existing graph

Run the scan with `--since-graph`, read only changed files, edit the affected nodes and links in
`graph.json` (keep ids stable so learning paths and links survive), update `meta`, rebuild.
Outside git, changes are detected by modification time. If the project's shape changed a lot,
say so and offer a full rebuild instead of patching.
