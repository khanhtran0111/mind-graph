---
name: mind-graph
description: Read a codebase, a folder of documents (PDF, Word, PowerPoint, Excel, Markdown/Obsidian notes, HTML, EPUB, notebooks), papers, or any mix of them, and turn the understanding into an interactive mind graph (HTML) plus a compact MINDGRAPH.md that later sessions load instead of re-reading everything. Use this whenever someone wants to understand, learn, onboard onto, summarize, map, visualize or get an overview of a repo, project, library, documentation set, course materials, research/paper collection, meeting notes or any folder of files — "explain this project's structure", "help me learn this codebase", "summarize everything in this folder", "draw a mind map / concept map / knowledge graph of these docs", "sơ đồ tư duy", "mind map cho project này", "tóm tắt các tài liệu trong thư mục này" — and to compare how a project's architecture changed between commits, PRs or releases, or to map a paper onto the code that implements it. Also use it whenever a .mindgraph/ folder already exists in the project, even if the user doesn't say "mind graph".
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
├── scripts/evidence.py         # node-level evidence: verify quotes, flag stale / changed nodes
├── scripts/paper_code.py       # research repos: paper inventory, hyperparameter diff, code candidates
├── scripts/paper_graph.py      # paper ↔ code table, validation and coverage (used by the build)
├── scripts/graph_diff.py       # conceptual diff of two graphs (used by build --diff)
├── assets/viewer.html          # interactive viewer template (no external dependencies)
└── references/graph_schema.md  # graph.json format + what makes a good mind graph — read before writing
```

Requirements: Python 3.9+. Everything works with the standard library. For PDF text, one of
`pymupdf`, `pypdf` or poppler's `pdftotext` is used if present; without them PDFs are still
listed (and you can open them with your own file-reading tool if it reads PDFs).

## Step 0 — Reuse before you read

If `<project>/.mindgraph/MINDGRAPH.md` exists, read it first. That is the point of this skill.
Then run the scan with `--since-graph` (below). It lists the files changed since the graph was
built and, for nodes that carry evidence, re-checks each quote against today's files. The
result is a list of nodes that are **stale** (the quote is gone) or **changed** (the text around
the quote was edited). Nodes reported ok can be trusted without re-reading. If nothing relevant
changed, answer from the existing graph. Otherwise re-read only the flagged nodes' sources,
update those branches of `graph.json` and rebuild. Don't regenerate the whole thing.

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

### Research repos: paper ↔ code

When the repo implements one or more papers (the scan prints "Paper + code detected", or the
user mentions a paper or reproducing results), map the paper onto the code. This is the most
valuable and the most laborious part of reproducing a paper.

```bash
python <skill>/scripts/paper_code.py paper.pdf --code <root>            # or main.tex / an arXiv source folder
```

It writes `.mindgraph/paper_map.json` and prints:

- the paper's inventory: sections, numbered equations, algorithms with their steps, tables, and
  stated hyperparameters, each with an id (`eq:7`, `alg:1`, `alg:1.4`, `§3.2`, `hp:clip`)
- a **hyperparameter diff**: each value stated in the paper against same-named settings in
  code, configs and scripts. A `≠` row is a deviation candidate.
- code comments that cite the paper ("Eq. (7)", "Algorithm 1")
- ranked candidate functions for each equation and algorithm

Treat the candidates as leads. Read each one and decide:

- **implements, and how faithfully?** `exact` (same math), `approx` (equivalent up to
  constants or numerics), `partial` (some steps), or `deviates`.
- **what differs?** A coefficient or hyperparameter, a missing step, an extra step, a different
  formula, or a trick that only exists in the code (gradient clipping, normalization, warmup,
  clamping, epsilon terms). Each difference becomes a deviation node. Be concrete: what the paper
  says, what the code does, and the likely impact. When the impact is a guess, say so.

An equation that nothing implements stays as a node with `paper_ref` and no `implements` link.
The table then shows it as ∅ (not in code), which is a finding in itself.

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
- **Back key claims with evidence.** On core nodes (`importance: 3`) and on leaves that make a
  specific claim, add `evidence`: 1–2 short quotes copied verbatim from the source that prove the
  summary, e.g. a signature, a config line, or a defining sentence (20–200 chars, unique in the
  file). The build locates each quote and stores a baseline. A quote it can't find is an error,
  so copy the quote, don't paraphrase it. Later builds and scans then show exactly which nodes no
  longer hold. Pillars that only group their children usually don't need evidence.
- **Research repos: two sides joined by `implements`.** Paper-side nodes carry `paper_ref`
  ("Eq. 7", "Alg. 1 L4", "§3.2", "hp:clip"). Code nodes link to them with
  `{"type": "implements", "fidelity": …}`. Each paper/code difference is a `question` node with
  a `deviation` object, linked with `contrasts`. Put these nodes under a "Paper vs code" pillar so
  a reader checks them first when results don't reproduce. The build turns all this into a
  Paper ↔ Code table and warns about equations, algorithms and `≠` hyperparameters from
  paper_map.json that the graph doesn't cover yet.
- **For a document collection, every relevant document should be reachable** from some node's
  `sources` (or named as skipped in `meta.scope`); the build reports coverage against scan.json.

Write it to `<root>/.mindgraph/graph.json`. Write labels, summaries and notes in the language the
user is working in (ask only if unclear); keep identifiers, paths and document titles verbatim.

## Step 4 — Build and check

```bash
python <skill>/scripts/build_mindgraph.py <root>/.mindgraph/graph.json
```

This validates the graph (unique ids, valid links, source paths that exist, page/slide formats,
size warnings, document coverage), verifies evidence, and writes `mindgraph.html`, `MINDGRAPH.md`
and `mindmap.mmd` next to it. Nodes whose evidence is stale or changed get a ⚠ in the viewer and
in MINDGRAPH.md. Fix every error and reconsider the warnings, then rebuild. `--check` validates
and verifies without writing anything.

## Step 5 — Hand it over

- Give the user `mindgraph.html` (send/present the file, or open it if a browser is available).
  Source links open the local files (PDFs at the cited page) as long as the HTML stays in
  `.mindgraph/`; with `meta.repo_url` they open on the code host instead.
- In chat, keep it short: what the project or collection is in 2–3 sentences, the pillars, and
  the first 3 stops of the learning path. The graph holds the rest. For a research repo, also
  list the deviations that are most likely to affect results.
- Mention that future sessions can start from `.mindgraph/MINDGRAPH.md`, and offer to add one
  line to the project's `CLAUDE.md` / `AGENTS.md` pointing there (edit it only if they agree —
  it's their file). Suggested line:
  `Project map: read .mindgraph/MINDGRAPH.md before exploring; it lists pillars, key files and links.`
- `mindmap.mmd` is a Mermaid mindmap for READMEs, Notion, or GitHub — mention it only if useful.

## Updating an existing graph

Run the scan with `--since-graph` and start from its evidence list. For each ⚠ node:

- **stale**: re-read the source and rewrite the node's summary and quote, or remove the node if
  the thing is gone.
- **changed**: re-read around the quote. If the node is still right, run the build with
  `--accept <id>[,<id>…]` to store a new baseline. If it is wrong, edit the node.

Editing a node's summary, details or quote re-verifies it automatically on the next build. A
plain rebuild never clears a flag. Then read changed files that no node covers, add or edit
nodes (keep ids stable so learning paths and links survive), update `meta`, and rebuild.
Outside git, changes are detected by modification time. If the project's shape changed a lot,
say so and offer a full rebuild instead of patching.

## Comparing two versions (PR review, releases)

```bash
python <skill>/scripts/build_mindgraph.py --diff OLD NEW [--out-dir DIR]
python <skill>/scripts/build_mindgraph.py --diff main:.mindgraph/graph.json .mindgraph/graph.json
```

OLD and NEW are graph.json paths or git objects (`REV:path`, e.g. the graph committed on `main`
or on a release tag). The command writes two files next to NEW:

- `mindgraph_diff.html`: the viewer with changed branches expanded and unchanged ones
  collapsed. Added nodes are green (+), removed ones red and struck through, changed ones amber
  (~, with the previous summary in the node panel), and moved ones blue (↷). Links are coloured
  the same way, and a Changes tab lists everything.
- `DIFF.md`: a summary to paste into a PR description. It lists the areas that changed most and
  the added, removed, changed and moved nodes, plus relationship and learning-path changes.

Nodes are matched by id, so the diff is only meaningful if ids stay stable when you update a
graph. Nodes whose id changed but whose label didn't are reported as "id renamed".

Use it when the user wants to review a large PR at the concept level, or to see how a library's
architecture changed between releases. To compare releases, build one graph per release (for
example, check out each tag and run Steps 1–4, or update the graph incrementally from tag to tag
so ids stay stable), then diff them. In chat, summarize the "Areas most changed" and the 3–5
changes that matter most for the user's question.
