"""
Watch tracking (added 2026-11-02, Kalani's spec). Kept apart from the official record.

1. Pre-market cards: at the confirmation read each one gets a status right away —
   CONFIRMED (became a GO card), NOT CONFIRMED (with the rule reasons and the stock's move vs the
   prior close), PASSED (passed every rule but wasn't in the top 5) or NOT SCANNED — and is
   re-priced with the live option price at that moment. From that price it's tracked to
   T1 (+20%) / T2 (+50%) / stop (-20%) like a real card -> the "Pre-market watch" record.
2. Gap-chase blocks: setups rejected by the gap-chase rule and nothing else (first one per
   ticker + direction per day, during the confirmation read and re-scans) are tracked the same
   way from the price at that run -> the "Gap-chase blocked" record. This answers with real
   prices whether the rule saves or costs money. The gap-chase rule itself is unchanged.

Files (ledger/):
  watch.csv          one row per watch card, write-once
  watch_grades.csv   T1 and T2 results per watch card, write-once (target = T1 / T2)
  watch_peaks.json   live view: hit times, "went on to", frozen after the expiry-day close
Everything is rebuilt from the locked run files + saved raw data, so it can be backfilled.
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402
from tracking import walk, _pct  # noqa: E402

WATCH = R.LEDGER / "watch.csv"
WATCH_GRADES = R.LEDGER / "watch_grades.csv"
WATCH_PEAKS = R.LEDGER / "watch_peaks.json"
WATCH_FIELDS = ["id", "trade_date", "kind", "ticker", "type", "expiry", "strike", "contract",
                "called_at", "priced_at", "premium", "price_source", "target1", "target", "stop",
                "underlying", "underlying_asof", "stock_move_pct", "status", "reason"]
WG_FIELDS = ["id", "target", "graded_at", "result", "exit_premium", "pnl_pct", "hit_at", "method"]


def _read_csv(p: Path) -> list:
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def load_watch() -> list:
    return _read_csv(WATCH)


def load_watch_grades() -> dict:
    """{watch id: {"T1": row, "T2": row}}"""
    out = {}
    for r in _read_csv(WATCH_GRADES):
        out.setdefault(r["id"], {})[r["target"]] = r
    return out


def load_watch_peaks() -> dict:
    try:
        return json.loads(WATCH_PEAKS.read_text()) if WATCH_PEAKS.exists() else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ building watch rows

def _raw(d: date, ran_at: str, mode: str) -> Optional[dict]:
    t = datetime.fromisoformat(ran_at)
    p = R.DATA / "raw" / d.isoformat() / f"{t.strftime('%H%M')}_{mode}.json.gz"
    if not p.exists():
        return None
    try:
        return json.load(gzip.open(p, "rt"))
    except Exception:  # noqa: BLE001
        return None


def _chain(raw_t: dict) -> list:
    exp = date.fromisoformat(raw_t["expiry"])
    out = []
    for r in raw_t.get("chain") or []:
        try:
            out.append(E.OptionQuote(expiry=exp, right=r[0], strike=float(r[1]), bid=r[2], ask=r[3],
                                     oi=r[4], iv=r[5], source=r[6]))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _move(raw_t: Optional[dict]) -> Optional[float]:
    tech = (raw_t or {}).get("tech") or {}
    last, prev = tech.get("last"), tech.get("prev_close")
    if not last or not prev:
        return None
    return round((last - prev) / prev * 100, 2)


def _price_contract(c: dict, raw_t: Optional[dict], at: datetime) -> tuple:
    """(premium, source) for contract c at time `at`: the run's own chain quote (same source the
    official cards use), else Schwab 1-minute contract bars at that minute, else (None, '')."""
    right = "C" if c["type"] == "CALL" else "P"
    if raw_t and raw_t.get("expiry") == c["expiry"]:
        for o in _chain(raw_t):
            if o.right == right and abs(o.strike - float(c["strike"])) < 1e-6 and o.mid:
                return round(o.mid, 2), f"chain {o.source}"
    if c.get("contract"):
        try:
            d = date.fromisoformat(c["trade_date"])
            bars = R.fetch_contract_bars(c["contract"], d, d)
            if bars is not None and not bars.empty:
                b = bars[bars.index <= at]
                if not b.empty and at - b.index[-1] <= timedelta(minutes=10):
                    return round(float(b["Close"].iloc[-1]), 2), bars.attrs.get("source", "bars")
                a = bars[(bars.index > at) & (bars.index <= at + timedelta(minutes=10))]
                if not a.empty:                  # no trade just before: first trade right after
                    return round(float(a["Open"].iloc[0]), 2), f'{bars.attrs.get("source", "bars")} (next trade {E._dt(a.index[0])})'
        except Exception as e:  # noqa: BLE001
            print(f"[warn] watch price {c['contract']}: {type(e).__name__}: {e}", file=sys.stderr)
    # Live quote, only if we're pricing "now" (the read that just ran), never for a past time.
    if c.get("contract") and E.PROVIDER is not None and abs(E.now_hst() - at) <= timedelta(minutes=10):
        try:
            import schwab as S
            q = S.option_quote(c["contract"])
            if q and q.get("last"):
                return round(q["last"], 2), q.get("source", "schwab quote")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] watch quote {c['contract']}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"[warn] watch: no price for {c.get('contract')} at {at.isoformat()}", file=sys.stderr)
    return None, ""


def fill_missing_prices() -> int:
    """Pre-market watch rows that got no price at the confirmation read are re-tried from the
    contract's minute bars at that same time (a fill-in of a missing value, never a change)."""
    rows = load_watch()
    changed = 0
    for r in rows:
        if r.get("premium") not in ("", None) or not r.get("contract"):
            continue
        at = datetime.fromisoformat(r["priced_at"])
        if E.now_hst() - at > timedelta(days=3):
            continue
        prem, src = _price_contract(r, None, at)
        if prem is not None:
            r.update(_row(r, prem, f"{src} · filled in later"))
            changed += 1
    if changed:
        with WATCH.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=WATCH_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return changed


