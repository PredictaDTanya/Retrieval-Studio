# Observable search traces vs hidden reasoning

## What the Responses API exposes
The API returns the **observable actions** of a run in output order:
`web_search_call` items (search actions with their queries and returned
sources, `open_page`, `find_in_page`), `reasoning` items that may carry a
**model-generated summary**, and the final `message` with `url_citation`
annotations. That is what the Search & Evidence Trace shows, chronologically,
with sequence preserved (output order + action index).

## What it does NOT expose
The model's raw hidden chain of thought. The app therefore:
- never titles anything "ChatGPT's thought process";
- requests `reasoning.summary = "auto"` and displays a summary **only when
  one is returned**, always with the disclaimer *"This is a model-generated
  reasoning summary, not the model's hidden raw chain of thought"*;
- shows "No reasoning summary returned." when absent — a summary is never
  fabricated;
- shows "Search action returned without query text." when a search action
  carries no queries — missing searches are never inferred or invented.

## Interpretation guidance
The trace is evidence of *what the system did* (queried, probed, opened,
cited), not *why*. Rate-based metrics across repeat runs are the unit of
evidence; a single run is a sample.
