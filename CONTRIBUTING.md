# Contributing

Thanks for the interest. Ground rules:

## Setup
```bash
python -m pip install -r requirements.txt
python -m unittest discover tests -v      # must pass before and after
python -m streamlit run app.py
```
Python 3.12+ (developed on 3.12–3.14).

## Principles the codebase enforces — PRs must not weaken them
1. **Dataset isolation** — one SQLite + raw dir per dataset; sample data can
   never enter a live dataset; every analysis reads one selected dataset.
2. **Model integrity** — requested vs actual model recorded; a mismatch
   aborts the batch; no silent fallback, ever.
3. **Honest evidence** — reasoning summaries only when the API returns them
   (never fabricated); search actions without query text are recorded as
   such (never inferred); returned sources / opened pages / in-page searches
   / citations stay distinct concepts; raw responses stay immutable.
4. **Reconciliation gates exports** — if you add a metric, add its
   reconciliation check and a test.

## Practical notes
- Stdlib `unittest`; fixtures live in `tests/fixtures.py` and must be
  clearly fabricated (`.example` domains, "TEST FIXTURE" labels).
- No new hard dependencies without discussion; keep `requirements.txt`
  pinned with upper bounds.
- Never commit anything under `data/`, keys, or real research output — CI's
  repo-hygiene tests will fail the PR.