def _row(base: dict, premium: Optional[float], src: str) -> dict:
    p = premium
    return {**base, "premium": "" if p is None else p, "price_source": src,
            "target1": "" if p is None else E.target1_price(p),
            "target": "" if p is None else round(p * (1 + E.TARGET_PCT), 2),
            "stop": "" if p is None else E.stop_price(p)}


def record_watch(d: date) -> list:
    """Add watch rows for day d from its locked run files (idempotent). Returns the rows for the
    pre-market cards (new or existing), for the Telegram status block."""
    runs = {}
    for p in sorted(R.RUNS.glob(f"{d.isoformat()}_*.json")):
        s = json.loads(p.read_text())
        runs.setdefault(s["mode"], []).append(s)
    have = {r["id"]: r for r in load_watch()}
    new, pre_rows = [], []
    conf = (runs.get("confirm") or [None])[0]
    official = {(c["ticker"], c["type"]) for m in ("confirm", "rescan") for s in runs.get(m, [])
                for c in s["cards"]}

    # 1) pre-market cards -> status + price at the confirmation read
    pre = (runs.get("premarket") or [None])[0]
    if pre and conf:
        raw = _raw(d, conf["ran_at"], "confirm") or {}
        at = datetime.fromisoformat(conf["ran_at"])
        conf_cards = {(c["ticker"], c["type"]): c for c in conf["cards"]}
        rej = {}
        for x in conf["rejections"]:
            name, _, why = x.partition(":")
            rej[name.strip()] = why.strip()
        for c in pre["cards"]:
            wid = f"{d.isoformat()}_watch-premarket_{c['ticker']}_{c['type']}_{c['expiry']}_{float(c['strike']):g}"
            if wid in have:
                pre_rows.append(have[wid])
                continue
            key = f"{c['ticker']} {'C' if c['type'] == 'CALL' else 'P'}"
            raw_t = (raw.get("tickers") or {}).get(c["ticker"])
            if (c["ticker"], c["type"]) in conf_cards:
                cc = conf_cards[(c["ticker"], c["type"])]
                status = "CONFIRMED"
                reason = (f"became a {cc['status']} card"
                          + ("" if float(cc["strike"]) == float(c["strike"]) else f" at strike {float(cc['strike']):g}"))
            elif key in rej:
                status, reason = "NOT CONFIRMED", rej[key]
            elif raw_t is not None and conf.get("passing", 0) > len(conf["cards"]):
                status, reason = "PASSED", f"passed every rule but wasn't in the top {E.MAX_CARDS}"
            elif raw_t is not None:
                status, reason = "NOT CONFIRMED", "rejected — reason not recorded"
            else:
                status, reason = "NOT SCANNED", "no data for this ticker at the confirmation read"
            prem, src = _price_contract({**c, "trade_date": d.isoformat()}, raw_t, at)
            tech = (raw_t or {}).get("tech") or {}
            base = {"id": wid, "trade_date": d.isoformat(), "kind": "premarket", "ticker": c["ticker"],
                    "type": c["type"], "expiry": c["expiry"], "strike": c["strike"],
                    "contract": c.get("contract", ""), "called_at": c["locked_at"], "priced_at": conf["ran_at"],
                    "underlying": tech.get("last") or "", "underlying_asof": E._short_t(at) + " HST",
                    "stock_move_pct": "" if _move(raw_t) is None else _move(raw_t),
                    "status": status, "reason": reason}
            row = _row(base, prem, src)
            new.append(row)
            pre_rows.append({**row, "_new": True})     # _new is not written to the CSV

    # 1b) pre-market cards tracked from THE OPEN too (Kalani 2026-11-04, after COIN 143C ran
    #     +57% between the 4:32 open price and the 5:10 trade). Price = the open update's price
    #     when it's a trade from today; otherwise the contract's first minute bar after the bell.
    #     Entering here breaks the amateur-hour rule — tracked only to measure what that rule costs.
    if pre:
        sess = E.session_for(d)
        op = R.OPEN_DIR / f"{d.isoformat()}.json"
        opened = json.loads(op.read_text()) if op.exists() else {}
        at_default = sess.at(R.OPEN_UPDATE_DUE[0] + 1) if sess else None
        for c in pre["cards"]:
            wid = f"{d.isoformat()}_watch-preopen_{c['ticker']}_{c['type']}_{c['expiry']}_{float(c['strike']):g}"
            if wid in have or at_default is None:
                continue
            o = (opened.get("cards") or {}).get(c["id"])
            prem, src, at = None, "", datetime.fromisoformat(opened["ran_at"]) if opened else at_default
            if o and datetime.fromisoformat(o["time"]).astimezone(E.HST).date() == d:
                prem, src, at = o["price"], f'open update · {o.get("source", "")}', datetime.fromisoformat(o["time"])
            elif E.now_hst() >= at:
                prem, src = _price_contract({**c, "trade_date": d.isoformat()}, None, at)
            else:
                continue                              # bell hasn't rung yet
            if prem is None:
                continue                              # retried next run (no row written)
            at = at.astimezone(E.HST)
            base = {"id": wid, "trade_date": d.isoformat(), "kind": "preopen", "ticker": c["ticker"],
                    "type": c["type"], "expiry": c["expiry"], "strike": c["strike"],
                    "contract": c.get("contract", ""), "called_at": c["locked_at"], "priced_at": at.isoformat(),
                    "underlying": (o or {}).get("underlying") or "", "underlying_asof": E._short_t(at) + " HST",
                    "stock_move_pct": "", "status": "AT THE OPEN",
                    "reason": "tracked from the open price — amateur hour, not a rule-approved entry"}
            new.append(_row(base, prem, src))

    # 2) gap-chase-only blocks (confirmation read + re-scans), first per ticker/direction/day
    seen = {(r["ticker"], r["type"]) for r in have.values() if r["kind"] == "gapchase" and r["trade_date"] == d.isoformat()}
    for mode in ("confirm", "rescan"):
        for s in runs.get(mode, []):
            raw = None
            for x in s["rejections"]:
                name, _, why = x.partition(":")
                if not why.strip().startswith("gap chase") or "," in why:
                    continue                      # only setups blocked by gap chase and nothing else
                sym, right = name.strip().split()
                typ = "CALL" if right == "C" else "PUT"
                if (sym, typ) in seen or (sym, typ) in official:
                    continue
                raw = raw or _raw(d, s["ran_at"], mode) or {}
                raw_t = (raw.get("tickers") or {}).get(sym)
                if not raw_t:
                    continue
                spot = (raw_t.get("tech") or {}).get("last")
                opt = E.pick_strike(_chain(raw_t), date.fromisoformat(raw_t["expiry"]), right, spot) if spot else None
                if opt is None or not opt.mid:
                    continue
                seen.add((sym, typ))
                at = datetime.fromisoformat(s["ran_at"])
                wid = f"{d.isoformat()}_watch-gapchase_{sym}_{typ}_{opt.expiry.isoformat()}_{opt.strike:g}"
                if wid in have:
                    continue
                base = {"id": wid, "trade_date": d.isoformat(), "kind": "gapchase", "ticker": sym, "type": typ,
                        "expiry": opt.expiry.isoformat(), "strike": opt.strike,
                        "contract": E.occ_symbol("SPXW" if sym == "SPX" else sym, opt),   # same form as official cards
                        "called_at": s["ran_at"], "priced_at": s["ran_at"], "underlying": spot,
                        "underlying_asof": E._short_t(at) + " HST",
                        "stock_move_pct": "" if _move(raw_t) is None else _move(raw_t),
                        "status": "BLOCKED", "reason": "gap chase"}
                new.append(_row(base, round(opt.mid, 2), f"chain {opt.source}"))
    R._append_csv(WATCH, WATCH_FIELDS, new)
    return pre_rows


