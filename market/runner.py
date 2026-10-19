#!/usr/bin/env python3
"""
TheEsness Market — runner. GitHub Actions wakes it every ~10 minutes from before the open
until 9:00am HST, and every ~30 minutes after the close.

GitHub's timer is best-effort: runs can be late or skipped. So each run asks "what is due
right now?" instead of assuming it fired on time:

  premarket   open-30 min until the open, once per day. Conditional cards, never graded.
  confirm     open+30 until open+120 min, once per day. Locked cards, graded.
  rescan      after confirm, every ~10 min until ACTIVE_WINDOW_END_HST (9:00am HST). Posts
              NEW setups only (a ticker+direction already posted today is skipped). Locked,
              graded.
  after close grade GO cards · save the day's 1-minute bars · re-run the bias backtest.

Locking: every run is written once to ledger/runs/<date>_<mode>[_HHMM].json and its cards
appended to ledger/locks.csv. Nothing ever rewrites them. Grades go in ledger/grades.csv.

Saved data (for backtesting): data/raw/<date>/<HHMM>_<mode>.json.gz each run (quotes,
technicals, near-the-money option rows), data/bars/<date>.csv.gz after the close.

Public vs private: Max Contracts (reveals account size) goes to Telegram only.

Test runs: `--test premarket|confirm` scans now, whatever the time, saves to ledger/tests/.
Never graded or counted.
"""
from __future__ import annotations

import argparse
import csv
import gzip
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
DATA = REPO / "data"

PREMARKET_DUE = (-30, 0)      # minutes relative to the open (3:00–3:30am HST in summer)
CONFIRM_DUE = (30, 120)
RESCAN_EVERY_MIN = 10
GRADE_AFTER_CLOSE_MIN = 20
GRADE_GIVE_UP_DAYS = 3        # no usable price data after this many days -> UNGRADABLE
GRADED_MODES = {"confirm", "rescan"}

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
    body = [l for l in path.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    if not body or body[0].strip() != f"date={today.isoformat()}":
        return ""
    return "\n".join(body[1:])


def overrides_for(today: date) -> E.Overrides:
    try:
        return E.parse_overrides(dated_input(INPUTS / "tos_override.txt", today))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] tos_override.txt ignored: {e}", file=sys.stderr)
        return E.Overrides()


# --------------------------------------------------------------------------------------------
# run files & due logic
# --------------------------------------------------------------------------------------------

def run_path(d: date, mode: str, now: Optional[datetime] = None) -> Path:
    stamp = f"_{now.strftime('%H%M')}" if mode == "rescan" else ""
    return RUNS / f"{d.isoformat()}_{mode}{stamp}.json"


def day_runs(d: date) -> list:
    """All run snapshots for date d, oldest first."""
    out = [json.loads(p.read_text()) for p in sorted(RUNS.glob(f"{d.isoformat()}_*.json"))]
    return sorted(out, key=lambda s: s["ran_at"])


def active_end(session: E.Session) -> datetime:
    return min(datetime.combine(session.trade_date, E.ACTIVE_WINDOW_END_HST, tzinfo=E.HST),
               session.close_hst)


def due_modes(session: E.Session, now: datetime) -> list:
    d = session.trade_date
    if session.at(PREMARKET_DUE[0]) <= now < session.at(PREMARKET_DUE[1]) \
            and not run_path(d, "premarket").exists():
        return ["premarket"]
    confirm_done = run_path(d, "confirm").exists()
    if session.at(CONFIRM_DUE[0]) <= now < session.at(CONFIRM_DUE[1]) and not confirm_done:
        return ["confirm"]
    if confirm_done and session.at(CONFIRM_DUE[0]) <= now < active_end(session):
        last = max(datetime.fromisoformat(s["ran_at"]) for s in day_runs(d)
                   if s["mode"] in GRADED_MODES)
        if now - last >= timedelta(minutes=RESCAN_EVERY_MIN - 2):   # 2 min slack for timer jitter
            return ["rescan"]
    return []


# --------------------------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------------------------

def card_id(d: date, mode: str, i: E.Idea) -> str:
    return f"{d.isoformat()}_{mode}_{i.ticker}_{i.type}_{i.expiry}_{i.strike:g}"


def public_card(i: E.Idea, d: date, mode: str, locked_at: str) -> dict:
    c = {k: v for k, v in asdict(i).items() if k not in PRIVATE_FIELDS}
    return {"id": card_id(d, mode, i), "trade_date": d.isoformat(), "mode": mode,
            "locked_at": locked_at, **c}


def snapshot(r: E.ScanResult, cards: list, test: bool = False) -> dict:
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
        "cards": [public_card(i, d, r.mode, locked_at) for i in cards],
        "passing": len(r.ideas), "rejected": len(r.skipped),
        "rejections": [] if r.mode == "rescan" else r.skipped[:80],
    }


def posted_today(d: date) -> set:
    return {(c["ticker"], c["type"]) for s in day_runs(d) if s["mode"] in GRADED_MODES
            for c in s["cards"]}


def lock(r: E.ScanResult) -> dict:
    """Write once. Refuses to overwrite. Rescans keep only setups not yet posted today."""
    d = r.session.trade_date
    p = run_path(d, r.mode, r.ran_at)
    if p.exists():
        raise RuntimeError(f"{p.name} already locked")
    if r.mode == "rescan":
        seen = posted_today(d)
        cards = [i for i in r.ideas if (i.ticker, i.type) not in seen][:E.MAX_CARDS]
    else:
        cards = r.top
    snap = snapshot(r, cards)
    RUNS.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, indent=1, default=str))
    _append_csv(LOCKS, LOCK_FIELDS, snap["cards"])
    save_raw(r)
    snap["_ideas"] = cards        # in-memory only (has Max Contracts for Telegram)
    return snap


