"""
Living watchlist (added 2026-11-12, Kalani: "if you see market movers, you can add them to the
list if you see fit, and trim them off the list when you see fit as well").

Your own list (watchlist.txt) is never edited by this file. Names the system adds live in
watchlist_auto.txt, which the scan reads too.

Every read saves Schwab's Market Movers list (most active, S&P 500 / Nasdaq / Dow) to
data/movers/<date>.json. Once a day after the close:

ADD a mover when ALL of these hold:
  * on the movers list on at least ADD_MIN_DAYS of the last LOOKBACK_DAYS trading days
  * a plain stock symbol (1-5 letters), not already on either list
  * 50-day average volume >= 1M shares (your rule)
  * its options pass your liquidity rules on BOTH sides (OI >= 500, a bid, spread <= 10%) at the
    strike the scan would pick, on the expiry the scan would use next session
  * the auto list has fewer than MAX_AUTO names (most mover days first, then biggest move)

DROP an auto-added name when EITHER:
  * it hasn't been on the movers list for DROP_AFTER_DAYS trading days and had no card in that time
  * its options fail the liquidity rules on both sides on 3 checks in a row (one bad quote
    after the close isn't enough)

Your own names are never removed automatically. On Fridays the Telegram note lists any of your
names whose options failed the liquidity rules on most reads over the last two weeks, for you
to decide.
Every add/drop goes to Telegram with the reason.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402

AUTO_PATH = E.AUTO_WATCHLIST_PATH
STATE = R.LEDGER / "autowatch.json"
MOVERS_DIR = E.MOVERS_DIR
LOOKBACK_DAYS = 5
ADD_MIN_DAYS = 2
DROP_AFTER_DAYS = 10
MAX_AUTO = 15
LIQ_FAILS_TO_DROP = 3
THIN_SHARE = 0.8            # Friday flag: your name failed liquidity on >= 80% of reads
LIQ_WORDS = ("OI ", "no bid", "spread ")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text()) if STATE.exists() else {}
    except ValueError:
        return {}


def _trading_days_back(d: date, n: int) -> list:
    out, x = [], d
    while len(out) < n:
        if E.is_trading_day(x):
            out.append(x)
        x -= timedelta(days=1)
    return out


def _movers_on(d: date) -> dict:
    p = MOVERS_DIR / f"{d.isoformat()}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def liquid(sym: str, now: datetime) -> tuple:
    """(ok, why) — would the scan find a liquid contract on both sides next session?"""
    nxt = E.next_trading_day(now.date())
    s = E.session_for(nxt)
    try:
        tech = E.fetch_technicals(sym, s, "rescan")
    except Exception as e:  # noqa: BLE001
        return None, f"no price data ({type(e).__name__})"
    if not tech.last:
        return None, "no price data"
    if tech.avg_volume is None or tech.avg_volume < E.MIN_AVG_VOLUME:
        return False, f"avg volume {tech.avg_volume or 0:,.0f} < {E.MIN_AVG_VOLUME:,}"
    chain = None
    if E.PROVIDER is not None:
        try:
            chain = E.PROVIDER.chain(sym, nxt, nxt + timedelta(days=E.HARD_MAX_DTE))
        except Exception:  # noqa: BLE001
            chain = None
    if not chain:
        return None, "no option chain"
    exp, why = E.pick_expiry(sorted({o.expiry for o in chain}), nxt, E.day_rules(nxt))
    if exp is None:
        return False, why
    bad = []
    for right in ("C", "P"):
        o = E.pick_strike(chain, exp, right, tech.last)
        ok, why = E.liquidity_check(o) if o else (False, "no strikes")
        if not ok:
            bad.append(f"{'calls' if right == 'C' else 'puts'}: {why}")
    return (not bad), "; ".join(bad) or f"avg vol {tech.avg_volume / 1e6:.1f}M, options liquid both sides"


def _cards_since(sym: str, since: date) -> int:
    return sum(1 for c in R.load_locks()
               if c["ticker"] == sym and c["status"] == "GO" and date.fromisoformat(c["trade_date"]) >= since)


def _thin_own_names(today: date) -> list:
    """Your names whose picked contract failed liquidity on >= THIN_SHARE of reads in 2 weeks."""
    days = {d.isoformat() for d in _trading_days_back(today, 10)}
    seen, thin = {}, {}
    for f in sorted((R.LEDGER / "runs").glob("*.json")):
        if f.name[:10] not in days:
            continue
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if run.get("test") or run.get("mode") == "premarket":
            continue
        for r in run.get("rejections", []):
            name, _, why = r.partition(":")
            sym = name.split()[0]
            seen[sym] = seen.get(sym, 0) + 1
            if any(w in why for w in LIQ_WORDS):
                thin[sym] = thin.get(sym, 0) + 1
        for c in run.get("cards", []):
            seen[c["ticker"]] = seen.get(c["ticker"], 0) + 1
    own = set(E.load_watchlist(E.WATCHLIST_PATH, auto=False)) - set(E.CORE_INDEX)
    return sorted(s for s in own if seen.get(s, 0) >= 10 and thin.get(s, 0) / seen[s] >= THIN_SHARE)


def write_auto(auto: dict) -> None:
    lines = ["# Managed by the system (market/autowatch.py). Market movers added automatically;",
             "# dropped when they stop moving or their options thin out. Edit watchlist.txt, not this.", ""]
    for s, v in sorted(auto.items()):
        lines.append(f"{s:6} # added {v['added']}: {v['why']}")
    AUTO_PATH.write_text("\n".join(lines) + "\n")


def maintain(now: datetime, send: bool = True) -> str:
    """Once per trading day after the close. Returns the Telegram text ('' if nothing changed)."""
    st = _load_state()
    today = now.date()
    if st.get("last_run") == today.isoformat():
        return ""
    auto = st.get("auto", {})
    own = set(E.load_watchlist(E.WATCHLIST_PATH, auto=False))
    days = _trading_days_back(today, max(LOOKBACK_DAYS, DROP_AFTER_DAYS))
    by_day = {d: _movers_on(d) for d in days}
    recent = days[:LOOKBACK_DAYS]
    count, pct = {}, {}
    for d in recent:
        for s, v in by_day[d].items():
            count[s] = count.get(s, 0) + 1
            if v.get("pct") is not None:
                pct[s] = max(pct.get(s, 0), abs(v["pct"]))
    added, dropped = [], []

    # --- drops first (frees room)
    for s in list(auto):
        last_seen = max((d for d in days if s in by_day[d]), default=None)
        quiet = last_seen is None or last_seen < days[DROP_AFTER_DAYS - 1]
        since = date.fromisoformat(auto[s]["added"])
        if quiet and since <= days[DROP_AFTER_DAYS - 1] and _cards_since(s, days[-1]) == 0:
            dropped.append((s, f"off the movers list {DROP_AFTER_DAYS} trading days, no cards"))
            del auto[s]
            continue
        ok, why = liquid(s, now)
        if ok is False:
            auto[s]["liq_fails"] = auto[s].get("liq_fails", 0) + 1
            if auto[s]["liq_fails"] >= LIQ_FAILS_TO_DROP:
                dropped.append((s, f"options thin {LIQ_FAILS_TO_DROP} checks in a row ({why})"))
                del auto[s]
        elif ok:
            auto[s]["liq_fails"] = 0

    # --- adds
    cands = sorted((s for s, n in count.items()
                    if n >= ADD_MIN_DAYS and re.fullmatch(r"[A-Z]{1,5}", s)
                    and s not in own and s not in auto and s not in E.INDICATOR_ONLY),
                   key=lambda s: (-count[s], -pct.get(s, 0)))
    for s in cands:
        if len(auto) >= MAX_AUTO:
            break
        ok, why = liquid(s, now)
        if ok:
            reason = f"Schwab most-active {count[s]} of last {LOOKBACK_DAYS} days; {why}"
            auto[s] = {"added": today.isoformat(), "why": reason}
            added.append((s, reason))
        else:
            st.setdefault("passed_over", {})[s] = {"on": today.isoformat(), "why": why}

    write_auto(auto)
    st.update(auto=auto, last_run=today.isoformat())
    log = st.setdefault("log", [])
    log += [{"on": today.isoformat(), "action": "add", "sym": s, "why": w} for s, w in added]
    log += [{"on": today.isoformat(), "action": "drop", "sym": s, "why": w} for s, w in dropped]
    st["log"] = log[-200:]
    thin = _thin_own_names(today) if today.weekday() == 4 else []
    STATE.write_text(json.dumps(st, indent=1))

    if not (added or dropped or thin):
        return ""
    lines = [f"<b>WATCHLIST</b> · {E._dt(now)} HST"]
    lines += [f"➕ <b>{E._h(s)}</b> — {E._h(w)}" for s, w in added]
    lines += [f"➖ <b>{E._h(s)}</b> — {E._h(w)}" for s, w in dropped]
    if thin:
        lines.append("Your names with thin options on most reads (last 2 weeks), your call: "
                     + ", ".join(E._h(s) for s in thin))
    lines.append(f"<i>Auto-added names now: {len(auto)} of {MAX_AUTO} max.</i>")
    msg = "\n".join(lines)
    if send:
        E.send_telegram(msg, html=True)
    return msg


if __name__ == "__main__":
    import schwab as S
    E.PROVIDER = S.provider_if_connected()
    if sys.argv[1:2] == ["check"]:         # read-only: test the liquidity check (no changes saved)
        for sym in sys.argv[2:] or ["NVDA", "AMD", "NIO", "RBLX", "F"]:
            print(sym, liquid(sym, E.now_hst()))
    else:
        print(maintain(E.now_hst(), send="--send" in sys.argv) or "no changes")
