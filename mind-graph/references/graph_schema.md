# graph.json format and quality guide

## Contents
1. Format (with a full minimal example)
2. Field reference
3. What makes a good mind graph
4. Patterns by project type (+ coverage for document collections)
5. Paper ↔ code (research repos)

## 1. Format

```json
{
  "meta": {
    "title": "LeRobot SO-101 pouring",
    "root_path": "..",
    "repo_url": "https://github.com/org/repo",
    "language": "vi",
    "scope": "Code + docs/. Skipped: notebooks/, data/.",
    "sources_read": ["README.md", "src/policy/act.py", "docs/calibration.md"]
  },
  "root": {
    "id": "root",
    "label": "SO-101 pouring",
    "kind": "root",
    "summary": "What the whole project is and the problem it solves, in 1–2 sentences.",
    "children": [
      {
        "id": "perception",
        "label": "Perception",
        "kind": "concept",
        "summary": "Two fixed cameras give the policy RGB views of bottle and cup; calibration aligns them to the robot base.",
        "importance": 3,
        "sources": [{"path": "src/perception/"}],
        "children": [
          {
            "id": "perception.calib",
            "label": "Camera calibration",
            "kind": "file",
            "summary": "Estimates camera-to-base extrinsics with an ArUco board; rerun after moving a camera.",
            "sources": [{"path": "src/perception/calibrate.py", "lines": "20-140"}],
            "tags": ["setup"]
          }
        ]
      }
    ]
  },
  "links": [
    {"from": "perception.calib", "to": "policy.obs", "type": "data_flow", "label": "extrinsics"}
  ],
  "learning_path": [
    {"node": "root", "note": "Start with the goal and the hardware."},
    {"node": "perception", "note": "Everything downstream depends on what the cameras see."}
  ],
  "glossary": [
    {"term": "ACT", "definition": "Action Chunking Transformer – predicts short action sequences.", "node": "policy.act"}
  ]
}
```

## 2. Field reference

**meta** (required: `title`)
- `root_path` – project root, relative to the folder containing graph.json (default `".."`, since
  graph.json lives in `<project>/.mindgraph/`). Used to check that `sources` exist.
- `repo_url` – optional; if set, source paths become links in the viewer
  (`<repo_url>/blob/<git_commit or HEAD>/<path>`). Without it, sources link to the local files
  relative to the HTML (PDFs open at the cited `page`). Set `local_links: false` to turn that off,
  e.g. when the HTML will be sent to someone who doesn't have the folder.
- `language` – language of labels/summaries (e.g. `vi`, `en`).
- `scope` – what was covered and what was deliberately skipped.
- `sources_read` – files you actually read, as plain paths, optionally with a note: `"src/train.py (skimmed)"`. Honest depth signal for readers and future sessions.
- `generated_at`, `git_commit` – filled in by the build script if missing.

**node** (required: `id`, `label`; `summary` strongly expected)
- `id` – unique, stable, readable (`training.loss`). Keep ids stable across updates.
- `label` – ≤ ~40 chars, ideally 1–5 words. It's drawn on the canvas.
- `kind` – one of:
  `root` · `concept` (idea, subsystem, pillar) · `module` (package/folder) · `file` ·
  `symbol` (class/function) · `doc` (document/section/paper) · `data` (dataset, schema, config,
  artifact) · `external` (library, service, API) · `question` (open issue, unclear area, TODO)
- `summary` – 1–2 sentences, ≤ ~300 chars: what it is + why it matters.
- `details` – optional, a few more sentences or `- bullet` lines (≤ ~120 words): how it works,
  gotchas, key parameters.
- `sources` – list of `{"path": "..."}` plus an optional locator saying *where inside* the file:
  - `lines`: `"10-40"` (code, text)
  - `page`: `5` or `"5-7"` (pdf) — the viewer opens the PDF at that page
  - `slide`: `12` or `"12-14"` (pptx/odp)
  - `sheet`: `"Results"` (xlsx/ods)
  - `section`: `"§3.2 Clipped objective"` (any document; heading text or number)
  Locators combine: `{"path": "paper.pdf", "page": 5, "section": "§3.2"}`. Folders end with `/`.
  The page/slide/unit numbers are the ones `extract_text.py --outline` prints.
- `importance` – 1 (minor) · 2 (normal, default) · 3 (core). Core nodes are emphasized.
- `tags` – optional short strings (searchable).
- `children` – sub-nodes.
- `collapsed` – optional bool to force the initial state in the viewer.
- `evidence` – optional list of `{"path": "...", "quote": "..."}`: short excerpts copied
  verbatim from the source that prove the summary (see below). The build adds `ctx`, `sig`,
  `verified`, `at` and `status` to each item and `evidence_status` to the node. Don't write these
  fields yourself, and keep them when you edit graph.json.

