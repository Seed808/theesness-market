#!/usr/bin/env python3
"""
TheEsness Market — runner (called by GitHub Actions every ~10 minutes around the open,
and every ~30 minutes after the close).

GitHub's timer is best-effort: runs can be late or skipped. So each run asks "what is due
right now?" instead of assuming it fired on time:

  premarket   due from open-75 min until the open, if not already done today
  confirm     due from open+30 min until open+120 min, if not already done today
  grade       due once the session has closed (+20 min), for any ungraded GO card

Locking: a run's cards are written once to ledger/runs/<date>_<mode>.json and appended to
ledger/locks.csv. Nothing ever rewrites them. Grades go in a separate file, ledger/grades.csv.

Public vs private: the repository and site are public. Max Contracts (which reveals account
size) is sent to Telegram only and never written to the ledger or the site.

Test runs: `--test premarket|confirm` runs a scan right now, whatever the time, and saves it
to ledger/tests/ (shown on test.html). Test runs are never graded or counted.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "ledger"
RUNS = LEDGER / "runs"
TESTS = LEDGER / "tests"
LOCKS = LEDGER / "locks.csv"
GRADES = LEDGER / "grades.csv"
INPUTS = REPO / "inputs"

PREMARKET_DUE = (-75, 0)      # minutes relative to the open
CONFIRM_DUE = (30, 120)
GRADE_AFTER_CLOSE_MIN = 20
GRADE_GIVE_UP_DAYS = 3        # no usable price data after this many days -> UNGRADABLE

PRIVATE_FIELDS = {"max_contracts"}
LOCK_FIELDS = ["id", "trade_date", "mode", "locked_at"] + [
    f for f in E.Idea.__dataclass_fields__ if f not in PRIVATE_FIELDS]
GRADE_FIELDS = ["id", "graded_at", "result", "exit_premium", "pnl_pct", "method"]


# --------------------------------------------------------------------------------------------
# inputs (ToS paste / headlines) — only used when stamped with today's date
# --------------------------------------------------------------------------------------------

def dated_input(path: Path, today: date) -> str:
    """Returns the file's text only if its first non-comment line is `date=<today>`."""
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    body = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    if not body or body[0].strip() != f"date={today.isoformat()}":
        return ""
    return "\n".join(body[1:])


# --------------------------------------------------------------------------------------------
# due logic
# --------------------------------------------------------------------------------------------

def run_path(d: date, mode: str) -> Path:
    return RUNS / f"{d.isoformat()}_{mode}.json"


def due_modes(session: E.Session, now: datetime) -> list:
    due = []
    for mode, (lo, hi) in (("premarket", PREMARKET_DUE), ("confirm", CONFIRM_DUE)):
        if session.at(lo) <= now < session.at(hi) and not run_path(session.trade_date, mode).exists():
            due.append(mode)
    return due


# --------------------------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------------------------

def card_id(d: date, mode: str, i: E.Idea) -> str:
    return f"{d.isoformat()}_{mode}_{i.ticker}_{i.type}_{i.expiry}_{i.strike:g}"


def public_card(i: E.Idea, d: date, mode: str, locked_at: str) -> dict:
    c = {k: v for k, v in asdict(i).items() if k not in PRIVATE_FIELDS}
    return {"id": card_id(d, mode, i), "trade_date": d.isoformat(), "mode": mode,
            "locked_at": locked_at, **c}


def snapshot(r: E.ScanResult, test: bool = False) -> dict:
    d = r.session.trade_date
    locked_at = r.ran_at.isoformat()
    return {
        "trade_date": d.isoformat(), "mode": r.mode, "ran_at": locked_at, "test": test,
        "open_hst": r.session.open_hst.isoformat(), "close_hst": r.session.close_hst.isoformat(),
        "early_close": r.session.early_close,
        "bias": {"regime": r.bias.regime, "favored": r.bias.favored, "clean": r.bias.clean,
                 "notes": r.bias.notes},
        "quotes": {k: {"last": q.last, "prev_close": q.prev_close, "chg_pct": q.chg_pct,
                       "source": q.source} for k, q in r.quotes.items()},
        "flags": r.flags, "headlines": r.headlines,
        "cards": [public_card(i, d, r.mode, locked_at) for i in r.top],
        "passing": len(r.ideas), "rejected": len(r.skipped), "rejections": r.skipped[:80],
    }


def lock(r: E.ScanResult) -> dict:
    """Write once. Refuses to overwrite an existing run file."""
    p = run_path(r.session.trade_date, r.mode)
    if p.exists():
        raise RuntimeError(f"{p.name} already locked")
    snap = snapshot(r)
    RUNS.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, indent=1, default=str))
    new = not LOCKS.exists()
    with LOCKS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOCK_FIELDS)
        if new:
            w.writeheader()
        for c in snap["cards"]:
            w.writerow(c)
    return snap


def save_test(r: E.ScanResult) -> dict:
    snap = snapshot(r, test=True)
    TESTS.mkdir(parents=True, exist_ok=True)
    (TESTS / f"{r.ran_at.strftime('%Y-%m-%dT%H%M')}_{r.mode}.json").write_text(
        json.dumps(snap, indent=1, default=str))
    return snap