def _append_csv(path: Path, fields: list, rows: list) -> None:
    if not rows:
        return
    if path.exists():
        with path.open() as f:
            fields = next(csv.reader(f))        # keep the existing column order
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def save_test(r: E.ScanResult) -> dict:
    snap = snapshot(r, r.top, test=True)
    TESTS.mkdir(parents=True, exist_ok=True)
    (TESTS / f"{r.ran_at.strftime('%Y-%m-%dT%H%M')}_{r.mode}.json").write_text(
        json.dumps(snap, indent=1, default=str))
    return snap


# --------------------------------------------------------------------------------------------
# saved data
# --------------------------------------------------------------------------------------------

def save_raw(r: E.ScanResult) -> Path:
    d = r.session.trade_date
    out = DATA / "raw" / d.isoformat() / f"{r.ran_at.strftime('%H%M')}_{r.mode}.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ran_at": r.ran_at.isoformat(), "mode": r.mode,
               "quotes": {k: {"last": q.last, "prev_close": q.prev_close, "source": q.source}
                          for k, q in r.quotes.items()},
               "tickers": r.raw}
    with gzip.open(out, "wt") as f:
        json.dump(payload, f, default=str, separators=(",", ":"))
    return out


def bar_symbols() -> list:
    syms = E.load_watchlist()      # includes shares-only names (RR) — bars are still useful
    return [E.YF_SYMBOL_MAP.get(s, s) for s in syms] + list(E.FUTURES.values()) + [E.VIX_SYMBOL]


def save_day_bars(d: date) -> Optional[Path]:
    """One file per day: 1-minute bars (incl. pre/post) for every scanned symbol."""
    out = DATA / "bars" / f"{d.isoformat()}.csv.gz"
    if out.exists():
        return None
    import pandas as pd
    frames = []
    for sym in dict.fromkeys(bar_symbols()):
        try:
            df = E._yf().Ticker(sym).history(start=d.isoformat(),
                                             end=(d + timedelta(days=1)).isoformat(),
                                             interval="1m", prepost=True, auto_adjust=False)
            if df is None or df.empty:
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].round(4)
            df.index = df.index.tz_convert(E.ET)
            df.insert(0, "symbol", sym)
            frames.append(df)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] day bars {sym}: {type(e).__name__}: {e}", file=sys.stderr)
    if not frames:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(frames).to_csv(out, index_label="time_et", compression="gzip")
    return out


# --------------------------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------------------------
# Graded: GO cards from confirm and rescan runs. Pre-market cards are never graded.
# Entry = locked signal premium. Walk the contract's 5-minute bars from the lock time to that
# day's close. First bar whose high reaches the target -> WIN; first whose low reaches the
# stop -> LOSS; both in one bar -> LOSS (order unknown, so the conservative call). Neither ->
# exit at the day's last bar: WIN above entry, LOSS below, FLAT equal. Same-day exit matches
# the day-trade rules.

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
        df = E._yf().Ticker(contract).history(start=d.isoformat(),
                                              end=(d + timedelta(days=1)).isoformat(),
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
    graded, done, rows = [], load_grades(), []
    for c in load_locks():
        if c["mode"] not in GRADED_MODES or c["status"] != "GO" or c["id"] in done:
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
        rows.append(row)
        graded.append({**c, **row})
    _append_csv(GRADES, GRADE_FIELDS, rows)
    return graded


# --------------------------------------------------------------------------------------------
# after-close jobs
# --------------------------------------------------------------------------------------------

def after_close(now: datetime) -> None:
    graded = grade_due(now)
    if graded:
        lines = [f"GRADED — {E.fmt_hst(now)}"]
        lines += [f"{g['ticker']} {g['type']} {g['strike']} {g['expiry']}: {g['result']} "
                  f"({g['pnl_pct']}%, {g['method']})" for g in graded]
        E.send_telegram("\n".join(lines))
    s = E.session_for(now.date())
    if s and now >= s.close_hst + timedelta(minutes=GRADE_AFTER_CLOSE_MIN):
        save_day_bars(now.date())
        import backtest
        bt = LEDGER / "backtest.json"
        stale = not bt.exists() or json.loads(bt.read_text()).get("built_for") != now.date().isoformat()
        if stale:
            backtest.run(bt, now)


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------

def announce(snap: dict, r: E.ScanResult, now: datetime) -> None:
    ideas = snap["_ideas"]
    if r.mode == "rescan":
        if ideas:
            E.send_telegram(f"NEW SETUP(S) — {E.fmt_hst(now)} · bias {r.bias.regime}\n"
                            + "\n".join(i.card() for i in ideas))
    else:
        E.send_telegram(E.format_report(r))
    for i in ideas:
        if i.a_plus:
            E.send_telegram(f"A+ ALERT — {E.fmt_hst(now)}\n{i.card()}")


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
            r = E.scan("confirm" if mode == "rescan" else mode, session, overrides_for(today), now)
            r.mode = mode
            snap = lock(r)
            print(f"{mode}: locked {len(snap['cards'])} card(s)")
            announce(snap, r, now)
    else:
        print("Market closed today.")

    after_close(now)

    import site_build
    site_build.build(Path(a.site), now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
