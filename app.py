#!/usr/bin/env python3
"""Retrieval Studio v2 — research platform UI.

Launch:  streamlit run app.py   (or run_app.bat)

Structure (spec §4):
  Build Study : Study Setup / Prompts / Locations / Tracked Entities /
                Review & Run
  Analyse     : Overview / Search & Evidence Traces / Prompt Coverage /
                Fan-Out Queries / Sources / Entity Comparison /
                Citation Gaps / Query Clusters / Exports

All analysis reads exactly ONE selected dataset (physically isolated SQLite).
The API key is never rendered, logged or exported.
"""

from __future__ import annotations

import json
import os
import time

import altair as alt
import pandas as pd
import streamlit as st

import core.datasets as ds
from core import runner, analysis, exports, migrate
from core.entities import EntityMatcher, new_entity
from core.model import open_db
from core import prompts as pr
from core import hosting
from core import leads

# Streamlit Community Cloud exposes config via st.secrets, NOT environment
# variables. The core modules read os.environ, so bridge known keys across
# (env wins if already set). Safe when no secrets file exists.
try:
    for _k in ("RETRIEVAL_MODE", "RETRIEVAL_ACCESS_CODE", "RETRIEVAL_LEADS_WEBHOOK",
               "RETRIEVAL_LEADS_TOKEN", "OPENAI_API_KEY"):
        if _k not in os.environ and _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

import re as _re

_SK_RE = _re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


def redact(text: str) -> str:
    """Strip any OpenAI key pattern from text shown in the UI or logs."""
    return _SK_RE.sub("sk-***REDACTED***", str(text or ""))


def validate_api_key(api_key: str):
    """Cheap pre-flight check: confirm the key authenticates before a large
    run. Returns (ok: bool, message: str). Never echoes the key."""
    try:
        from openai import OpenAI
        OpenAI(api_key=api_key, timeout=20, max_retries=0).models.list()
        return True, ""
    except Exception as e:  # noqa: BLE001
        msg = redact(str(e))
        if "401" in msg or "invalid" in msg.lower() or "auth" in msg.lower():
            return False, "Key rejected by OpenAI (401). Check the key."
        return False, f"Could not validate key: {msg[:160]}"


def forget_key_button(*state_keys):
    """A 'Forget API key' button that clears the key from session state."""
    if st.button("Forget API key"):
        for k in state_keys:
            st.session_state.pop(k, None)
        st.rerun()


HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "data", "ui_settings.json")
MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"]

# Models materially pricier than Sol get a cost caption under the selector.
# Sol stays first (the default) so a flagship is never picked by accident.
PREMIUM_MODELS = {
    "gpt-6-astra": "gpt-6 flagship — ~2.5x Sol's per-token cost "
                   "($10 / $50 vs $4 / $20 per 1M in / out). A separate model "
                   "family, so treat it as a comparison condition, not a "
                   "drop-in Sol. Keep runs-per-prompt low.",
}


def model_cost_note(container, model):
    """Render a cost caption under a model selector for premium models."""
    note = PREMIUM_MODELS.get(model)
    if note:
        container.caption(f"⚠️ {note}")

# --------------------------------------------------------------------------- #
# Theme (dark / light / system) — persisted in data/ui_settings.json
# --------------------------------------------------------------------------- #

def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)


def _actual_theme() -> str:
    """The theme the frontend ACTUALLY applied this session. Custom CSS keys
    off this — never off the preference — so the two cannot disagree."""
    try:
        t = st.context.theme.type
        if t in ("dark", "light"):
            return t
    except Exception:
        pass
    try:
        return st._config.get_option("theme.base") or "dark"
    except Exception:
        return "dark"


def _apply_theme_runtime(pref: str):
    """Best-effort live switch (works on fresh loads; a hard refresh always
    applies the persisted config)."""
    if pref not in ("dark", "light"):
        return
    try:
        dark = pref == "dark"
        st._config.set_option("theme.base", pref)
        st._config.set_option("theme.primaryColor", "#FD9904")
        st._config.set_option("theme.backgroundColor",
                              "#0d0a10" if dark else "#ffffff")
        st._config.set_option("theme.secondaryBackgroundColor",
                              "#191022" if dark else "#f2ecf4")
        st._config.set_option("theme.textColor",
                              "#ece6f2" if dark else "#241031")
    except Exception:
        pass


settings = _load_settings()
_PREF = settings.get("theme", "dark")
_apply_theme_runtime(_PREF)
# CSS follows the preference for explicit dark/light (runtime set_option
# aligns the native theme best-effort; the tracked .streamlit/config.toml is
# never modified). st.context is only trusted for "system".
THEME = _PREF if _PREF in ("dark", "light") else _actual_theme()
DARK = THEME == "dark"

st.set_page_config(page_title="Retrieval Studio", page_icon="🔎",
                   layout="wide")


# --------------------------------------------------------------------------- #
# Deploy mode + per-session isolation (hosted) / persistent (local)
# --------------------------------------------------------------------------- #
import uuid as _uuid

HOSTED = hosting.is_hosted()
if HOSTED:
    # Stable per-session id (one per browser session), then scope ALL storage
    # to this session's temporary workspace so visitors never share data.
    if "_sid" not in st.session_state:
        st.session_state["_sid"] = _uuid.uuid4().hex
    _sid = st.session_state["_sid"]
    ds.set_session_root(hosting.session_workspace(_sid))
    hosting.gc_sessions()  # opportunistic cleanup of expired workspaces

    # Optional access gate for a gated beta — captures name + email (lead
    # capture) alongside the access code.
    _code = hosting.access_code()
    if _code and not st.session_state.get("_access_ok"):
        st.title("🔎 Retrieval Studio")
        st.caption("Enter your details and the access code to continue.")
        g_name = st.text_input("Name")
        g_email = st.text_input("Email")
        g_org = st.text_input("Organisation (optional)")
        g_entered = st.text_input("Access code", type="password")
        g_consent = st.checkbox(
            "I agree to be contacted about Retrieval Studio, and I understand "
            "my prompts are sent to OpenAI for processing. My API key is sent "
            "directly to OpenAI and is not stored by this app; my research "
            "data is temporary and deleted when my session expires.")
        if st.button("Enter", type="primary"):
            if not (g_name.strip() and g_email.strip()):
                st.error("Name and email are required.")
            elif not leads.valid_email(g_email):
                st.error("Please enter a valid email address.")
            elif not g_consent:
                st.error("Please tick the consent box to continue.")
            elif g_entered.strip() != _code:
                st.error("Incorrect access code.")
            else:
                leads.record_lead(g_name, g_email, g_org,
                                  session_id=st.session_state.get("_sid", ""))
                st.session_state["_access_ok"] = True
                st.rerun()
        st.stop()


# palette (Predicta Workbook): orange primary, deep purple surfaces,
# lime = primary entity, bright purple = other entities, cyan = search path
ORANGE, PURPLE, LIME, VPURPLE, CYAN = ("#FD9904", "#481056", "#B8E900",
                                       "#C162FF", "#00B8D4")
GREEN_T = "#2c3a04" if DARK else "#eef7d4"
AMBER_T = "#38104f" if DARK else "#f3e3ff"
BLUE_T = "#063540" if DARK else "#d9f3f8"
RED_T = "#4c1414" if DARK else "#fde2e2"
CLASS_SCALE = alt.Scale(domain=["primary", "comparison", "competitor",
                                "neutral"],
                        range=[LIME, CYAN, VPURPLE, "#7d8590"])

st.markdown(f"""
<style>
div[data-testid="stMetric"] {{
  background: {"#191022" if DARK else "#f7f3fa"};
  border: 1px solid {"#2e1b3d" if DARK else "#e2d6ec"};
  border-radius: 12px; padding: 12px 14px 8px 14px;
}}
section[data-testid="stSidebar"] {{
  background: {"linear-gradient(180deg,#1a0f24 0%,#120a18 100%)"
               if DARK else "linear-gradient(180deg,#f6f1f9 0%,#efe7f4 100%)"};
  border-right: 1px solid {"#2e1b3d" if DARK else "#e2d6ec"};
}}
.pd-banner {{
  background: linear-gradient(120deg,#481056 0%,#241031 55%,#12091a 100%);
  border: 1px solid #5d1a6e; border-radius: 14px;
  padding: 14px 22px; margin-bottom: 10px; color: #fff;
}}
.pd-banner .t {{ font-size: 1.45rem; font-weight: 800; }}
.pd-banner .s {{ color: #d9c9e8; font-size: .85rem; margin-top: 2px; }}
.app-title {{ font-size: 1.9rem; font-weight: 800; line-height: 1.15;
  letter-spacing: -.01em; margin: 2px 0 10px 0; }}
.app-title .rs-accent {{ color: {ORANGE}; }}
.badge {{ display:inline-block; padding: 3px 12px; border-radius: 999px;
  font-weight: 800; font-size: .8rem; letter-spacing: .06em; }}
.badge-live {{ background:#B8E900; color:#1a2400; }}
.badge-sample {{ background:#ff5252; color:#fff; }}
.badge-imported {{ background:#FD9904; color:#2b1600; }}
.idrow {{ color:{"#b9a6cc" if DARK else "#5d4a70"}; font-size:.82rem; }}
</style>""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Session / selection helpers
# --------------------------------------------------------------------------- #

def studies():
    return ds.list_studies()


def current_study():
    sid = st.session_state.get("study_id")
    for s in studies():
        if s["study_id"] == sid:
            return s
    return None


def set_study(study):
    st.session_state["study_id"] = study["study_id"]


def save_study(study):
    ds.save_study(study)


def selected_dataset():
    s = current_study()
    if not s:
        return None, None
    dsets = ds.list_datasets(s["study_id"])
    if not dsets:
        return s, None
    did = st.session_state.get("dataset_id")
    for m in dsets:
        if m["dataset_id"] == did:
            return s, m
    return s, dsets[-1]


def pricing():
    return migrate.legacy_pricing()


# --------------------------------------------------------------------------- #
# Sidebar navigation
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.markdown("<div class='app-title'>🔎 Retrieval "
                "<span class='rs-accent'>Studio</span></div>",
                unsafe_allow_html=True)
    slist = studies()
    if slist:
        names = {f'{s["study_name"]}': s for s in slist}
        cur = current_study()
        idx = list(names.values()).index(cur) if cur in names.values() else \
            len(names) - 1
        pick = st.selectbox("Study", list(names.keys()), index=idx)
        set_study(names[pick])
    else:
        st.caption("No runs yet — start on Quick Run.")

    # In-memory deployment: a run writes nothing to the server, so the
    # disk-backed Analyse pages are hidden — everything is on Quick Run.
    page = "Quick Run"

    st.divider()
    if HOSTED:
        st.divider()
        st.caption("🌐 Hosted session — your studies are temporary and are "
                   "deleted when the session expires. Bring your own OpenAI "
                   "key; it is never stored.")
        cc1, cc2 = st.columns(2)
        if cc1.button("Clear studies"):
            hosting.clear_session(st.session_state["_sid"])
            for k in ("study_id", "dataset_id"):
                st.session_state.pop(k, None)
            st.rerun()
        if cc2.button("Delete all my data"):
            hosting.clear_session(st.session_state["_sid"])
            for k in list(st.session_state.keys()):
                if k != "_access_ok":
                    st.session_state.pop(k, None)
            st.rerun()

    theme_pick = st.selectbox("Theme", ["dark", "light", "system"],
                              index=["dark", "light", "system"].index(
                                  settings.get("theme", "dark")))
    if theme_pick != settings.get("theme", "dark"):
        settings["theme"] = theme_pick
        _save_settings(settings)   # data/ui_settings.json (gitignored)
        st.session_state["theme_changed"] = True
        st.rerun()
    if st.session_state.pop("theme_changed", False):
        st.info("Theme preference saved (under data/). If widget colours "
                "don't fully switch, restart the app — the tracked "
                ".streamlit/config.toml is never modified at runtime.")


# --------------------------------------------------------------------------- #
# Shared UI pieces
# --------------------------------------------------------------------------- #

def flash(msg: str):
    """Queue a confirmation that survives st.rerun()."""
    st.session_state["_flash"] = msg


if "_flash" in st.session_state:
    st.success(st.session_state.pop("_flash"))


def style_rows(df, color_fn):
    def _apply(row):
        c = color_fn(row)
        return [f"background-color: {c}" if c else "" for _ in row]
    return df.style.apply(_apply, axis=1)


def identity_header(study, manifest):
    """Study identity header — the live/sample status is unmissable."""
    t = manifest.get("dataset_type", "?")
    badge = {"live": "badge-live", "sample": "badge-sample",
             "imported": "badge-imported"}.get(t, "badge-imported")
    tot = manifest.get("totals", {})
    actual = ", ".join(manifest.get("actual_models", [])) or "—"
    locs = "; ".join(ds.format_location(L)
                     for L in manifest.get("locations", [])
                     if ds.format_location(L)) or "—"
    st.markdown(f"""
<div class="pd-banner">
  <span class="badge {badge}">{t.upper()} DATASET</span>
  <span class="t">&nbsp;{study["study_name"]}</span>
  <div class="s">{manifest["dataset_id"]} · created {manifest.get("created_at","")}</div>
  <div class="idrow">requested <b>{manifest.get("requested_model","—")}</b> ·
  actual <b>{actual}</b> · {manifest.get("search_mode","")} · {locs} ·
  {len(ds.dataset_prompts(study["study_id"], manifest["dataset_id"]))} prompts ×
  {manifest.get("runs_per_prompt","?")} runs ·
  ok {tot.get("ok",0)} / failed {tot.get("failed",0)} ·
  cost ${tot.get("cost_usd",0)}</div>
</div>""", unsafe_allow_html=True)


def dataset_picker(study):
    dsets = ds.list_datasets(study["study_id"])
    if not dsets:
        st.info("This study has no datasets yet. Run it (Review & Run) or "
                "load the sample study from Study Setup.")
        return None
    labels = {f'{m["dataset_id"]}  ·  {m["dataset_type"].upper()}  ·  '
              f'{m.get("totals",{}).get("observations",0)} obs': m
              for m in dsets}
    keys = list(labels.keys())
    cur = st.session_state.get("dataset_id")
    idx = next((i for i, m in enumerate(labels.values())
                if m["dataset_id"] == cur), len(keys) - 1)
    pick = st.selectbox("Dataset (every number below comes from this dataset "
                        "only)", keys, index=idx)
    m = labels[pick]
    st.session_state["dataset_id"] = m["dataset_id"]
    return m


def need_dataset():
    s = current_study()
    if not s:
        st.warning("Create or select a study first (Study Setup).")
        st.stop()
    m = dataset_picker(s)
    if m is None:
        st.stop()
    identity_header(s, m)
    conn = open_db(ds.dataset_db_path(s["study_id"], m["dataset_id"]))
    return s, m, conn


# --------------------------------------------------------------------------- #
# BUILD — Quick Run (the default landing: prompts -> model -> run)
# --------------------------------------------------------------------------- #

if page == "Quick Run":
    st.header("Quick Run")
    st.caption("Type one or more prompts, pick a model, run. Each run "
               "creates its own fresh study + live dataset, fully analysable "
               "under Analyse. Use the Build pages instead when you need a "
               "managed prompt library, tracked entities or multi-location "
               "design.")
    qp = st.text_area("Prompts — one per line", height=150, key="qr_prompts",
                      placeholder="recommend a few agencies that can help me "
                                  "get cited by ChatGPT\nbest SEO agencies "
                                  "in Melbourne")
    c1, c2, c3 = st.columns(3)
    q_model = c1.selectbox("Model", MODELS, key="qr_model")
    model_cost_note(c1, q_model)
    q_runs = c2.number_input("Runs per prompt", 1, 20, 3, key="qr_runs",
                             help="Repeat runs turn one-off answers into "
                                  "rates.")
    q_loc = c3.text_input("Location (City, Region, CC — or just a country)",
                          "Melbourne, Victoria, AU", key="qr_loc")
    q_ent_text = st.text_area(
        "Track brands / competitors (optional — one per line; the first is "
        "yours, the rest are competitors)", key="qr_entities", height=90,
        placeholder="One brand or competitor per line")

    q_prompts = [l.strip() for l in (qp or "").splitlines() if l.strip()]
    q_parsed = ds.parse_location_string(q_loc)
    loc_ok = q_parsed is not None
    q_total = len(q_prompts) * int(q_runs)
    q_rate = pricing().get(q_model, {})
    q_est = q_total * ((28000 / 1e6) * q_rate.get("input_per_mtok", 0)
                       + (1300 / 1e6) * q_rate.get("output_per_mtok", 0)
                       + 9 * q_rate.get("per_web_search_call", 0.01))
    st.caption(f"**{len(q_prompts)} prompt(s) × {int(q_runs)} runs = "
               f"{q_total} live calls · estimated ~${q_est:.2f}**")
    if not loc_ok and q_loc.strip():
        st.error("Location not recognised — use 'City, Region, CC', "
                 "'City, CC', or just a country (e.g. Australia).")

    env_key = os.environ.get("OPENAI_API_KEY", "")
    q_typed = "" if env_key else st.text_input(
        "OpenAI API key", type="password", key="qr_key",
        help="Masked; session-only; never logged or exported.")
    if not env_key:
        st.caption("🔒 Your API key is sent directly to OpenAI and is not "
                   "stored by this application.")
        forget_key_button("qr_key")
    q_key = env_key or q_typed.strip()
    if q_total > 500:
        st.error(f"{q_total} calls exceeds the safety cap of 500 per run.")
        st.stop()
    q_big = q_est > 2.0 or q_total > 20
    q_confirm = st.checkbox(
        f"I confirm this run (~{q_total} calls, est. ~${q_est:.2f})",
        key="qr_confirm") if q_big else True

    if st.button("Run now", type="primary",
                 disabled=not (q_prompts and q_key and loc_ok and q_confirm)):
        ok_key, key_msg = validate_api_key(q_key)
        if not ok_key:
            st.error(key_msg)
            st.stop()
        from openai import OpenAI
        _ent_names, _seen = [], set()
        for _l in (q_ent_text or "").splitlines():
            _n = _l.strip()
            if _n and _n.lower() not in _seen:
                _seen.add(_n.lower())
                _ent_names.append(_n)
        entities = [new_entity(_n,
                               role=("primary" if _i == 0 else "competitor"))
                    for _i, _n in enumerate(_ent_names)]
        st.session_state["qr_last_entities"] = list(_ent_names)
        client = OpenAI(api_key=q_key, timeout=240, max_retries=1)
        prog = st.progress(0.0)
        stat = st.empty()

        def qcb(info):
            prog.progress(min(info["done"] / max(info["total"], 1), 1.0))
            stat.markdown(
                f'**{info["done"]}/{info["total"]}** · prompt '
                f'{info["prompt_id"]} run {info["run_number"]} · ok '
                f'{info["ok"]} · failed {info["failed"]} · '
                f'{info["elapsed_s"]}s · ${info["cost"]}')

        # Privacy by design: run into a throwaway temp workspace and delete it
        # immediately afterwards, so NOTHING is written to the server's
        # persistent store and no other visitor can ever see this run. The
        # results (rows + raw) are captured in memory for the dashboard and
        # download below.
        import tempfile as _tf
        import shutil as _sh
        from core.extraction import extract_observation as _extract
        from collections import Counter as _Counter
        _tmp = _tf.mkdtemp(prefix="rs_run_")
        _prev_root = ds._session_root.get()
        ds.set_session_root(os.path.join(_tmp, "studies"))
        try:
            study = ds.create_study(
                f"Quick run {time.strftime('%Y-%m-%d %H%M')}",
                mode=("multi" if len(entities) > 1 else
                      "single" if entities else "unbranded"),
                entities=entities, prompts=pr.from_lines(qp),
                locations=[q_parsed], prompt_set_version="quick-v1",
                notes="Created by Quick Run.")
            manifest = ds.create_dataset(
                study, "live", requested_model=q_model,
                runs_per_prompt=int(q_runs), reasoning={"summary": "auto"})
            res = runner.run_batch(client, study, manifest, pricing(),
                                   progress=qcb)
            # In-memory rollups for the on-page dashboard.
            _qc, _dc = _Counter(), _Counter()
            for _it in res.get("raw") or []:
                _rawr = _it.get("response")
                if not _rawr:
                    continue
                try:
                    _ex = _extract(_rawr, None)
                except Exception:
                    continue
                for _q in _ex.get("fanout_queries", []):
                    _qt = (_q.get("query") or "").strip()
                    if _qt:
                        _qc[_qt] += 1
                for _c in _ex.get("citations", []):
                    _dom = _c.get("domain") or ""
                    if _dom:
                        _dc[_dom] += 1
            res["_top_queries"] = _qc.most_common(5)
            res["_top_domains"] = _dc.most_common(5)
            st.session_state["qr_last"] = res
        finally:
            ds.set_session_root(_prev_root)
            _sh.rmtree(_tmp, ignore_errors=True)

    # Run result + in-memory download. Rendered OUTSIDE the Run-now button so it
    # survives the rerun a download click triggers, and so your data is handed
    # to you directly here — never dependent on server-side storage.
    _last = st.session_state.get("qr_last")
    if _last:
        if _last.get("aborted"):
            st.error(f'Run ABORTED — {redact(_last.get("abort_reason") or "")}')
        else:
            _rows = _last.get("rows") or []
            _ok = sum(1 for r in _rows if r.get("status") == "ok")
            _failed = len(_rows) - _ok
            st.success(f'Run complete — {_ok} ok, {_failed} failed, '
                       f'${_last.get("cost")}. Everything below is computed here '
                       f'in your browser and downloadable — nothing is stored '
                       f'on the server.')
            m1, m2, m3 = st.columns(3)
            m1.metric("Runs", len(_rows))
            m2.metric("Answers OK", _ok)
            m3.metric("Cost (USD)", f'${_last.get("cost")}')

            # Did your brand / competitors get mentioned in the answers?
            _ents = st.session_state.get("qr_last_entities") or []
            if _ents and _rows:
                st.markdown("**Did your brand get mentioned?**")
                for _i, _nm in enumerate(_ents):
                    _c = sum(1 for _r in _rows if _nm.lower()
                             in (_r.get("answer_text") or "").lower())
                    _who = " *(your brand)*" if _i == 0 else ""
                    _tick = "✅" if _c else "❌"
                    st.write(f"- {_tick} **{_nm}**{_who} — mentioned in "
                             f"{_c}/{len(_rows)} answers")

            # Top fan-out queries the model ran.
            _tq = _last.get("_top_queries") or []
            if _tq:
                st.markdown("**Top fan-out queries** — what the model searched")
                for _q, _n in _tq:
                    st.write(f"- {_q}  ·  {_n}×")

            # Top domains cited in the answers.
            _td = _last.get("_top_domains") or []
            if _td:
                st.markdown("**Top cited sites** — domains cited in the answers")
                for _d, _n in _td:
                    st.write(f"- {_d}  ·  {_n} citation(s)")

            st.markdown("**Download this run** — data is not kept on the server")
            _bundle = json.dumps(
                {"rows": _rows, "raw": _last.get("raw") or []},
                ensure_ascii=False, indent=2).encode("utf-8")
            d1, d2 = st.columns(2)
            d1.download_button(
                "⬇ Full data + raw (JSON)", _bundle,
                file_name="retrieval-studio-run.json",
                mime="application/json", type="primary")
            if _rows:
                _df = pd.DataFrame(_rows)
                d2.download_button(
                    "⬇ Runs table (CSV)",
                    _df.to_csv(index=False).encode("utf-8"),
                    file_name="retrieval-studio-run.csv", mime="text/csv")
                _pv = [c for c in ("prompt_id", "run_number", "actual_model",
                                   "status", "num_citations",
                                   "num_search_actions", "cost_usd")
                       if c in _df.columns]
                with st.expander("Preview the runs table"):
                    st.dataframe(_df[_pv] if _pv else _df, width="stretch")

# --------------------------------------------------------------------------- #
# BUILD — Study Setup
# --------------------------------------------------------------------------- #

elif page == "Study Setup":
    st.header("Study Setup")
    with st.form("study_form"):
        name = st.text_input("Study name", value=(current_study() or {}).get(
            "study_name", ""))
        mode = st.selectbox("Study mode", ["unbranded", "single", "multi"],
                            index=["unbranded", "single", "multi"].index(
                                (current_study() or {}).get("mode",
                                                            "unbranded")),
                            help="Unbranded research needs no tracked entity; "
                                 "single tracks one; multi compares several.")
        psv = st.text_input("Prompt-set version label",
                            value=(current_study() or {}).get(
                                "prompt_set_version", "v1"))
        notes = st.text_area("Notes", value=(current_study() or {}).get(
            "notes", ""), height=80)
        c1, c2 = st.columns(2)
        create = c1.form_submit_button("Create new study", type="primary")
        update = c2.form_submit_button("Update selected study")
    if create and name.strip():
        s = ds.create_study(name.strip(), mode=mode, notes=notes,
                            prompt_set_version=psv)
        set_study(s)
        flash(f'Study created: {s["study_id"]}')
        st.rerun()
    if update and current_study():
        s = current_study()
        s.update(study_name=name.strip() or s["study_name"], mode=mode,
                 notes=notes, prompt_set_version=psv)
        save_study(s)
        st.success("Study updated.")

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Sample study")
        st.caption("Creates a SEPARATE study + dataset from labelled test "
                   "fixtures. Sample rows can never enter a live dataset.")
        if st.button("Create sample study (free, no API)"):
            out = migrate.build_sample_dataset()
            set_study(out["study"])
            st.session_state["dataset_id"] = out["manifest"]["dataset_id"]
            flash("Sample study created — open Analyse ▸ Overview.")
            st.rerun()
    with c2:
        st.subheader("Import v1 data")
        legacy = os.path.join(HERE, "output", "raw")
        if HOSTED:
            st.caption("Disabled in hosted mode.")
        elif os.path.isdir(legacy):
            st.caption("Found the v1 flat output/. Importing splits it into "
                       "one dataset per batch (requested model), reprocessed "
                       "from raw with v2 rules.")
            if st.button("Import v1 output/ as datasets"):
                with st.spinner("Reprocessing raw responses..."):
                    out = migrate.migrate_legacy()
                set_study(out["study"])
                flash(f'Imported {out["observations"]} observations into '
                      f'{len(out["datasets"])} datasets.')
                st.rerun()
        else:
            st.caption("No v1 output/ directory found.")

# --------------------------------------------------------------------------- #
# BUILD — Prompts
# --------------------------------------------------------------------------- #

elif page == "Prompts":
    s = current_study()
    if not s:
        st.warning("Create a study first.")
        st.stop()
    st.header("Prompts")
    st.caption(f'Library for **{s["study_name"]}** — '
               f'{len(s.get("prompts", []))} prompts · '
               f'set version {s.get("prompt_set_version","v1")} · '
               f'hash {ds.prompt_set_hash(s.get("prompts", []))}')

    t_add, t_bulk, t_upload, t_lib = st.tabs(
        ["Add one prompt", "Add multiple", "Upload Excel / CSV",
         "Library (view & edit)"])

    with t_add:
        with st.form("add_one"):
            pid = st.text_input("Prompt ID (auto if blank)")
            text = st.text_area("Prompt text", height=90)
            c1, c2, c3 = st.columns(3)
            cat = c1.text_input("Category")
            intent = c2.text_input("Intent")
            loc = c3.text_input("Location override (City, Region, CC — "
                                "or just a country)")
            notes_p = st.text_input("Notes")
            active = st.checkbox("Active", value=True)
            if st.form_submit_button("Add prompt", type="primary") \
                    and text.strip():
                s["prompts"].append(pr.new_prompt(
                    text, prompt_id=pid.strip(), category=cat, intent=intent,
                    location=loc, active=active, notes=notes_p))
                pr.assign_ids(s["prompts"])
                save_study(s)
                flash(f"Prompt added — library now has {len(s['prompts'])} prompts.")
                st.rerun()

    with t_bulk:
        bulk = st.text_area("One prompt per line", height=160)
        if st.button("Add pasted prompts") and bulk.strip():
            newps = pr.from_lines(bulk)
            existing = {p["prompt_text"].strip().lower()
                        for p in s["prompts"]}
            added = [p for p in newps
                     if p["prompt_text"].strip().lower() not in existing]
            for p in added:
                p["prompt_id"] = ""
            s["prompts"].extend(added)
            pr.assign_ids(s["prompts"])
            save_study(s)
            flash(f"Added {len(added)} prompts "
                  f"({len(newps)-len(added)} duplicates skipped).")
            st.rerun()

    with t_upload:
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Excel template",
                           exports.prompt_template_bytes("xlsx"),
                           "prompt_template.xlsx")
        c2.download_button("⬇ CSV template",
                           exports.prompt_template_bytes("csv"),
                           "prompt_template.csv")
        up = st.file_uploader("Upload prompts", type=["xlsx", "csv", "txt"])
        if up is not None:
            try:
                parsed, unsupported, blanks = pr.parse_upload(up)
            except pr.UploadError as e:
                st.error(str(e))
                parsed = None
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not read {up.name}: {redact(str(e))}")
                parsed = None
            if parsed is not None:
                v = pr.validate(parsed, unsupported, blanks)
                st.markdown("**Validation preview**")
                for label, key in [("Missing prompt text", "missing_text"),
                                   ("Duplicate IDs", "duplicate_ids"),
                                   ("Duplicate prompts", "duplicate_prompts"),
                                   ("Invalid locations", "invalid_locations"),
                                   ("Unsupported fields",
                                    "unsupported_fields")]:
                    vals = v[key]
                    (st.error if key in ("missing_text", "duplicate_ids")
                     and vals else st.warning if vals else st.success)(
                        f"{label}: {vals if vals else 'none'}")
                if v["blank_rows"]:
                    st.warning(f"Blank rows skipped: {v['blank_rows']}")
                st.dataframe(pd.DataFrame(parsed), width="stretch",
                             height=240)
                if st.button("Import these prompts",
                             disabled=not v["ok"], type="primary"):
                    s["prompts"] = parsed
                    save_study(s)
                    flash(f"Imported {len(parsed)} prompts (replaced library). "
                          "Bump the prompt-set version label in Study Setup "
                          "if this is a new set.")
                    st.rerun()

    with t_lib:
        if not s["prompts"]:
            st.info("Library is empty.")
        else:
            df = pd.DataFrame(s["prompts"])
            edited = st.data_editor(
                df, width="stretch", num_rows="dynamic",
                column_config={"active": st.column_config.CheckboxColumn()},
                key="prompt_editor")
            c1, c2 = st.columns([1, 3])
            if c1.button("Save library", type="primary"):
                newlib = [pr.new_prompt(
                    r.get("prompt_text", ""), prompt_id=r.get("prompt_id", ""),
                    category=r.get("category", ""), intent=r.get("intent", ""),
                    location=r.get("location", ""),
                    active=bool(r.get("active", True)),
                    notes=r.get("notes", ""))
                    for _, r in edited.iterrows()
                    if str(r.get("prompt_text", "")).strip()]
                pr.assign_ids(newlib)
                v = pr.validate(newlib)
                if not v["ok"]:
                    st.error(f"Not saved — fix first: {v}")
                else:
                    s["prompts"] = newlib
                    save_study(s)
                    flash(f"Library saved — {len(newlib)} prompts.")
                    st.rerun()
            v = pr.validate(s["prompts"])
            if v["duplicate_prompts"]:
                c2.warning(f"Duplicate prompt text: {v['duplicate_prompts']}")

# --------------------------------------------------------------------------- #
# BUILD — Locations
# --------------------------------------------------------------------------- #

elif page == "Locations":
    s = current_study()
    if not s:
        st.warning("Create a study first.")
        st.stop()
    st.header("Locations")
    st.caption("Approximate user location for web search. The full prompt "
               "set runs once per location.")
    txt = st.text_area(
        "One location per line — 'City, Region, CC', 'City, CC', or just a "
        "country ('Australia' / 'AU')",
        "\n".join(ds.format_location(L) for L in s.get("locations", [])),
        height=120)
    if st.button("Save locations", type="primary"):
        locs = []
        for line in txt.splitlines():
            if not line.strip():
                continue
            parsed = ds.parse_location_string(line)
            if parsed is None:
                st.error(f"Bad line: {line!r} (need at least a country)")
                st.stop()
            locs.append(parsed)
        s["locations"] = locs or [{"city": "Melbourne", "region": "Victoria",
                                   "country": "AU"}]
        save_study(s)
        st.success(f"Saved {len(s['locations'])} location(s).")

# --------------------------------------------------------------------------- #
# BUILD — Tracked Entities
# --------------------------------------------------------------------------- #

elif page == "Tracked Entities":
    s = current_study()
    if not s:
        st.warning("Create a study first.")
        st.stop()
    st.header("Tracked Entities")
    st.caption("Businesses, brands, people, products or domains to measure. "
               "Unbranded studies can leave this empty. Roles are yours to "
               "set — nothing is auto-classified as a competitor.")
    for prob in ds.validate_study(s):
        st.warning(prob)
    if s.get("mode") == "unbranded":
        st.info("This study is UNBRANDED — entity tracking is off. Switch "
                "mode in Study Setup to track entities.")
    ents = s.get("entities", [])
    if ents:
        df = pd.DataFrame([{**e,
                            "alt_domains": "; ".join(e.get("alt_domains", [])),
                            "aliases": "; ".join(e.get("aliases", []))}
                           for e in ents])
        edited = st.data_editor(
            df, width="stretch", num_rows="dynamic",
            column_config={"role": st.column_config.SelectboxColumn(
                options=["primary", "comparison", "competitor", "neutral"])},
            key="entity_editor")
        if st.button("Save entities", type="primary"):
            new = []
            for _, r in edited.iterrows():
                if not str(r.get("name", "")).strip():
                    continue
                new.append(new_entity(
                    r.get("name", ""), primary_domain=r.get("primary_domain",
                                                            ""),
                    alt_domains=[d.strip() for d in
                                 str(r.get("alt_domains", "")).split(";")
                                 if d.strip()],
                    aliases=[a.strip() for a in
                             str(r.get("aliases", "")).split(";")
                             if a.strip()],
                    entity_type=r.get("entity_type", "business"),
                    role=r.get("role", "comparison"),
                    notes=r.get("notes", ""),
                    entity_id=r.get("entity_id", "")))
            s["entities"] = new
            save_study(s)
            flash(f"Saved {len(new)} entities.")
            st.rerun()
    with st.form("add_entity"):
        st.markdown("**Add entity**")
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Name")
        dom = c2.text_input("Primary domain")
        role = c3.selectbox("Role", ["primary", "comparison", "competitor",
                                     "neutral"])
        c4, c5, c6 = st.columns(3)
        alt = c4.text_input("Alt domains (; separated)")
        ali = c5.text_input("Aliases (; separated)")
        etype = c6.selectbox("Type", ["business", "brand", "person",
                                      "product", "domain"])
        notes_e = st.text_input("Notes", key="ent_notes")
        if st.form_submit_button("Add", type="primary") and name.strip():
            s.setdefault("entities", []).append(new_entity(
                name, primary_domain=dom,
                alt_domains=[d.strip() for d in alt.split(";") if d.strip()],
                aliases=[a.strip() for a in ali.split(";") if a.strip()],
                entity_type=etype, role=role, notes=notes_e))
            save_study(s)
            flash(f"Entity added: {name}")
            st.rerun()

# --------------------------------------------------------------------------- #
# BUILD — Review & Run
# --------------------------------------------------------------------------- #

elif page == "Review & Run":
    s = current_study()
    if not s:
        st.warning("Create a study first.")
        st.stop()
    st.header("Review & Run")

    c1, c2, c3 = st.columns(3)
    model = c1.selectbox("Requested model", MODELS)
    model_cost_note(c1, model)
    runs_pp = c2.number_input("Runs per prompt", 1, 50, 5)
    effort = c3.selectbox("Reasoning effort", ["default", "low", "medium",
                                               "high"])
    reasoning = {"summary": "auto"}
    if effort != "default":
        reasoning["effort"] = effort

    with st.expander("Advanced search settings (recorded in the manifest — "
                     "different settings are different experimental "
                     "conditions)"):
        a1, a2, a3 = st.columns(3)
        adv_ctx = a1.selectbox("Search context size",
                               ["default", "low", "medium", "high"])
        adv_tc = a2.selectbox("Tool choice", ["auto", "required"])
        adv_mtc = a3.number_input("Max tool calls (0 = default)", 0, 100, 0)
        adv_domains = st.text_input(
            "Allowed domains (comma-separated; blank = whole web)")
        adv_results = st.checkbox(
            "Also capture web_search_call.results (experimental, "
            "feature-detected)", value=False)
    search_settings = {}
    if adv_ctx != "default":
        search_settings["search_context_size"] = adv_ctx
    if adv_tc != "auto":
        search_settings["tool_choice"] = adv_tc
    if adv_mtc:
        search_settings["max_tool_calls"] = int(adv_mtc)
    doms = [d.strip() for d in adv_domains.split(",") if d.strip()]
    if doms:
        search_settings["allowed_domains"] = doms
    if adv_results:
        search_settings["include_results"] = True

    active = [p for p in s.get("prompts", []) if p.get("active", True)]
    locs = s.get("locations", [])
    total = len(active) * int(runs_pp) * max(len(locs), 1)
    est_in, est_out = 28000, 1300     # per-run token profile from live data
    rate = pricing().get(model, {})
    est_cost = total * ((est_in / 1e6) * rate.get("input_per_mtok", 0)
                        + (est_out / 1e6) * rate.get("output_per_mtok", 0)
                        + 9 * rate.get("per_web_search_call", 0.01))

    st.markdown(f"""
| | |
|---|---|
| Study type | **{s.get("mode")}** |
| Prompts (active / total) | **{len(active)} / {len(s.get("prompts", []))}** |
| Tracked entities | **{len(s.get("entities", []))}** |
| Locations | **{len(locs)}** |
| Requested model | **{model}** |
| Reasoning | **summary=auto{", effort="+effort if effort != "default" else ""}** |
| Search mode | **web_search : auto** |
| Runs per prompt | **{int(runs_pp)}** |
| Estimated max observations | **{total}** |
| Estimated cost | **~${est_cost:.2f}** (based on the observed ~28k-in / 1.3k-out per-run profile) |
""")
    if not active:
        st.error("No active prompts — add some under Prompts.")
        st.stop()
    problems = ds.validate_study(s)
    if problems:
        for p in problems:
            st.error(p)
        st.stop()

    env_key = os.environ.get("OPENAI_API_KEY", "")
    typed = "" if env_key else st.text_input(
        "OpenAI API key", type="password", key="rr_key",
        help="Masked; session-only; never logged or exported.")
    if not env_key:
        st.caption("🔒 Your API key is sent directly to OpenAI and is not "
                   "stored by this application.")
        forget_key_button("rr_key")
    api_key = env_key or typed.strip()

    if total > 500:
        st.error(f"{total} calls exceeds the safety cap of 500 per run — "
                 "reduce prompts, runs or locations.")
        st.stop()
    big = est_cost > 2.0 or total > 20
    confirm = st.checkbox(
        f"I confirm this live run (~{total} calls, est. ~${est_cost:.2f})",
        value=not big) if big else True

    st.caption("Every run creates its own NEW live dataset — appending to an "
               "existing dataset is not supported (it breaks run-count and "
               "prompt-set integrity).")
    target = None

    if st.button("Start live run", type="primary",
                 disabled=not (api_key and confirm)):
        ok_key, key_msg = validate_api_key(api_key)
        if not ok_key:
            st.error(key_msg)
            st.stop()
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=240, max_retries=1)
        manifest = target or ds.create_dataset(
            s, "live", requested_model=model, runs_per_prompt=int(runs_pp),
            reasoning=reasoning, search_settings=search_settings)
        st.session_state["dataset_id"] = manifest["dataset_id"]
        prog = st.progress(0.0)
        stat = st.empty()

        def cb(info):
            prog.progress(min(info["done"] / max(info["total"], 1), 1.0))
            stat.markdown(
                f'**{info["done"]}/{info["total"]}** · prompt '
                f'{info["prompt_id"]} run {info["run_number"]} · '
                f'{info["location"]} · ok {info["ok"]} · failed '
                f'{info["failed"]} · elapsed {info["elapsed_s"]}s · '
                f'cost ${info["cost"]}')

        res = runner.run_batch(client, s, manifest, pricing(), progress=cb)
        if res["aborted"]:
            st.error(f'Run ABORTED — {redact(res["abort_reason"])}  '
                     f'(no calls continued under a substituted model).')
        else:
            st.success(f'Done: {res["ok"]} ok, {res["failed"]} failed, '
                       f'${res["cost"]}. Open Analyse ▸ Overview.')
        if not res.get("reasoning_supported", True):
            st.info("The API rejected the reasoning parameter for this "
                    "model; runs completed without reasoning summaries "
                    "(model unchanged).")

# --------------------------------------------------------------------------- #
# ANALYSE pages
# --------------------------------------------------------------------------- #

elif page == "Overview":
    s, m, conn = need_dataset()
    ov = analysis.overview(conn)
    r1 = st.columns(6)
    r1[0].metric("Observations", ov["observations"])
    r1[1].metric("Failed", ov["failed"])
    r1[2].metric("Search-trigger rate", f'{ov["search_trigger_rate"]:.0%}')
    r1[3].metric("Avg fan-outs / run", f'{ov["avg_fanout_per_run"]:.1f}')
    r1[4].metric("site: share", f'{ov["site_share"]:.0%}')
    r1[5].metric("Cost (USD)", f'${ov["cost_usd"]}')
    r2 = st.columns(6)
    r2[0].metric("Unique source domains", ov["unique_source_domains"])
    r2[1].metric("Pages opened", ov["pages_opened"])
    r2[2].metric("Citations", ov["total_citations"])
    r2[3].metric("Unique cited URLs", ov["unique_cited_urls"])
    if ov["entities_tracked"]:
        r2[4].metric("Entity mention rate",
                     f'{(ov["entity_mention_rate"] or 0):.0%}')
        r2[5].metric("Entity citation rate",
                     f'{(ov["entity_citation_rate"] or 0):.0%}')
    if ov["queries_missing_text"]:
        st.caption(f'{ov["queries_missing_text"]} search action(s) returned '
                   'without query text (shown as such — never inferred).')

    st.markdown("#### Retrieval journey")
    st.caption("Observable stage volumes. Stages are related but NOT a "
               "strict conversion funnel.")
    stages = pd.DataFrame(analysis.retrieval_journey(conn),
                          columns=["stage", "count"])
    ch = (alt.Chart(stages).mark_bar(color=CYAN)
          .encode(x=alt.X("count:Q", title=None),
                  y=alt.Y("stage:N", sort=None, title=None),
                  tooltip=["stage", "count"]).properties(height=260))
    st.altair_chart(ch, width="stretch")
    conn.close()

elif page == "Search & Evidence Traces":
    s, m, conn = need_dataset()
    st.markdown("#### Search & Evidence Trace")
    st.caption("The observable retrieval process per run. Reasoning "
               "summaries, when shown, are model-generated summaries — not "
               "the model's hidden raw chain of thought.")
    obs = [dict(r) for r in conn.execute(
        "SELECT * FROM observations ORDER BY prompt_id, run_number")]
    if not obs:
        st.info("No observations.")
        st.stop()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    ent_names = {e["entity_id"]: e["name"] for e in ents}
    c1, c2, c3 = st.columns([2, 1, 2])
    pids = sorted({o["prompt_id"] for o in obs})
    pid = c1.selectbox("Prompt", pids)
    runs_for = [o for o in obs if o["prompt_id"] == pid]
    key = "trace_idx_" + pid
    idx = st.session_state.get(key, 0) % len(runs_for)
    rsel = c2.selectbox("Run", [f'run {o["run_number"]}' for o in runs_for],
                        index=idx)
    idx = [f'run {o["run_number"]}' for o in runs_for].index(rsel)
    ent_filter = c3.selectbox("Highlight entity",
                              ["(all)"] + [ent_names[e] for e in ent_names])
    b1, b2, _ = st.columns([1, 1, 6])
    if b1.button("◀ previous run"):
        idx = (idx - 1) % len(runs_for)
    if b2.button("next run ▶"):
        idx = (idx + 1) % len(runs_for)
    st.session_state[key] = idx
    o = runs_for[idx]
    oid = o["observation_id"]

    k = st.columns(7)
    k[0].metric("Status", o["status"])
    k[1].metric("Queries", o["num_queries"] or 0)
    k[2].metric("site: probes", o["num_site_queries"] or 0)
    k[3].metric("Sources returned", o["num_search_sources"] or 0)
    k[4].metric("Pages opened", o["num_open_page"] or 0)
    k[5].metric("Citations", o["num_citations"] or 0)
    cost_v = o.get("cost_usd")
    k[6].metric("Cost / latency",
                f'${cost_v or 0:.3f} · {(o.get("latency_ms") or 0)/1000:.1f}s')

    st.markdown(f'**1 · Original prompt** — {o["prompt_text"]}')
    st.caption(f'2 · {o["timestamp"]} · {o["location"]} · requested '
               f'{o["requested_model"]} · actual {o["actual_model"] or "—"}')
    st.markdown("**3 · Reasoning summary**")
    if o.get("reasoning_summary"):
        st.info(o["reasoning_summary"])
        st.caption("This is a model-generated reasoning summary, not the "
                   "model's hidden raw chain of thought.")
    else:
        st.caption("No reasoning summary returned.")

    events = [dict(r) for r in conn.execute(
        "SELECT * FROM trace_events WHERE observation_id=? ORDER BY seq",
        (oid,))]
    queries = [dict(r) for r in conn.execute(
        "SELECT * FROM fanout_queries WHERE observation_id=? ORDER BY seq",
        (oid,))]
    q_by_seq = {}
    for q in queries:
        q_by_seq.setdefault(q["seq"], []).append(q)
    srcs = [dict(r) for r in conn.execute(
        "SELECT * FROM search_sources WHERE observation_id=? ORDER BY seq",
        (oid,))]
    src_by_action = {}
    for x in srcs:
        src_by_action.setdefault(x["action_seq"], []).append(x)
    batches = sorted({q["batch_index"] for q in queries})
    bpick = st.selectbox("Search batch", ["(all)"] + [f"batch {b+1}"
                                                      for b in batches])

    st.markdown("**4–11 · Retrieval trail (chronological)**")
    for e in events:
        et = e["event_type"]
        if et == "search":
            qs = q_by_seq.get(e["seq"], [])
            b = qs[0]["batch_index"] if qs else -1
            if bpick != "(all)" and f"batch {b+1}" != bpick:
                continue
            lines = []
            for q in qs:
                if q["query_missing"]:
                    lines.append("· *Search action returned without query "
                                 "text.*")
                else:
                    tag = ""
                    if q["has_site_operator"]:
                        tag += f'  `site:{q["site_domain"]}`'
                    hits = json.loads(q["entity_hits"] or "[]")
                    if hits:
                        tag += "  🎯 " + ", ".join(ent_names.get(h, h)
                                                   for h in hits)
                    lines.append(f'· `{q["query"]}`{tag}')
            n_src = len(src_by_action.get(e["seq"], []))
            st.markdown(f'🔎 **Search batch {b+1}** — {len(qs)} quer'
                        f'{"y" if len(qs)==1 else "ies"}, {n_src} source '
                        f'URL(s) returned\n' + "\n".join(lines))
            with_srcs = src_by_action.get(e["seq"], [])
            if with_srcs:
                with st.expander(f"Sources returned by batch {b+1} "
                                 f"({len(with_srcs)})"):
                    st.dataframe(pd.DataFrame(with_srcs)[
                        ["domain", "title", "raw_url"]],
                        width="stretch", hide_index=True)
        elif et == "open_page" and bpick == "(all)":
            p = json.loads(e["payload_json"])
            st.markdown(f'🌐 **Opened page** — {p.get("url", "")}')
        elif et == "find_in_page" and bpick == "(all)":
            p = json.loads(e["payload_json"])
            st.markdown(f'🔬 **In-page search** — `{p.get("pattern","")}` in '
                        f'{p.get("url","")}')

    st.markdown("**12 · Cited in the final answer**")
    cits = [dict(r) for r in conn.execute(
        "SELECT * FROM citations WHERE observation_id=? "
        "ORDER BY citation_count DESC", (oid,))]
    if cits:
        for c in cits:
            st.markdown(f'- [{c["title"] or c["canonical_url"]}]'
                        f'({c["raw_url"]}) ×{c["citation_count"]}')
    else:
        st.caption("— none —")

    if ents:
        st.markdown("**13–16 · Entities in this run**")
        er = [dict(r) for r in conn.execute(
            "SELECT * FROM entity_runs WHERE observation_id=?", (oid,))]
        rows = [{"entity": ent_names.get(x["entity_id"], x["entity_id"]),
                 "searched": bool(x["search_targeted"]),
                 "source returned": bool(x["source_returned"]),
                 "page opened": bool(x["page_opened"]),
                 "page searched": bool(x["page_searched"]),
                 "cited": bool(x["cited"]),
                 "named in answer": bool(x["named_in_answer"])} for x in er]
        if ent_filter != "(all)":
            rows = [r for r in rows if r["entity"] == ent_filter]
        st.dataframe(pd.DataFrame(rows), width="stretch",
                     hide_index=True)

    with st.expander("17 · Complete final answer"):
        st.write(o.get("answer_text") or "_empty_")
    st.caption(f'18 · tokens in/out {o.get("input_tokens") or 0}/'
               f'{o.get("output_tokens") or 0} · reasoning tokens '
               f'{o.get("reasoning_tokens") or 0} · latency '
               f'{(o.get("latency_ms") or 0)} ms · cost '
               f'${o.get("cost_usd") or 0}')
    conn.close()

elif page == "Prompt Coverage":
    s, m, conn = need_dataset()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    cov = analysis.prompt_coverage(conn, ents,
                                   ds.dataset_prompts(s["study_id"],
                                                      m["dataset_id"]))
    gaps = analysis.citation_gaps(conn, ents)
    gap_by_pid = {}
    for g in gaps:
        gap_by_pid.setdefault(g["prompt_id"], []).append(g["gap_type"])
    for row in cov:
        row["identified_gaps"] = "; ".join(
            dict.fromkeys(gap_by_pid.get(row["prompt_id"], [])))
    df = pd.DataFrame(cov)
    st.markdown("#### Prompt Coverage — primary analysis table")
    st.caption("Sortable/filterable. Rates are share-of-runs; entity columns "
               "refer to the primary entity.")
    st.dataframe(df, width="stretch", height=460)
    conn.close()

elif page == "Fan-Out Queries":
    s, m, conn = need_dataset()
    q = pd.read_sql_query(
        "SELECT observation_id, batch_index, query, query_missing, "
        "has_site_operator, site_domain, taxonomy, entity_hits "
        "FROM fanout_queries", conn)
    st.markdown(f"#### Fan-out queries — {len(q)} rows")
    tax = st.multiselect("Filter by cluster", sorted(q["taxonomy"].dropna()
                                                     .unique()))
    if tax:
        q = q[q["taxonomy"].isin(tax)]
    q["query"] = q.apply(lambda r: r["query"] if not r["query_missing"]
                         else "(search action returned without query text)",
                         axis=1)

    def qf(r):
        if r.get("entity_hits") and r["entity_hits"] != "[]":
            return GREEN_T
        if r.get("has_site_operator"):
            return BLUE_T
        return None
    st.dataframe(style_rows(q.drop(columns=["query_missing"]), qf),
                 width="stretch", height=480)
    conn.close()

elif page == "Sources":
    s, m, conn = need_dataset()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    entity_domains = [e.get("primary_domain") for e in ents
                      if e.get("primary_domain")]
    st.markdown("#### Domain leaderboard")
    st.caption("ONE row per canonical domain. `class` describes the domain "
               "itself; entity support is a separate dimension "
               "(entity_links).")
    lb = pd.DataFrame(analysis.domain_leaderboard(conn, entity_domains))
    if len(lb):
        def lf(r):
            if r.get("class") == "first_party":
                return GREEN_T
            if r.get("class") == "directory":
                return AMBER_T
            return None
        st.dataframe(style_rows(lb, lf), width="stretch", height=420)
        agg = lb.groupby("class", as_index=False)["citations"].sum()
        ch = (alt.Chart(agg).mark_bar(color=VPURPLE)
              .encode(x=alt.X("citations:Q"), y=alt.Y("class:N", sort="-x"),
                      tooltip=["class", "citations"]).properties(height=220))
        st.altair_chart(ch, width="stretch")
    tabs = st.tabs(["Search sources (returned)", "Opened pages",
                    "In-page searches", "Citations"])
    for t, table in zip(tabs, ["search_sources", "opened_pages",
                               "inpage_searches", "citations"]):
        with t:
            df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            st.dataframe(df, width="stretch", height=320)
    conn.close()

elif page == "Entity Comparison":
    s, m, conn = need_dataset()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    if not ents:
        st.info("Unbranded study — no tracked entities to compare.")
        st.stop()
    ev = analysis.entity_visibility(conn, ents)
    df = pd.DataFrame(ev)
    st.markdown("#### Entity visibility — one row per tracked entity")
    st.dataframe(df, width="stretch")
    melt = df.melt(["entity", "role"],
                   ["mention_rate", "search_targeting_rate",
                    "source_returned_rate", "page_opened_rate",
                    "page_searched_rate", "citation_rate",
                    "answer_mention_rate"], var_name="metric",
                   value_name="rate")
    ch = (alt.Chart(melt).mark_bar()
          .encode(x=alt.X("rate:Q", axis=alt.Axis(format="%"),
                          scale=alt.Scale(domain=[0, 1])),
                  y=alt.Y("entity:N", title=None),
                  color=alt.Color("role:N", scale=CLASS_SCALE),
                  row=alt.Row("metric:N", title=None),
                  tooltip=["entity", "metric",
                           alt.Tooltip("rate:Q", format=".0%")])
          .properties(height=28 * max(len(df), 1)))
    st.altair_chart(ch, width="stretch")
    conn.close()

elif page == "Citation Gaps":
    s, m, conn = need_dataset()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    gaps = analysis.citation_gaps(conn, ents)
    if not gaps:
        st.info("No gaps to report (no primary entity, or the primary is "
                "cited in every run).")
    else:
        st.markdown("#### Citation gaps — classified, evidence-linked")
        st.caption("Each row links back to the specific queries and winning "
                   "sources that support the diagnosis.")
        st.dataframe(style_rows(pd.DataFrame(gaps),
                                lambda r: RED_T if r.get(
                                    "primary_cited_runs") == 0 else AMBER_T),
                     width="stretch", height=480)
    conn.close()

elif page == "Query Clusters":
    s, m, conn = need_dataset()
    ents = ds.dataset_entities(s["study_id"], m["dataset_id"])
    cl = analysis.query_clusters(conn, ents)
    st.markdown(f"#### Query clusters — stable taxonomy "
                f"({len(cl)} of 15 categories present)")
    st.caption("Fixed top-level categories so trends compare across study "
               "periods.")
    df = pd.DataFrame(cl)
    if len(df):
        st.dataframe(df, width="stretch", height=380)
        ch = (alt.Chart(df).mark_bar(color=CYAN)
              .encode(x=alt.X("queries:Q"), y=alt.Y("cluster:N", sort="-x"),
                      tooltip=["cluster", "queries", "site_probes"])
              .properties(height=24 * len(df)))
        st.altair_chart(ch, width="stretch")
    conn.close()

elif page == "Exports":
    s, m, conn = need_dataset()
    conn.close()
    st.markdown("#### Exports — selected dataset only")
    checks = exports.reconcile(s["study_id"], m["dataset_id"])
    okall = exports.reconciliation_ok(checks)
    dfc = pd.DataFrame([{"check": n, "ok": "✅" if ok else "❌",
                         "detail": d} for n, ok, d in checks])
    st.dataframe(style_rows(dfc, lambda r: RED_T if r["ok"] == "❌"
                            else None), width="stretch", height=300)

    st.markdown("**Reports & dumps** (always available; they embed the "
                "reconciliation report)")
    d0, d1, d2 = st.columns(3)
    d0.download_button(
        "⬇ Insights report (.md)",
        exports.insights_markdown_bytes(s["study_id"], m["dataset_id"]),
        f'{m["dataset_id"]}_insights.md',
        help="ONE file with all insights from the run: key numbers, entity "
             "visibility, prompt coverage, classified gap recommendations, "
             "clusters, domain leaderboard, notable cited claims - without "
             "the per-run trace dumps.")
    d1.download_button(
        "⬇ Full study dump (.json)",
        exports.full_json_bytes(s["study_id"], m["dataset_id"]),
        f'{m["dataset_id"]}_full.json', mime="application/json",
        help="Manifest, study config, prompts, entities, every table row "
             "incl. ordered trace events, all analysis outputs, "
             "reconciliation.")
    d2.download_button(
        "⬇ Full study report (.md)",
        exports.full_markdown_bytes(s["study_id"], m["dataset_id"]),
        f'{m["dataset_id"]}_report.md',
        help="Human-readable: identity, all analysis tables, then every "
             "run's chronological trace, citations, entity flags and "
             "complete answer.")

    if not okall:
        st.error("Reconciliation FAILED — exports are blocked so a "
                 "misleading workbook cannot be produced. Reprocess the "
                 "dataset or investigate the failed checks.")
        if st.button("Reprocess dataset from raw"):
            n = ds.reprocess(s["study_id"], m["dataset_id"], pricing())
            st.success(f"Reprocessed {n} observations from raw.")
            st.rerun()
        st.stop()
    st.success("All reconciliation checks passed.")
    exp_dir = os.path.join(ds.dataset_dir(s["study_id"], m["dataset_id"]),
                           "exports")
    c1, c2, c3 = st.columns(3)
    if c1.button("Build study workbook (.xlsx)", type="primary"):
        path = os.path.join(exp_dir, "study_workbook.xlsx")
        exports.build_workbook(s["study_id"], m["dataset_id"], path)
        st.session_state["wb_path"] = path
    if st.session_state.get("wb_path") and \
            os.path.exists(st.session_state["wb_path"]):
        with open(st.session_state["wb_path"], "rb") as fh:
            c1.download_button("⬇ study_workbook.xlsx", fh.read(),
                               "study_workbook.xlsx")
    c2.download_button("⬇ Raw API JSON (zip)",
                       exports.raw_zip_bytes(s["study_id"], m["dataset_id"]),
                       f'{m["dataset_id"]}_raw.zip')
    c3.download_button("⬇ Data package (zip of CSVs)",
                       exports.data_package_bytes(s["study_id"],
                                                  m["dataset_id"]),
                       f'{m["dataset_id"]}_package.zip')
    c4, c5, c6 = st.columns(3)
    for col, table, label in [(c4, "fanout_queries", "fanout_queries.csv"),
                              (c5, "search_sources", "sources.csv"),
                              (c6, "citations", "citations.csv")]:
        col.download_button(f"⬇ {label}",
                            exports.table_csv_bytes(s["study_id"],
                                                    m["dataset_id"], table),
                            label)
    c7, c8, _ = st.columns(3)
    c7.download_button("⬇ Prompt template (.xlsx)",
                       exports.prompt_template_bytes("xlsx"),
                       "prompt_template.xlsx")
    c8.download_button("⬇ Prompt template (.csv)",
                       exports.prompt_template_bytes("csv"),
                       "prompt_template.csv")
