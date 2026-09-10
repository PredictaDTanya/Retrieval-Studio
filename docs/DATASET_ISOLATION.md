# Dataset isolation model

## The problem it solves
The v1 tool wrote every batch into one flat store. Two live batches (terra
25 runs, sol 20 runs — the sol batch was killed mid-run by a Streamlit rerun)
accumulated in one `observations.db`, while the runner's XLSX covered only
the batch that finished. The dashboard (cumulative, 44 obs / 442 queries) and
the "clean" export (last batch, 25 obs / 219 queries) told different stories,
and the UI's model picker didn't describe the mixed store. No sample data was
ever mixed in and no model was substituted — but nothing in the architecture
*prevented* either. v2 makes the confusion structurally impossible.

## The model
- Every execution creates a **dataset**: `dataset_id`, `dataset_type`
  (`live` / `sample` / `imported`), tied to one `study_id`.
- **One SQLite file per dataset** at
  `data/studies/{study_id}/{dataset_id}/observations.db`. A live dataset
  cannot contain sample rows because they are different files on disk.
- Sample data is created only by the sample builder, always as
  `dataset_type="sample"` inside its own clearly-named study, with a red
  SAMPLE badge everywhere it is shown.
- A live run creates a NEW dataset by default; appending to an existing
  live dataset requires deliberately selecting it on Review & Run.
- Every row still carries `dataset_id` + `study_id`, so reconciliation
  re-verifies membership before any export (belt and braces).
- The manifest (`manifest.json`) freezes the run's identity: dataset type,
  requested model, actual models observed, prompt-set version + hash,
  schema version, app version, git commit, locations, totals, cost.
- `prompts.json` / `entities.json` freeze the prompt list and entity roster
  the dataset was executed with, so later library edits can't rewrite
  history.

## Model integrity
`requested_model` is what you chose; `actual_model` is read from every API
response. A mismatch marks the run failed (`model_mismatch`) and aborts the
batch — the system never continues under a substituted model, and both
values appear in the UI, the manifest and every export.