**Evidence (node-level verification).** A quote should be 20–200 characters, distinctive (unique
in its file) and directly support the claim, for example:

```json
"evidence": [
  {"path": "src/policy/act.py", "quote": "class ACTPolicy(nn.Module):"},
  {"path": "papers/act.pdf", "quote": "predicts a chunk of k future actions"}
]
```

Good quotes: a class or function signature, a config key with its value, a constant, a defining
sentence in a document, a slide bullet, a header row. Avoid boilerplate that occurs many times
(`return None`, `import torch`). Whitespace, line breaks, curly quotes and dashes are ignored
when matching. For PDFs a letters-and-digits fallback also handles hyphenation. Any format
`extract_text.py` reads can be quoted from.

What the build does with it:
- new or edited evidence (the node's summary, details or quote changed) must be found in the
  file, otherwise the build stops with an error. It then stores a hash of the text around the
  quote as the baseline.
- existing evidence is compared with that baseline. **ok** means the quote is found and the
  nearby text is unchanged; it may have moved. **changed** means the quote is found but nearby
  text was edited. **stale** means the quote or the file is gone.
- `build --accept id1,id2` (or `all`) re-baselines nodes you re-read and found still correct. A
  plain rebuild never clears a flag.
- the node status is the worst of its quotes. Flags show in the viewer (red = stale,
  amber = changed, a ring on collapsed branches that hide flags), in MINDGRAPH.md ("Needs
  re-check" and ⚠ in the map), and in `scan_project.py --since-graph`.

**links** – cross-links outside the tree. `from`, `to` (node ids), `type`, optional `label` (≤ 3 words).
Types: `depends_on` · `calls` · `data_flow` · `explains` (doc → code/concept) · `implements`
(code → concept/paper) · `configures` · `contrasts` · `related`.
`implements` links may carry `fidelity`: `exact` · `approx` · `partial` · `deviates`
(see Paper ↔ code below).

**learning_path** – ordered `{node, note}`; `note` is one line explaining why this stop comes now.

**glossary** – optional; domain terms a newcomer will trip over, each tied to a node.

## 3. What makes a good mind graph

The test: *after 5 minutes with the graph, could a newcomer explain how the project works and
know where to look to change X?* Concretely:

- **Concepts over folders.** A folder mirror adds little that `ls` doesn't. Group by
  responsibility / stage of the flow / idea, and use `sources` to anchor each idea in files.
  Folders that genuinely are the concept (e.g. `api/`) can map 1:1.
- **Follow the main flow.** Pillars ordered left-to-right or in learning-path order along the
  way data or control moves (input → processing → output) are easier to absorb.
- **Summaries say why, not just what.** "Loads config" is weak; "Single place where all
  hyper-parameters are resolved; CLI flags override YAML" teaches something.
- **Balanced, shallow tree.** 3–8 pillars; 3–9 children each; leaves at depth 3–4. Too many
  children → group them. A pillar with one child → merge it upward.
- **Few, meaningful cross-links.** Typically 0.3–1× the number of pillars-and-topics. Prefer
  the links that explain architecture (who feeds whom, what implements what). Never dump the
  whole import graph.
- **Make uncertainty visible.** Use `question` nodes for confusing areas, dead code, or things
  the docs and code disagree on. That's useful to a learner and honest about depth.
- **Learning path tells a story.** Start at intent, then the main flow, then the core
  abstraction, then extension points / how to run it, then advanced or optional parts.

## 4. Patterns by project type

Pick the closest pattern, then adapt; real collections are often a mix.

- **Application / service**: pillars = entry & config · request/data flow stages · domain
  model · storage/integrations · ops (deploy, tests).
- **ML / research code**: data & preprocessing · model/architecture · training loop & losses ·
  evaluation/metrics · experiments & configs · results/artifacts. Link code to paper sections
  with `implements`.
- **Library**: public API surface · core internals · extension points · examples · build/release.
- **Docs set / course notes**: core concepts · how-tos/procedures · reference · FAQ/gotchas.
  Sources are documents with `section`.
- **Course materials (slides + readings + exercises)**: pillars follow the syllabus units or the
  big ideas (not one node per lecture file); leaves cite `slide` ranges and reading `page`s;
  exercises/labs attach to the concept they practise; glossary is especially valuable.
- **Note vault (Obsidian, wiki, Zettelkasten)**: start from index/MOC notes and the most-linked
  notes in the scan; clusters of densely linked notes become pillars. Keep a note's own title as
  the node label when it is short. Use `related` links sparingly — the vault already has many.
- **Folder of mixed office files (project / business docs)**: pillars like goals & scope · plan &
  timeline · design / requirements · decisions · data & results · people & responsibilities ·
  open questions. Cite spreadsheets with `sheet`, decks with `slide`, reports with `page`.
  `contrasts` links are good for versions or proposals that disagree.
- **Meeting notes / logs over time**: pillars by topic or workstream, not by date; leaves are
  decisions and open questions with the meeting file as source; put the chronology in `details`.
- **Papers (one or several)**: problem & motivation · method (components) · experiments ·
  results & limitations · related work. Across several papers, pillars can be themes, with
  `contrasts` links between approaches.
- **Mixed code + paper (research repo)**: pillars = paper method (theory) · implementation ·
  paper vs code (deviations) · experiments & configs. The theory and implementation sides are
  joined by many `implements` links. Those links, and the deviations, are the most valuable part
  of the graph. Follow the conventions in "Paper ↔ code" below.
- **Data collection (CSV/XLSX/notebooks)**: pillars = datasets (what each describes, key
  columns) · how they are produced · analyses/notebooks · findings; `data_flow` links from
  datasets to the analyses that use them.

### Coverage for document collections

When the user asked about "everything in this folder", each relevant document should be
reachable from some node's `sources` — a single leaf per minor document is fine, and a folder
source (`"notes/"`) covers everything inside it. Documents you deliberately skip (duplicates,
drafts, boilerplate) go in `meta.scope` by name or folder. `build_mindgraph.py` reads
`.mindgraph/scan.json` and warns about documents that are neither.

## 5. Paper ↔ code

For repos that implement a paper. Run `scripts/paper_code.py` first. It writes
`.mindgraph/paper_map.json`, which the build uses for coverage.

**Paper side.** Paper-side nodes carry `paper_ref` (a string or a list), which says which part of
the paper the node stands for. Accepted forms:

| Form | Example | Normalized id |
|---|---|---|
| Equation | `"Eq. 7"`, `"Equation (7)"` | `eq:7` |
| Algorithm, or one of its lines | `"Alg. 1"`, `"Algorithm 1 line 4"`, `"Alg. 1 L4"` | `alg:1`, `alg:1.4` |
| Section / appendix | `"§3.2"`, `"Section 3.2"`, `"App. B.1"` | `sec:3.2`, `app:b.1` |
| Table / figure | `"Table 2"`, `"Fig. 3"` | `tab:2`, `fig:3` |
| Hyperparameter | `"hp:clip"`, `"hp:learning_rate"` | `hp:clip` (keys come from paper_code.py) |

With several papers, add `"paper": "papers/x.pdf"` to the node. Cite the location in `sources`
(`{"path": "paper/x.pdf", "page": 4}`). An `evidence` quote from the paper is a good way to pin
what the paper actually says.

**Code side.** Code nodes link to paper nodes:

```json
{"from": "code.loss", "to": "paper.clip_objective", "type": "implements", "fidelity": "approx"}
```

`fidelity` is one of:
- `exact`: the same math
- `approx`: equivalent up to constants, numerics or vectorization
- `partial`: some terms or steps are missing
- `deviates`: different behaviour

Leave `fidelity` out only if you didn't check.

**Deviations.** Every difference between paper and code is a `question` node with a
`deviation` object:

```json
{"id": "dev.clip", "label": "Clip ε 0.1 not 0.2", "kind": "question", "paper_ref": "hp:clip",
 "deviation": {"type": "hyperparameter", "paper": "ε = 0.2 (Table 1)",
               "code": "clip_range: 0.1 in configs/default.yaml",
               "impact": "Smaller trust region; likely slower learning (not verified)."},
 "summary": "Default clip range is half the paper's value.",
 "sources": [{"path": "configs/default.yaml", "lines": "1"}],
 "evidence": [{"path": "configs/default.yaml", "quote": "clip_range: 0.1"}]}
```

`deviation.type` is one of `coefficient` · `hyperparameter` · `missing_step` · `extra_step` ·
`different_formula` · `implementation_trick` (only in the code) · `other`. Attach each
deviation to what it differs from, either with `paper_ref` or with a `contrasts` link to the
paper node, and link it to the code node too. A code-only trick links only to its code node.
Group deviations under one pillar ("Paper vs code") so they are easy to scan.

**What the build produces.** A row per paper item:
- ✓ exact
- ≈ approx/mapped
- ◐ partial
- ⚠ deviates (any deviation, or `fidelity: deviates`)
- ∅ not in code (no `implements` link)

The rows appear as a table in MINDGRAPH.md and in the viewer's **Paper ↔ Code** tab, together
with all deviations. With paper_map.json present, the build also warns about equations and
algorithms that no node maps, and about `≠` hyperparameters that have no deviation node.
Neither warning blocks the build.
