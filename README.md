# Retrieval Studio

A local research platform for studying how the **OpenAI Responses API's web
search** behaves: what queries it fans out into, which sources come back,
which pages it opens, and what ends up cited — measured as rates across
repeat runs, with tracked-entity visibility and reconciled exports.

**Status: open beta (v0.1.2-beta).** Local, single-user tool. Not a hosted
service.

## What it measures — and what it doesn't

- It studies the **Responses API** surface (a clean-room condition: no
  account memory, no cookies, no personalisation). It does **not** observe
  ordinary users' private ChatGPT sessions, and results can differ from the
  consumer ChatGPT app.
- It records the **observable retrieval process**: fan-out queries, `site:`
  probes, sources returned, pages opened, in-page searches, citations, and
  (when the API returns one) a model-generated reasoning summary. It
  **cannot expose the model's hidden chain of thought**, and it never
  fabricates one — see
  [docs/TRACES_VS_REASONING.md](docs/TRACES_VS_REASONING.md).
- A single model answer is a sample, not market data. The tool's unit of
  evidence is the **rate across repeat runs**, with every raw response
  preserved for reprocessing.

## Definitions

| Term | Meaning |
|---|---|
| Fan-out query | Query text a web-search action ran. Actions sometimes return **no** query text — recorded as such, never inferred. |
| Search source | URL returned by a search action (`web_search_call.action.sources`). |
| Opened page | An `open_page` action — distinct from a returned source. |
| In-page search | A `find_in_page` action and its pattern. |
| Citation | A `url_citation` annotation on the final answer. Raw URL preserved; canonical URL (tracking params stripped) used for aggregation; repeat citations counted. |
| Tracked entity | A business/brand/person/product/domain you measure, with a role you assign (primary / comparison / competitor / neutral). Studies can also be unbranded. |

## Install

Requires **Python 3.12+** (tested on 3.12–3.14).

Clone or download this repository, then from its folder:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open http://localhost:8501. Try it **free first**: *Study Setup ▸ Create
sample study* builds a clearly-labelled sample dataset from fabricated
fixtures — every screen works without an API key.

## API key & costs

Live runs need an OpenAI API key: set `OPENAI_API_KEY` in your environment
(see `.env.example`) or type it into the masked field on Quick Run /
Review & Run (session-only; never logged, stored or exported).

Costs are real: each run makes one Responses API call per prompt × repeat ×
location, with web search pulling substantial content into context
(observed ≈ 28k input / 1.3k output tokens per run) plus a per-search tool
fee. The app shows an estimate before every run, requires confirmation
above ~US$2, and hard-caps a run at 500 calls. Verify current rates in
`pricing.json` against the OpenAI pricing page; fund with prepaid credit
for a hard ceiling.

## How data is stored

```
data/studies/{study_id}/{dataset_id}/
    manifest.json        identity: dataset type, requested + actual models,
                         prompt-set version+hash, schema/app version, totals
    prompts.json         frozen prompt list used by this dataset
    entities.json        frozen entity roster
    observations.db      SQLite — this dataset only
    raw/{obs_id}.json    immutable raw API responses (written first)
    exports/
```

One SQLite file **per dataset** (`live` / `sample` / `imported`) — sample
data physically cannot enter a live dataset; every screen and export reads
exactly one selected dataset; reconciliation checks gate the workbook. See
[docs/DATASET_ISOLATION.md](docs/DATASET_ISOLATION.md). If extraction rules
change, datasets reprocess from raw with zero API calls.

**Privacy warning:** your prompts and the raw responses may contain
sensitive research/commercial information. `data/` is gitignored — keep it
that way, and treat exports with the same care. See
[SECURITY.md](SECURITY.md).

## Using it

1. **Quick Run** (landing page) — type prompts, pick model/runs/location,
   run. Each run creates its own study + live dataset.
2. **Build Study** — for structured research: prompt library (single / bulk
   / Excel-CSV upload with validation preview and downloadable template),
   locations, tracked entities with roles, Review & Run with cost
   confirmation.
3. **Analyse** — Overview, per-run **Search & Evidence Traces**, Prompt
   Coverage, Fan-Out Queries, Sources (domain leaderboard: one row per
   canonical domain; entity attribution is a separate dimension), Entity
   Comparison, Citation Gaps (12 evidence-linked gap types), Query Clusters
   (stable 15-category taxonomy), Exports (21-sheet workbook, full JSON/
   Markdown dumps, raw ZIP, CSVs) — every field defined in
   [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

<!-- Screenshots: add PNGs to docs/img/ (quick-run.png, trace.png,
     overview.png) and link them here before announcing the repo. -->

## Model integrity

The requested model is enforced: every response's `model` field is recorded
as `actual_model`; a mismatch marks the run failed and **aborts the batch**
— the tool never continues under a silently substituted model. Incomplete
API responses are stored as evidence but classified `incomplete`, never
counted as successful runs.

## Tests

```bash
python -m unittest discover tests -v
```

59 tests: dataset isolation, model integrity, extraction shapes (plural /
singular / missing queries, sources, open_page, find_in_page, citations),
URL canonicalisation, per-stage entity attribution, alias matching, prompt
upload validation, location overrides, incomplete-response classification,
export reconciliation, raw persistence + reprocessing, unbranded studies,
and repo hygiene (no data artefacts or keys tracked). CI runs the same
suite on every push.

## Current limitations

- One engine (OpenAI Responses API). No Perplexity/Gemini/consumer-app
  coverage.
- Answer-mention detection is name/alias matching; true "recommendation"
  detection is not implemented (the metric is honestly named
  `answer_mention_rate`).
- Reasoning summaries appear only when the model returns them.
- Query clustering is rule-based (stable taxonomy), not semantic.
- Ships in **local mode** (single-user, persistent) by default. A **hosted
  mode** (`RETRIEVAL_MODE=hosted`) adds per-session isolation, an optional
  access-code gate and session cleanup — see [docs/HOSTING.md](docs/HOSTING.md).
  A *fully public, anonymous* deployment still needs rate-limiting, legal
  pages and outage handling (not yet built); use the access code and a
  controlled audience until then.
- Live runs block the UI tab for the duration of the batch.

## License

AGPL-3.0 — see [LICENSE](LICENSE). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
