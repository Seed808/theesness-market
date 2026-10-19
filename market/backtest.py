"""
Bias backtest — replays the ES/YM + VIX + SPY/QQQ bias rules over every past trading day
Yahoo still serves 5-minute bars for (the window is whatever comes back; the page shows the
actual date range).

What it tests: the BIAS rules only, on the underlying (SPY and QQQ). It cannot test the option
cards — free data has no past option prices. Card-level backtests become possible from the
option rows saved in data/raw/ each run, once enough days have accumulated.

Per day:
  1. Pre-market read, exactly like the live code: each symbol's last 5-minute bar before the
     open vs its prior regular-session daily close -> compute_bias().
  2. Outcome: entry = SPY (and QQQ) price at open+30 min (amateur hour over). Watch until
     open+150 min. Favored PUTS: WIN if price falls BT_MOVE_PCT before it rises BT_MOVE_PCT;
     LOSS if the reverse; both inside one bar = LOSS; neither = NONE. Calls mirror it.
     No favored side -> not scored.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import engine as E

BT_MOVE_PCT = 0.30
BT_START_MIN, BT_END_MIN = 30, 150
SYMBOLS = {"ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "VIX": E.VIX_SYMBOL, "SPY": "SPY", "QQQ": "QQQ"}


def fetch(sym: str):
    t = E._yf().Ticker(sym)
    daily = t.history(period="6mo", interval="1d", auto_adjust=False)
    m5 = t.history(period="60d", interval="5m", prepost=True, auto_adjust=False)
    if daily is None or daily.empty or m5 is None or m5.empty:
        return None, None
    daily.index = daily.index.tz_convert(E.ET) if daily.index.tz is not None else daily.index
    return daily, m5.tz_convert(E.ET)


def premarket_quote(name: str, daily, m5, d: date, open_et: datetime) -> tuple:
    """(Quote, had_premarket_bar). Mirrors the live fetch_quote at ~30 min before the open."""
    prior = daily[[i.date() < d for i in daily.index]]
    prev = float(prior["Close"].iloc[-1]) if not prior.empty else None
    cut = open_et - timedelta(minutes=30)
    before = m5[m5.index < cut]
    on_day = before[before.index.date == d]
    last = float(before["Close"].iloc[-1]) if not before.empty else None
    return E.Quote(name, last, prev), not on_day.empty


def outcome(m5, d: date, open_et: datetime, side: str) -> tuple:
    rth = m5[(m5.index >= open_et) & (m5.index.date == d)]
    start = open_et + timedelta(minutes=BT_START_MIN)
    end = open_et + timedelta(minutes=BT_END_MIN)
    entry_bars = rth[rth.index < start]
    if entry_bars.empty:
        return "NO DATA", None
    entry = float(entry_bars["Close"].iloc[-1])
    w = rth[(rth.index >= start) & (rth.index < end)]
    if w.empty:
        return "NO DATA", None
    up, dn = entry * (1 + BT_MOVE_PCT / 100), entry * (1 - BT_MOVE_PCT / 100)
    for _, b in w.iterrows():
        hi, lo = b["High"] >= up, b["Low"] <= dn
        if hi and lo:
            return "LOSS", entry
        if side == "P" and lo or side == "C" and hi:
            return "WIN", entry
        if side == "P" and hi or side == "C" and lo:
            return "LOSS", entry
    return "NONE", entry


def run(out: Path, now: datetime) -> dict:
    data = {}
    for name, sym in SYMBOLS.items():
        try:
            data[name] = fetch(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] backtest {sym}: {type(e).__name__}: {e}", file=sys.stderr)
            data[name] = (None, None)
    if any(v[0] is None for v in data.values()):
        missing = [k for k, v in data.items() if v[0] is None]
        result = {"built_for": now.date().isoformat(), "built_at": now.isoformat(),
                  "error": f"missing data for {', '.join(missing)}", "days": []}
        out.write_text(json.dumps(result, indent=1))
        return result

    spy_days = sorted(set(data["SPY"][1].index.date))
    days = []
    for d in spy_days:
        s = E.session_for(d)
        if s is None or (d == now.date() and now < s.close_hst):
            continue
        open_et = s.open_hst.astimezone(E.ET)
        q, pm_bar = {}, {}
        for name, (daily, m5) in data.items():
            q[name], pm_bar[name] = premarket_quote(name, daily, m5, d, open_et)
        b = E.compute_bias(q["ES"], q["NQ"], q["YM"], q["VIX"], q["SPY"], q["QQQ"])
        row = {"date": d.isoformat(), "regime": b.regime, "favored": b.favored, "clean": b.clean,
               "vix_confirms": b.vix_confirms, "index_confirms": b.index_confirms,
               "chg": {k: (None if v.chg_pct is None else round(v.chg_pct, 2)) for k, v in q.items()},
               "premarket_bar": pm_bar}
        for name in ("SPY", "QQQ"):
            row[name] = outcome(data[name][1], d, open_et, b.favored)[0] if b.favored else "—"
        days.append(row)

    def tally(rows, key):
        r = [x[key] for x in rows]
        w, l, n = r.count("WIN"), r.count("LOSS"), r.count("NONE")
        return {"days": len(rows), "win": w, "loss": l, "none": n,
                "win_pct": round(w / (w + l) * 100, 1) if w + l else None}

    summary = []
    for regime in ("BEAR", "ROTATION", "BULL"):
        for clean in (True, False):
            rows = [x for x in days if x["regime"] == regime and x["clean"] == clean]
            if rows:
                summary.append({"regime": regime, "clean": clean,
                                "SPY": tally(rows, "SPY"), "QQQ": tally(rows, "QQQ")})
    unscored = sum(1 for x in days if not x["favored"])
    result = {
        "built_for": now.date().isoformat(), "built_at": now.isoformat(),
        "range": [days[0]["date"], days[-1]["date"]] if days else None,
        "params": {"move_pct": BT_MOVE_PCT, "start_min": BT_START_MIN, "end_min": BT_END_MIN,
                   "flat_band_pct": E.FLAT_BAND_PCT, "vix_confirm_pct": E.VIX_CONFIRM_PCT},
        "unscored_days": unscored,
        "vix_premarket_bar_days": sum(1 for x in days if x["premarket_bar"].get("VIX")),
        "summary": summary, "days": list(reversed(days)),
    }
    out.write_text(json.dumps(result, indent=1))
    return result