# --------------------------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------------------------
# Graded: cards from the CONFIRM run whose status was GO. (Pre-market cards are conditional
# watch ideas and are never graded.)
# Method: entry = locked signal premium. Walk the contract's 5-minute bars from the lock time
# to that day's close. First bar whose high reaches the target -> WIN; first bar whose low
# reaches the stop -> LOSS; both in the same bar -> LOSS (conservative, order unknown).
# Neither -> exit at the day's last bar ("TIME" method): WIN if above entry, LOSS if below,
# FLAT if equal. Same-day exit matches the day-trade rules (no overnight holds).

def load_grades() -> dict:
    if not GRADES.exists():
        return {}
    with GRADES.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def load_locks() -> list:
    if not LOCKS.exists():
        return []
    with LOCKS.open() as f:
        return list(csv.DictReader(f))


def fetch_contract_bars(contract: str, d: date):
    """5-minute bars for one option contract on trade date d, index in HST. None on failure."""
    try:
        t = E._yf().Ticker(contract)
        df = t.history(start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
                       interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df.index = df.index.tz_convert(E.HST)
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[warn] bars {contract}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def grade_from_bars(bars, entry: float, target: float, stop: float,
                    start: datetime, end: datetime) -> Optional[tuple]:
    """(result, exit_premium, method) or None if no bars in the window. Pure function."""
    w = bars[(bars.index >= start) & (bars.index <= end)]
    if w.empty:
        return None
    for _, b in w.iterrows():
        hit_t, hit_s = b["High"] >= target, b["Low"] <= stop
        if hit_t and hit_s:
            return "LOSS", stop, "STOP (target+stop same bar)"
        if hit_s:
            return "LOSS", stop, "STOP"
        if hit_t:
            return "WIN", target, "TARGET"
    last = float(w["Close"].iloc[-1])
    res = "WIN" if last > entry else "LOSS" if last < entry else "FLAT"
    return res, last, "TIME (close)"


def grade_due(now: datetime, fetch=None) -> list:
    fetch = fetch or fetch_contract_bars
    graded, done = [], load_grades()
    for c in load_locks():
        if c["mode"] != "confirm" or c["status"] != "GO" or c["id"] in done:
            continue
        d = date.fromisoformat(c["trade_date"])
        s = E.session_for(d)
        if s is None or now < s.close_hst + timedelta(minutes=GRADE_AFTER_CLOSE_MIN):
            continue
        entry = float(c["premium"])
        g = None
        bars = fetch(c["contract"], d) if c.get("contract") else None
        if bars is not None:
            g = grade_from_bars(bars, entry, float(c["target"]), float(c["stop"]),
                                datetime.fromisoformat(c["locked_at"]), s.close_hst)
        if g is None:
            if (now.date() - d).days >= GRADE_GIVE_UP_DAYS:
                g = ("UNGRADABLE", None, "no contract price data")
            else:
                continue   # try again next run
        res, exit_p, method = g
        row = {"id": c["id"], "graded_at": now.isoformat(), "result": res,
               "exit_premium": "" if exit_p is None else round(exit_p, 2),
               "pnl_pct": "" if exit_p is None else round((exit_p - entry) / entry * 100, 1),
               "method": method}
        graded.append({**c, **row})
        new = not GRADES.exists()
        with GRADES.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=GRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    return graded


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------

def overrides_for(today: date) -> E.Overrides:
    try:
        return E.parse_overrides(dated_input(INPUTS / "tos_override.txt", today))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] tos_override.txt ignored: {e}", file=sys.stderr)
        return E.Overrides()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", choices=["premarket", "confirm"],
                    help="scan now regardless of time; saved as a test, never graded")
    ap.add_argument("--site", default=str(REPO / "_site"), help="output folder for the site")
    a = ap.parse_args()

    now = E.now_hst()
    print(f"Run at {E.fmt_hst(now)}")
    today = now.date()
    session = E.session_for(today)
    hl = dated_input(INPUTS / "headlines.txt", today)
    E.load_headlines = lambda: [h.strip() for h in hl.splitlines() if h.strip()][:8]

    if a.test:
        s = session or E.session_for(E.next_trading_day(today))
        r = E.scan(a.test, s, overrides_for(today), now)
        save_test(r)
        print(E.format_report(r))
        E.send_telegram("[TEST RUN — not a live signal]\n" + E.format_report(r))
    elif session is not None:
        for mode in due_modes(session, now):
            print(f"{mode} is due")
            r = E.scan(mode, session, overrides_for(today), now)
            lock(r)
            E.send_telegram(E.format_report(r))
            for i in r.top:
                if i.a_plus:
                    E.send_telegram(f"A+ ALERT — {E.fmt_hst(now)}\n{i.card()}")
    else:
        print("Market closed today.")

    graded = grade_due(now)
    if graded:
        lines = [f"GRADED — {E.fmt_hst(now)}"]
        lines += [f"{g['ticker']} {g['type']} {g['strike']} {g['expiry']}: {g['result']} "
                  f"({g['pnl_pct']}%, {g['method']})" for g in graded]
        E.send_telegram("\n".join(lines))

    import site_build
    site_build.build(Path(a.site), now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