def status_block(rows: list, late: bool = False) -> str:
    """Telegram block: what happened to each pre-market card at the confirmation read."""
    if not rows:
        return ""
    icon = {"CONFIRMED": "✓", "NOT CONFIRMED": "✗", "PASSED": "•", "NOT SCANNED": "?"}
    when = E._dt(rows[0]["priced_at"])
    lines = [f"<b>PRE-MARKET CARDS</b> · at the {when} HST confirmation read" + (" (late notice)" if late else "")]
    for r in rows:
        mv = f" · stock {float(r['stock_move_pct']):+.1f}% vs prior close" if r.get("stock_move_pct") not in ("", None) else ""
        px = (f" · option ${float(r['premium']):.2f}" if r.get("premium") not in ("", None) else " · price n/a")
        lines.append(f"{icon.get(r['status'], '')} <b>{E._h(r['ticker'])} {E._h(r['type'])} {float(r['strike']):g}</b> "
                     f"{E._h(r['status'].lower())}: {E._h(r['reason'])}{mv}{px}")
    lines.append("<i>All are tracked from this price on the site (Pre-market watch).</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ grading watch cards

def track_watch(now: datetime, fetch=None) -> None:
    fetch = fetch or R.fetch_contract_bars
    grades, peaks = load_watch_grades(), load_watch_peaks()
    new = []
    for c in load_watch():
        wid = c["id"]
        if c.get("premium") in ("", None) or c["kind"] == "baseline":
            continue                      # baseline rows are graded once after expiry (grade_baseline)
        g = grades.get(wid, {})
        if "T1" in g and "T2" in g and peaks.get(wid, {}).get("final"):
            continue
        start = datetime.fromisoformat(c["priced_at"])
        if now < start:
            continue
        d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
        s_exp = E.session_for(exp) or E.session_for(d)
        finished = now >= s_exp.close_hst + timedelta(minutes=R.GRADE_AFTER_CLOSE_MIN)
        try:
            bars = fetch(c["contract"], d, min(exp, now.date())) if c.get("contract") else None
        except Exception as e:  # noqa: BLE001
            print(f"[warn] watch bars {wid}: {type(e).__name__}: {e}", file=sys.stderr)
            bars = None
        w = None
        if bars is not None:
            w = bars[(bars.index > start) & (bars.index <= (s_exp.close_hst if finished else now))]
            if w.empty:
                w = None
        if w is None:
            if finished and (now.date() - exp).days >= R.GRADE_GIVE_UP_DAYS:
                for tgt in ("T1", "T2"):
                    if tgt not in g:
                        new.append({"id": wid, "target": tgt, "graded_at": now.isoformat(), "result": "UNGRADABLE",
                                    "exit_premium": "", "pnl_pct": "", "hit_at": "", "method": "no contract price data"})
                peaks[wid] = {"final": True, "no_prices": True}
            continue
        entry, stop = float(c["premium"]), float(c["stop"])
        src = bars.attrs.get("source", "")
        p = {"source": src, "as_of": now.isoformat()}
        for tgt, level in (("T1", float(c["target1"])), ("T2", float(c["target"]))):
            r, px, at, how = walk(w, level, stop)
            if r == "WIN":
                p[f"{tgt.lower()}_at"] = at.isoformat()
            if tgt == "T2" and r == "LOSS":
                p["stop_at"] = at.isoformat()
            if tgt in g:
                continue
            if r is not None:
                new.append({"id": wid, "target": tgt, "graded_at": now.isoformat(), "result": r,
                            "exit_premium": round(px, 2), "pnl_pct": _pct(px, entry),
                            "hit_at": at.isoformat(), "method": f"{how} · {src}"})
            elif finished:
                res = "WIN" if px > entry else "LOSS" if px < entry else "FLAT"
                new.append({"id": wid, "target": tgt, "graded_at": now.isoformat(), "result": res,
                            "exit_premium": round(px, 2), "pnl_pct": _pct(px, entry), "hit_at": "",
                            "method": f"TIME ({exp.strftime('%a')} close) · {src}"})
        alive = w[w.index <= datetime.fromisoformat(p["stop_at"])] if p.get("stop_at") else w
        p["peak_pct"] = _pct(float(alive["High"].max()), entry)
        p["peak_at"] = alive["High"].idxmax().isoformat()
        p["last"] = round(float(w["Close"].iloc[-1]), 2)
        p["last_pct"] = _pct(p["last"], entry)
        p["last_time"] = w.index[-1].isoformat()
        p["final"] = finished
        peaks[wid] = p
    R._append_csv(WATCH_GRADES, WG_FIELDS, new)
    WATCH_PEAKS.write_text(json.dumps(peaks, indent=1))


# ------------------------------------------------------------------ baseline (added 2026-11-04)
# Every setup the rules looked at in the confirmation read that has a LIQUID contract (passes
# OI / bid / spread) — called or not, any direction, any rejection reason — priced from that read's
# own chain. Answers: do the rules beat taking everything? Watch-only, never a signal.
# Graded once, after the contract's expiry-day close, in batches (keeps Schwab calls low).

BASELINE_BATCH = 60


def record_baseline(d: date) -> int:
    conf_p = R.run_path(d, "confirm")
    if not conf_p.exists():
        return 0
    conf = json.loads(conf_p.read_text())
    raw = _raw(d, conf["ran_at"], "confirm") or {}
    have = {r["id"] for r in load_watch()}
    names = [x.partition(":")[0].strip() for x in conf["rejections"]]
    names += [f'{c["ticker"]} {"C" if c["type"] == "CALL" else "P"}' for c in conf["cards"]]
    official = {(c["ticker"], c["type"]) for c in conf["cards"]}
    rej = {x.partition(":")[0].strip(): x.partition(":")[2].strip() for x in conf["rejections"]}
    new = []
    for name in dict.fromkeys(names):
        try:
            sym, right = name.split()
        except ValueError:
            continue
        raw_t = (raw.get("tickers") or {}).get(sym)
        spot = ((raw_t or {}).get("tech") or {}).get("last")
        if not raw_t or not spot:
            continue
        opt = E.pick_strike(_chain(raw_t), date.fromisoformat(raw_t["expiry"]), right, spot)
        if opt is None or not opt.mid or not E.liquidity_check(opt)[0]:
            continue
        typ = "CALL" if right == "C" else "PUT"
        wid = f"{d.isoformat()}_watch-baseline_{sym}_{typ}_{opt.expiry.isoformat()}_{opt.strike:g}"
        if wid in have:
            continue
        status = "OFFICIAL" if (sym, typ) in official else "REJECTED"
        base = {"id": wid, "trade_date": d.isoformat(), "kind": "baseline", "ticker": sym, "type": typ,
                "expiry": opt.expiry.isoformat(), "strike": opt.strike,
                "contract": E.occ_symbol("SPXW" if sym == "SPX" else sym, opt),
                "called_at": conf["ran_at"], "priced_at": conf["ran_at"], "underlying": spot,
                "underlying_asof": E._short_t(datetime.fromisoformat(conf["ran_at"])) + " HST",
                "stock_move_pct": "" if _move(raw_t) is None else _move(raw_t),
                "status": status, "reason": rej.get(name, "official card")}
        new.append(_row(base, round(opt.mid, 2), f"chain {opt.source}"))
    R._append_csv(WATCH, WATCH_FIELDS, new)
    return len(new)


def grade_baseline(now: datetime, fetch=None) -> int:
    fetch = fetch or R.fetch_contract_bars
    grades = load_watch_grades()
    new, done = [], 0
    for c in load_watch():
        if c["kind"] != "baseline" or c.get("premium") in ("", None):
            continue
        g = grades.get(c["id"], {})
        if "T1" in g and "T2" in g:
            continue
        d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
        s_exp = E.session_for(exp) or E.session_for(d)
        if now < s_exp.close_hst + timedelta(minutes=R.GRADE_AFTER_CLOSE_MIN):
            continue
        if done >= BASELINE_BATCH:
            break
        done += 1
        try:
            bars = fetch(c["contract"], d, exp) if c.get("contract") else None
        except Exception:  # noqa: BLE001
            bars = None
        start = datetime.fromisoformat(c["priced_at"])
        w = None if bars is None else bars[(bars.index > start) & (bars.index <= s_exp.close_hst)]
        if w is None or w.empty:
            if (now.date() - exp).days >= R.GRADE_GIVE_UP_DAYS:
                for tgt in ("T1", "T2"):
                    if tgt not in g:
                        new.append({"id": c["id"], "target": tgt, "graded_at": now.isoformat(),
                                    "result": "UNGRADABLE", "exit_premium": "", "pnl_pct": "", "hit_at": "",
                                    "method": "no contract price data"})
            continue
        entry, stop, src = float(c["premium"]), float(c["stop"]), bars.attrs.get("source", "")
        for tgt, level in (("T1", float(c["target1"])), ("T2", float(c["target"]))):
            if tgt in g:
                continue
            r, px, at, how = walk(w, level, stop)
            if r is None:
                r, how, at = ("WIN" if px > entry else "LOSS" if px < entry else "FLAT"), \
                    f"TIME ({exp.strftime('%a')} close)", None
            new.append({"id": c["id"], "target": tgt, "graded_at": now.isoformat(), "result": r,
                        "exit_premium": round(px, 2), "pnl_pct": _pct(px, entry),
                        "hit_at": at.isoformat() if at is not None else "", "method": f"{how} · {src}"})
    R._append_csv(WATCH_GRADES, WG_FIELDS, new)
    return done
