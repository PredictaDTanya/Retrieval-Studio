"""Core package for the Retrieval Studio v2.

Modules:
    model       constants, URL canonicalisation, SQLite schema + helpers
    extraction  raw Responses payload -> ordered trace + typed rows
    entities    tracked-entity matching and attribution
    classify    source-domain classes, query taxonomy, gap diagnosis
    datasets    study/dataset lifecycle, manifests, raw persistence, reprocess
    runner      live run engine (requested vs actual model, no fallback)
    analysis    overview metrics, prompt coverage, entity visibility, clusters
    exports     study workbook, CSVs, raw ZIP, reconciliation
"""

APP_VERSION = "0.1.0b1"
