"""Prompt library: model, parsing (bulk paste / Excel / CSV), validation.

A prompt is a dict:
    {prompt_id, prompt_text, category, intent, location, active, notes}

Validation (spec §3) never mutates input silently — it returns a preview of
issues (missing text, duplicate IDs, duplicate prompts, invalid locations,
unsupported fields, blank rows) for the user to confirm before import.
"""

from __future__ import annotations

import re

SUPPORTED_COLUMNS = ["prompt_id", "prompt_text", "category", "intent",
                     "location", "active", "notes"]
_TEXT_ALIASES = ("prompt_text", "prompt", "text", "query", "question")
_LOC_RE = re.compile(r"^[^,]+,\s*[^,]+,\s*[A-Za-z]{2}$")


def new_prompt(prompt_text: str, prompt_id: str = "", category: str = "",
               intent: str = "", location: str = "", active: bool = True,
               notes: str = "") -> dict:
    return {"prompt_id": prompt_id, "prompt_text": (prompt_text or "").strip(),
            "category": category, "intent": intent, "location": location,
            "active": bool(active), "notes": notes}


def assign_ids(prompts: list) -> list:
    """Fill empty prompt_ids with P01… without touching provided ids."""
    used = {p.get("prompt_id") for p in prompts if p.get("prompt_id")}
    n = 0
    for p in prompts:
        if not p.get("prompt_id"):
            n += 1
            while f"P{n:02d}" in used:
                n += 1
            p["prompt_id"] = f"P{n:02d}"
            used.add(p["prompt_id"])
    return prompts


_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]\s+|[-•*]\s+)")


def clean_prompt_line(line: str) -> str:
    """Strip numbered/bulleted list markers pasted from documents."""
    return _LIST_MARKER_RE.sub("", line or "").strip()


def from_lines(text: str) -> list:
    """Bulk paste: one prompt per line (list markers stripped)."""
    return assign_ids([new_prompt(clean_prompt_line(l))
                       for l in (text or "").splitlines() if l.strip()])


MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB
MAX_IMPORT_PROMPTS = 1000
MAX_PROMPT_CHARS = 4000


class UploadError(Exception):
    pass


def parse_upload(upload) -> tuple:
    """Parse an uploaded .xlsx/.csv/.txt file object (has .name/.getvalue()).
    Returns (prompts, unsupported_fields, blank_rows). Raises UploadError for
    unsafe or unreadable files (macro-enabled workbooks, oversize, corrupt or
    password-protected)."""
    import pandas as pd
    name = (upload.name or "").lower()
    if name.endswith((".xlsm", ".xltm", ".xlsb")):
        raise UploadError("Macro-enabled Excel files are not accepted. "
                          "Re-save as .xlsx or .csv.")
    if not name.endswith((".xlsx", ".csv", ".txt")):
        raise UploadError("Only .xlsx, .csv and .txt are accepted.")
    data = upload.getvalue()
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is {len(data)//1024//1024} MB; the limit is "
                          f"{MAX_UPLOAD_BYTES//1024//1024} MB.")
    try:
        if name.endswith(".txt"):
            text = data.decode("utf-8-sig", errors="replace")
            return _cap(from_lines(text)), [], 0
        if name.endswith(".xlsx"):
            import io as _io
            df = pd.read_excel(_io.BytesIO(data), dtype=str)
        else:
            import io as _io
            df = pd.read_csv(_io.BytesIO(data), encoding="utf-8-sig",
                             dtype=str)
    except Exception as e:  # corrupt / password-protected / bad encoding
        raise UploadError(f"Could not read the file (corrupted, "
                          f"password-protected or not a valid "
                          f"spreadsheet): {e}")
    df = df.fillna("")
    cols = {str(c).strip().lower(): c for c in df.columns}
    text_col = next((cols[a] for a in _TEXT_ALIASES if a in cols), None)
    headerless = False
    if text_col is None:
        # Headerless single-column file: first row was promoted to header.
        text_col = df.columns[0]
        headerless = True
    unsupported = [str(c) for c in df.columns
                   if str(c).strip().lower() not in SUPPORTED_COLUMNS
                   and str(c).strip().lower() not in _TEXT_ALIASES
                   and not headerless]
    prompts, blanks = [], 0
    if headerless:
        first = str(text_col).strip()
        if first and not first.lower().startswith("unnamed"):
            prompts.append(new_prompt(first))
    for _, row in df.iterrows():
        text = str(row.get(text_col, "")).strip()
        if not text or text.lower() == "nan":
            blanks += 1
            continue
        def g(field):
            c = cols.get(field)
            return str(row.get(c, "")).strip() if c else ""
        active_raw = g("active").lower()
        prompts.append(new_prompt(
            text, prompt_id=g("prompt_id"), category=g("category"),
            intent=g("intent"), location=g("location"),
            active=active_raw not in ("false", "0", "no", "inactive"),
            notes=g("notes")))
    return _cap(assign_ids(prompts)), unsupported, blanks


def _cap(prompts: list) -> list:
    """Enforce import caps: max prompt count and per-prompt length."""
    for p in prompts:
        if len(p.get("prompt_text", "")) > MAX_PROMPT_CHARS:
            p["prompt_text"] = p["prompt_text"][:MAX_PROMPT_CHARS]
    return prompts[:MAX_IMPORT_PROMPTS]


def validate(prompts: list, unsupported=None, blank_rows: int = 0) -> dict:
    """Validation preview. Returns dict of issue lists; `ok` is True when no
    blocking issues (missing text, duplicate ids) exist."""
    issues = {"missing_text": [], "duplicate_ids": [], "duplicate_prompts": [],
              "invalid_locations": [],
              "unsupported_fields": list(unsupported or []),
              "blank_rows": blank_rows}
    seen_ids, seen_text = {}, {}
    for i, p in enumerate(prompts):
        if not (p.get("prompt_text") or "").strip():
            issues["missing_text"].append(p.get("prompt_id") or f"row {i+1}")
        pid = p.get("prompt_id") or ""
        if pid in seen_ids:
            issues["duplicate_ids"].append(pid)
        seen_ids[pid] = i
        key = (p.get("prompt_text") or "").strip().lower()
        if key and key in seen_text:
            issues["duplicate_prompts"].append(
                f'{pid} duplicates {prompts[seen_text[key]].get("prompt_id")}')
        else:
            seen_text[key] = i
        loc = (p.get("location") or "").strip()
        if loc and not _LOC_RE.match(loc):
            issues["invalid_locations"].append(f'{pid}: {loc!r}')
    issues["ok"] = not (issues["missing_text"] or issues["duplicate_ids"])
    return issues
