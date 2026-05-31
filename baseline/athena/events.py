"""Structured event logging for the Athena solver."""
from __future__ import annotations

import json
import os
import time

# -----------------------------------------------------------------------------
# Optional structured event log (mirrors the Hermes convention so existing
# eval tooling can pick traces up unchanged when OGC2026_EVENT_LOG is set).
# -----------------------------------------------------------------------------

_event_log_fh = None
_event_log_t0 = None


def _init_event_log(t0: float) -> None:
    global _event_log_fh, _event_log_t0
    _event_log_t0 = t0
    path = os.environ.get("OGC2026_EVENT_LOG")
    if not path:
        _event_log_fh = None
        return
    try:
        _event_log_fh = open(path, "a", buffering=1, encoding="utf-8")
    except Exception:
        _event_log_fh = None


def _close_event_log() -> None:
    global _event_log_fh
    if _event_log_fh is not None:
        try:
            _event_log_fh.close()
        except Exception:
            pass
        _event_log_fh = None


def _emit(event: str, **payload) -> None:
    if _event_log_fh is None:
        return
    try:
        rec = {"t": round(time.time() - _event_log_t0, 4), "event": event}
        rec.update(payload)
        _event_log_fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass
