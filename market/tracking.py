"""
Two-target tracking (added 2026-10-27, Kalani's spec).

* Target 1 (+20%) is scored on its own: did the contract reach T1 before the -20% stop?
  Written once per card to ledger/grades_t1.csv (write-once, like grades.csv).
  The card itself keeps running to Target 2 (+50%) / stop / expiry-day close; that official
  result stays in ledger/grades.csv, unchanged.
* "Went on to": the contract's highest minute price while the trade was alive (from lock until
  the stop was hit, or until the expiry-day close), as % vs entry. That's the best possible exit,
  not one you could reliably get. Kept in ledger/peaks.json and frozen after the expiry-day close.
* Break-even side number (tracking only, not a rule): if the stop had moved to the entry price
  once T1 was hit, what would the T2 trade have done? Kept in ledger/peaks.json.

Earlier cards whose contracts Schwab no longer returns prices for (10/20-10/23):
  T2 WIN -> T1 WIN (it had to pass +20% on the way to +50%).
  Otherwise the option quotes saved in data/raw/ at each run (~every 35 min) are checked: a tight
  quote at or above T1, earlier than any saved quote at or below the stop -> T1 WIN; if none ->
  T1 UNKNOWN (it may have touched +20% between runs). UNKNOWN is never counted as a loss.
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402

GRADES_T1 = R.LEDGER / "grades_t1.csv"
GRADE_T1_FIELDS = ["id", "graded_at", "result", "exit_premium", "pnl_pct", "hit_at", "method"]
PEAKS = R.LEDGER / "peaks.json"
HIT_NOTICE_MIN = 45        # only hits this recent get a Telegram "T1 HIT" note


def load_t1() -> dict:
    if not GRADES_T1.exists():
        return {}
    import csv
    with GRADES_T1.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def load_peaks() -> dict:
    try:
        return json.loads(PEAKS.read_text()) if PEAKS.exists() else {}
    except ValueError:
        return {}


def card_t1(c: dict) -> float:
    return float(c.get("target1") or 0) or E.target1_price(float(c["premium"]))


def walk(w, target: float, stop: float):
    """First level touched, same rules as the live grader: a bar that OPENS past a level exits at
    that open (gap); target and stop in the same bar = stop. -> (result, price, time, how) or
    (None, last close, last time, "") if neither was touched."""
    for t, b in w.iterrows():
        o = float(b["Open"])
        if o <= stop:
            return "LOSS", o, t, "STOP (gapped through)"
        if o >= target:
            return "WIN", o, t, "TARGET (gapped through)"
        hit_t, hit_s = b["High"] >= target, b["Low"] <= stop
        if hit_s:
            return "LOSS", stop, t, ("STOP (target+stop same bar)" if hit_t else "STOP")
        if hit_t:
            return "WIN", target, t, "TARGET"
    return None, float(w["Close"].iloc[-1]), w.index[-1], ""


def _pct(px: float, entry: float) -> float:
    return round((px - entry) / entry * 100, 1)


# ------------------------------------------------------------------ backfill from saved quotes

def snapshot_t1(c: dict) -> Optional[tuple]:
    """(result, price, time, how) from data/raw quotes, or None if they don't settle it."""
    d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
    entry, t1, stop = float(c["premium"]), card_t1(c), float(c["stop"])
    locked = datetime.fromisoformat(c["locked_at"])
    right = "C" if c["type"] == "CALL" else "P"
    k = float(c["strike"])
    obs = []
    day = d
    while day <= exp:
        folder = R.DATA / "raw" / day.isoformat()
        for f in sorted(folder.glob("*.json.gz")) if folder.exists() else []:
            try:
                snap = json.load(gzip.open(f, "rt"))
                ran = datetime.fromisoformat(snap["ran_at"])
                tk = snap.get("tickers", {}).get(c["ticker"]) or {}
            except Exception:  # noqa: BLE001
                continue
            if ran <= locked or str(tk.get("expiry")) != c["expiry"]:
                continue
            for row in tk.get("chain") or []:
                try:
                    r_, strike, bid, ask = row[0], float(row[1]), row[2], row[3]
                except (IndexError, TypeError, ValueError):
                    continue
                if r_ != right or abs(strike - k) > 1e-6 or not bid or not ask:
                    continue
                mid = (float(bid) + float(ask)) / 2
                if float(ask) - float(bid) <= max(0.05, 0.15 * mid):     # tight quotes only
                    obs.append((ran, mid))
        day += timedelta(days=1)
    # The stop time must be known, or a later saved quote could be counted after the trade was
    # already stopped out (NFLX/WMT 10/21 were). Known stop times come from the stop study, which
    # replayed those cards on 5-minute bars while the prices were still available.
    stop_at = known_stop_time(c)
    if stop_at is None:
        return None
    for ran, mid in sorted(obs):
        if ran >= stop_at or mid <= stop:
            return None
        if mid >= t1:
            return "WIN", t1, ran, "T1 (saved run quote, before the stop)"
    return None


def known_stop_time(c: dict) -> Optional[datetime]:
    p = R.LEDGER / "stop_study.json"
    try:
        cards = {x["id"]: x for x in json.loads(p.read_text())["cards"]} if p.exists() else {}
    except (ValueError, KeyError):
        return None
    x = cards.get(c["id"])
    if not x:
        return None
    entry, stop = float(c["premium"]), float(c["stop"])
    key = f"stop{int(round((entry - stop) / entry * 100 / 5) * 5)}"        # nearest 5% bucket
    s = x.get(key) or {}
    if s.get("result") != "LOSS" or not s.get("at"):
        return None
    return datetime.fromisoformat(s["at"])


# ------------------------------------------------------------------ main

def track(now: datetime, fetch=None) -> list:
    """Update T1 grades + peaks for every GO card. Returns cards whose T1 was newly hit (for the
    Telegram 'T1 HIT' note)."""
    fetch = fetch or R.fetch_contract_bars
    t1_done, peaks, t2_grades = load_t1(), load_peaks(), R.load_grades()
    new_rows, hits = [], []
    for c in R.load_locks():
        if c["mode"] not in R.GRADED_MODES or c["status"] != "GO":
            continue
        cid = c["id"]
        locked = datetime.fromisoformat(c["locked_at"])
        if now < locked:
            continue
        if cid in t1_done and peaks.get(cid, {}).get("final"):
            continue
        d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
        s_exp = E.session_for(exp) or E.session_for(d)
        finished = now >= s_exp.close_hst + timedelta(minutes=R.GRADE_AFTER_CLOSE_MIN)
        entry, t1, t2, stop = float(c["premium"]), card_t1(c), float(c["target"]), float(c["stop"])
        try:
            bars = fetch(c["contract"], d, min(exp, now.date())) if c.get("contract") else None
        except Exception as e:  # noqa: BLE001
            print(f"[warn] track bars {cid}: {type(e).__name__}: {e}", file=sys.stderr)
            bars = None
        end = s_exp.close_hst if finished else now
        w = None
        if bars is not None:
            w = bars[(bars.index >= locked) & (bars.index <= end)]
            if w.empty:
                w = None

        if w is None:
            # No contract prices. Expired cards from before Schwab history: backfill T1 once.
            g2 = t2_grades.get(cid)
            if cid not in t1_done and g2 and finished and (now.date() - exp).days >= 1:
                if g2["result"] == "WIN" and str(g2.get("method", "")).startswith("TARGET"):
                    row = ("WIN", t1, "", "T2 reached, so T1 passed first")
                elif g2["result"] == "UNGRADABLE":
                    row = ("UNGRADABLE", None, "", "no contract price data")
                else:
                    s = snapshot_t1(c)
                    row = (s[0], s[1], s[2].isoformat(), s[3]) if s else \
                        ("UNKNOWN", None, "", "no minute prices; saved run quotes don't show it")
                res, px, at, how = row
                new_rows.append({"id": cid, "graded_at": now.isoformat(), "result": res,
                                 "exit_premium": "" if px is None else round(px, 2),
                                 "pnl_pct": "" if px is None else _pct(px, entry),
                                 "hit_at": at, "method": how})
                peaks[cid] = {"final": True, "no_prices": True, "as_of": now.isoformat()}
            continue

        src = bars.attrs.get("source", "")
        p = {"source": src, "as_of": now.isoformat()}
        # --- Target 1 (scored on its own, same stop)
        r1, px1, at1, how1 = walk(w, t1, stop)
        if r1 == "WIN":
            p["t1_at"] = at1.isoformat()
        if cid not in t1_done:
            if r1 is not None:
                new_rows.append({"id": cid, "graded_at": now.isoformat(), "result": r1,
                                 "exit_premium": round(px1, 2), "pnl_pct": _pct(px1, entry),
                                 "hit_at": at1.isoformat(), "method": f"{how1} · {src}"})
                if r1 == "WIN" and now - at1 <= timedelta(minutes=HIT_NOTICE_MIN):
                    hits.append({**c, "t1_at": at1, "t1_price": px1})   # fresh hits only, not backfills
            elif finished:
                res = "WIN" if px1 > entry else "LOSS" if px1 < entry else "FLAT"
                new_rows.append({"id": cid, "graded_at": now.isoformat(), "result": res,
                                 "exit_premium": round(px1, 2), "pnl_pct": _pct(px1, entry),
                                 "hit_at": "", "method": f"TIME ({exp.strftime('%a')} close) · {src}"})
        # --- Target 2 path: when did it end, and the peak while the trade was alive
        r2, px2, at2, _ = walk(w, t2, stop)
        if r2 == "WIN":
            p["t2_at"] = at2.isoformat()
        alive = w[w.index <= at2] if r2 == "LOSS" else w          # after a stop the trade is over
        if r2 == "LOSS":
            p["stop_at"] = at2.isoformat()
        hi = float(alive["High"].max())
        p["peak_pct"] = _pct(hi, entry)
        p["peak_at"] = alive["High"].idxmax().isoformat()
        # --- Break-even side number: stop moved to entry once T1 was hit
        if r1 == "WIN":
            after = w[w.index > at1]
            if after.empty:
                be = None
            else:
                rb, pxb, atb, howb = walk(after, t2, entry)
                if rb is None:
                    rb = (("WIN" if pxb > entry else "LOSS" if pxb < entry else "FLAT")
                          if finished else "OPEN")
                    howb = "TIME (close)" if finished else "still open"
                elif rb == "LOSS":
                    howb = "BREAK-EVEN STOP" if pxb == entry else "BREAK-EVEN STOP (gapped through)"
                    rb = "FLAT" if pxb == entry else "LOSS"
                be = {"result": rb, "pnl_pct": _pct(pxb, entry), "how": howb}
            if be:
                p["be"] = be
        p["final"] = finished
        peaks[cid] = p
    R._append_csv(GRADES_T1, GRADE_T1_FIELDS, new_rows)
    PEAKS.write_text(json.dumps(peaks, indent=1))
    return hits


def t1_hit_message(hits: list, now: datetime) -> str:
    lines = [f"<b>T1 HIT</b> · {E._short_t(now)} HST · +{int(E.TARGET1_PCT * 100)}% reached, card still running to T2"]
    for h in hits:
        lines.append(f"<b>{E._h(h['ticker'])} {E._h(h['type'])} {float(h['strike']):g}</b> · entry "
                     f"${float(h['premium']):.2f} → T1 ${h['t1_price']:.2f} at {E._short_t(h['t1_at'])} · "
                     f"T2 ${float(h['target']):.2f} / stop ${float(h['stop']):.2f}")
    return "\n".join(lines)
