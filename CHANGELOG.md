# Changelog

## 0.1.2-beta — 2026-09-10

- **Renamed the product to Retrieval Studio** (formerly "Fan-Out Responses
  Studio"). The *fan-out query* feature term is unchanged — only the product
  name, package slug (`retrieval-studio`) and version (0.1.2b1) moved.
- **Environment variables renamed** `FANOUT_*` → `RETRIEVAL_*`:
  `RETRIEVAL_MODE`, `RETRIEVAL_ACCESS_CODE`, `RETRIEVAL_LEADS_WEBHOOK`,
  `RETRIEVAL_LEADS_TOKEN`. Update your Streamlit secrets / `.env` to match —
  the old names are no longer read.


## 0.1.1-beta — 2026-09-10

- Added **`gpt-6-astra`** as a selectable comparison model. Astra is its own
  gpt-6 flagship family (not a gpt-5.6 variant), priced ~2.5× Sol
  ($10 / $50 per 1M in/out). Sol stays the default; both model selectors show
  a cost caption when Astra is chosen. New `TestModelPricingCoverage` ensures
  every selectable model is priced. 59 tests.


## 0.1.0-beta (r6) — 2026-09-03

- Lead capture: optional `FANOUT_LEADS_TOKEN` shared secret sent to the
  webhook (as `_token`) so the endpoint can reject spam; never stored in the
  local record. Ready-to-paste Google Sheet endpoint at
  `docs/leads_google_sheet.gs`; HOSTING.md documents the recipe. 54 tests.


## 0.1.0-beta (r5) — 2026-09-03

- **Lead capture** on the gated hosted access gate: name + email (+ org) with
  a consent tick, recorded to data/leads.jsonl and, if `FANOUT_LEADS_WEBHOOK`
  is set, POSTed to a durable sink (Google Sheet / Zapier / Airtable) — the
  reliable database on an ephemeral host. API keys never stored in a lead.
  See docs/HOSTING.md. Suite: 53 tests.


## 0.1.0-beta (r4) — 2026-09-03

- **Hosting foundation** (deploy-mode flag): `local` (default, persistent,
  unchanged) vs `hosted` (per-session isolated temporary workspaces). Hosted
  mode adds an optional `FANOUT_ACCESS_CODE` gate, a "Hosted session" banner,
  Clear/Delete-my-data controls, TTL garbage-collection of expired sessions,
  and hides the local-only v1 import. Path-traversal-safe session ids. See
  docs/HOSTING.md.
- Prompt UX: multi-line paste into the single-prompt form is caught and
  offered as split/one/cancel; bulk paste strips list markers.
- Suite: 50 tests (incl. two-session isolation, session deletion, TTL GC).


## 0.1.0-beta (r3) — 2026-09-03

- **Citation-to-claim mapping**: every citation occurrence stored separately
  (`citation_occurrences`: span, cited_text, containing answer sentence) —
  aggregated `citations` kept; reconciliation enforces parity.
- **Search-call provenance**: `search_calls` table (id, status, action type,
  query/source counts per web_search_call); reconciled against rollups.
- **Source types preserved** (`url`, `oai-weather`/`oai-sports`/`oai-finance`
  feed sources no longer reduced to URL-only).
- **Response provenance**: created_at, service_tier; failed response bodies
  retain the actual API error code/message (classified `error`).
- **Advanced search settings** (behind an expander, recorded in the
  manifest): search context size, tool_choice auto/required, max tool
  calls, allowed domains, optional feature-detected
  `web_search_call.results` capture.
- **Insights report**: one Markdown file with all insights from a dataset
  (no per-run trace bulk) — primary download on Exports.
- Suite: 41 tests.

## 0.1.0-beta — 2026-09-01 (first public release)

### Research-integrity fixes
- "Append to existing dataset" removed from the UI (every run creates its
  own dataset); core writes remain reconciliation-coherent regardless.
- Incomplete API responses (`status != completed`) are classified
  `incomplete` with `incomplete_details.reason` stored — never counted ok.
- `response_id` and `response_status` stored on every observation and
  verified by reconciliation.
- Prompt-level location overrides implemented: an overridden prompt runs
  only at its own location.
- Study modes enforced (unbranded = no entities, single = exactly one,
  multi = two-plus; at most one primary); duplicate prompt/entity ids block
  runs.
- Entity attribution separated **per stage**: search-targeted, source
  returned, page opened, page searched, cited, named in answer.
- "Recommendation rate" renamed to **answer_mention_rate** (recommendation
  detection is not implemented, so it is not claimed).
- Reconciliation expanded (response ids, incomplete classification, frozen
  library uniqueness, like-for-like rollup comparisons); suite now 34 tests.

### Public packaging
- Predicta/client-specific config removed from tracking; `config.example.json`
  (fictional entities) + application-owned `pricing.json` added.
- v1 pipeline moved to `legacy/`; `examples/` (fictional prompts +
  entities), `.env.example`, `SECURITY.md`, `CONTRIBUTING.md`,
  `pyproject.toml` (Python ≥3.12), pinned + bounded `requirements.txt`
  (no lock file in the beta), `.gitattributes`, GitHub Actions CI,
  AGPL-3.0 LICENSE.
- Run safety: 500-call hard cap per run; cost estimate + confirmation
  unchanged.

## 2.0.0 (internal) — 2026-09-01

### Data integrity
- Dataset isolation: one SQLite + raw dir per dataset under
  `data/studies/{study_id}/{dataset_id}/`; dataset types live / sample /
  imported; sample data can never enter a live dataset; every row carries
  dataset_id/study_id; live runs create a new dataset by default.
- Model integrity: requested_model vs actual_model recorded per run;
  mismatch fails the run and aborts the batch (no silent fallback); model
  rejection aborts with the API error; both values shown in UI + exports.
- Root cause of the v1 confusion documented (two live batches in one flat
  store + last-batch-only exports); v1 data migrated into two isolated
  imported datasets, recovering one orphaned sol run (20, not 19).

### Structure
- "Client" replaced by tracked entities (id, name, domains, aliases, type,
  role primary/comparison/competitor/neutral, notes); unbranded / single /
  multi study modes; no auto-competitor classification.
- Prompt management: add-one, bulk paste, Excel/CSV upload with validation
  preview (missing text, duplicate ids/prompts, invalid locations,
  unsupported fields, blank rows), editable library, downloadable
  templates, prompt-set version + hash.

### Extraction & analysis
- Search queries / returned sources / opened pages / in-page searches /
  citations captured as distinct tables; raw_url preserved, canonical_url
  (tracking params stripped) for aggregation; repeat citations counted.
- Search actions without query text recorded as such — never inferred.
- Reasoning summaries requested (`reasoning.summary=auto`), stored and
  displayed only when returned, with disclaimer; never fabricated.
- Source-domain classification (9 classes) separated from entity
  attribution (source_entity bridge); domain leaderboard = one row per
  canonical domain.
- Query clustering rebuilt on a stable 15-category taxonomy (11 categories
  populated on the migrated data vs ~143 fragmented clusters in v1).
- Citation-gap diagnosis classified into 12 evidence-linked gap types.

### UI
- Left-nav restructure (Build Study / Analyse), settings live in their
  screens; study identity header with unmissable LIVE/SAMPLE/IMPORTED badge;
  dark / light / system themes (persisted in data/ui_settings.json);
  Review & Run confirmation with cost estimate and live progress;
  Search & Evidence Trace per run with prev/next navigation.

### Exports
- 21-sheet study workbook; raw JSON zip; per-table CSVs; data package;
  prompt templates; reconciliation checks gate every workbook build.
- Raw responses immutable; datasets reprocessable from raw without API
  calls.

### Tests
- 24-test stdlib unittest suite + labelled fixtures
  (`python -m unittest discover tests -v`).
