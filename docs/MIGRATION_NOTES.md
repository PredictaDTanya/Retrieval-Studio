# Migration notes — v1 client-based data → v2 studies

## What "Import v1 data" does
Reads the flat v1 `output/raw/*.json` envelopes and rebuilds them under the
v2 structure, entirely from raw (no API calls):

- One study: *Melbourne SEO agencies (migrated v1)*.
- One dataset **per batch** (grouped by requested model): the terra batch
  (25 obs) and the sol batch (20 obs — including the run the killed v1
  process saved as raw but never stored; migration recovers it).
- `dataset_type="imported"` so migrated data is visibly not a fresh live run.

## Client → entity conversion
- v1 `config.json` `client` → a tracked entity with `role="primary"`
  (name variants → aliases, domains → primary/alt domains).
- v1 `competitors` → entities with `role="competitor"` — that is what they
  were configured as in v1; roles are editable afterwards and nothing NEW is
  ever auto-classified as a competitor.
- v1 flag fields map as: `is_client_*` → primary-entity attribution;
  `competitor_hits` → source_entity bridge rows; "consulted" (which in v1
  conflated returned sources and opened pages) is split into
  `search_sources` / `opened_pages` / `inpage_searches`.

## Caveats on migrated datasets
- v1 calls did not send `include=web_search_call.action.sources` — migrated
  datasets contain citations but few/no returned-source rows. The
  consulted-vs-cited split is fully populated from the first v2 live run.
- v1 calls did not request reasoning summaries → traces show "No reasoning
  summary returned." (correctly).
- v1 stored requested model only; migration reads `actual_model` from each
  raw response — for the legacy data every requested model matched its
  actual model (verified during the 2026-09-01 audit).

## Older v1 exports (xlsx/csv)
Keep them as artefacts; do not re-import spreadsheets. The raw JSON is the
source of truth and produces richer v2 tables via reprocessing.
