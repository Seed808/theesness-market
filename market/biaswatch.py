"""
Intraday bias tracker (added 2026-11-10, Kalani: "option (a) for this week").

DISPLAY + ALERT ONLY: this file never changes which cards get called. (Each re-scan already
re-reads ES/NQ/YM/VIX/SPY/QQQ live for its own rules; that's unchanged. The new CHOP reading
below is shown and alerted, not used by the rules yet.)

Every confirmation read and re-scan:
  live reading = CHOP   if SPY and QQQ are both within CHOP_VWAP_PCT of their VWAP and both have
                        a flat 15-minute trend,
                 else the read's own bias (BULL / BEAR / ROTATION / MIXED) from your rules.
  A different reading becomes the day's bias only after it shows up on two reads in a row
  (~20 minutes), so one spike can't flip it back and forth.
History is kept in ledger/bias/<date>.json; every change goes to Telegram.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402

BIAS_DIR = R.LEDGER / "bias"
CHOP_VWAP_PCT = 0.30          # 11/10 replay: 0.15 never triggered on a visibly choppy late morning


def _path(d) -> Path:
    return BIAS_DIR / f"{d.isoformat()}.json"


def load(d) -> dict:
    p = _path(d)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def chop_reading(raw: dict) -> tuple:
    """(is_chop, why) from SPY/QQQ technicals saved by the read."""
    parts, ok = [], True
    for sym in ("SPY", "QQQ"):
        t = (raw.get(sym) or {}).get("tech") or {}
        last, vwap, tr15 = t.get("last"), t.get("vwap"), (t.get("trend") or {}).get("15m")
        if not last or not vwap:
            return False, ""
        dist = (last - vwap) / vwap * 100
        parts.append(f"{sym} {dist:+.2f}% from VWAP, 15m {tr15}")
        ok = ok and abs(dist) <= CHOP_VWAP_PCT and tr15 == "flat"
    return ok, "; ".join(parts)


def update(r: E.ScanResult, mode: str, now: datetime) -> str:
    """Record this read. Returns a Telegram message if the day's bias changed, else ""."""
    d = now.date()
    st = load(d)
    hist = st.get("history", [])
    is_chop, why_chop = chop_reading(r.raw or {})
    b = r.bias
    reading = {"regime": "CHOP" if is_chop else b.regime,
               "clean": False if is_chop else b.clean,
               "why": why_chop if is_chop else "; ".join(b.notes[:2]),
               "at": now.isoformat()}
    msg = ""
    if mode == "confirm" or not hist:
        if not hist:
            hist.append({**reading, "regime": b.regime, "clean": b.clean,
                         "why": "; ".join(b.notes[:2]), "source": "confirmation read"})
        st["pending"] = None
    else:
        cur = hist[-1]
        pend = st.get("pending")
        if reading["regime"] == cur["regime"]:
            st["pending"] = None
        elif pend and pend.get("regime") == reading["regime"]:
            hist.append({**reading, "source": "re-scan (held 2 reads)"})
            st["pending"] = None
            msg = (f"<b>BIAS CHANGE</b> · {E._dt(now)} HST\n"
                   f"<b>{E._h(cur['regime'])} → {E._h(reading['regime'])}</b>"
                   f"{'' if reading['regime'] == 'CHOP' else (' · clean' if reading['clean'] else ' · not clean')}\n"
                   f"{E._h(reading['why'])}\n"
                   f"<i>was {E._h(cur['regime'])} since {E._short_t(datetime.fromisoformat(cur['at']))}. "
                   f"Display only this week: CHOP doesn't change which cards get called.</i>")
        else:
            st["pending"] = reading
    st["history"] = hist
    st["last_reading"] = reading
    BIAS_DIR.mkdir(parents=True, exist_ok=True)
    _path(d).write_text(json.dumps(st, indent=1))
    return msg
