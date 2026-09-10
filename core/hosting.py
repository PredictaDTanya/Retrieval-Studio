"""Deploy-mode + per-session isolation for hosted deployments.

Two modes (env `RETRIEVAL_MODE`):
  local  (default) — single user, persistent studies under data/studies/.
                     Exactly the desktop behaviour; nothing here changes it.
  hosted           — each browser session gets an isolated, TEMPORARY
                     workspace under data/sessions/{session_id}/studies/.
                     No visitor can see another's studies; workspaces are
                     garbage-collected after a TTL and on explicit clear.

Optional gate (env `RETRIEVAL_ACCESS_CODE`): when set in hosted mode, visitors
must enter the code before using the app — collapses the abuse surface for a
gated beta without CAPTCHA/rate-limiting.

This module is pure (no Streamlit import) so it stays unit-testable; the app
calls it to wire the active data root per run.
"""

from __future__ import annotations

import os
import shutil
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_ROOT = os.path.join(HERE, "data", "sessions")
DEFAULT_TTL_HOURS = 12


def deploy_mode() -> str:
    m = (os.environ.get("RETRIEVAL_MODE") or "local").strip().lower()
    return "hosted" if m == "hosted" else "local"


def is_hosted() -> bool:
    return deploy_mode() == "hosted"


def access_code() -> str:
    return (os.environ.get("RETRIEVAL_ACCESS_CODE") or "").strip()


def _safe_sid(session_id: str) -> str:
    """Never build a path directly from caller input."""
    return "".join(c for c in str(session_id) if c.isalnum() or c in "-_")[:64] \
        or "anon"


def session_workspace(session_id: str) -> str:
    """Absolute studies root for one hosted session (created on demand)."""
    root = os.path.join(SESSIONS_ROOT, _safe_sid(session_id), "studies")
    os.makedirs(root, exist_ok=True)
    _touch(os.path.dirname(root))
    return root


def _touch(path: str):
    try:
        os.utime(path, None)
    except OSError:
        pass


def clear_session(session_id: str) -> bool:
    """Delete one session's entire workspace (Clear this session / Delete all
    my data). Returns True if something was removed."""
    d = os.path.join(SESSIONS_ROOT, _safe_sid(session_id))
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def gc_sessions(ttl_hours: float = DEFAULT_TTL_HOURS) -> int:
    """Delete session workspaces whose dir mtime is older than the TTL.
    Returns the count removed. Safe to call opportunistically each run."""
    if not os.path.isdir(SESSIONS_ROOT):
        return 0
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for name in os.listdir(SESSIONS_ROOT):
        d = os.path.join(SESSIONS_ROOT, name)
        try:
            if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
