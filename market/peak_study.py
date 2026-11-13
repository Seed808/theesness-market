"""
Read-only target study. For every graded GO card, walks the contract's real minute prices from
lock time to its actual exit and records the best gain reached BEFORE the exit (did it go green,
how far, then come back?). Also replays each card with other targets (same locked stop, same
gap/same-bar logic as the live grader) so target levels can be compared.
Writes ledger/tests/peak_study.json. Changes no cards, rules or grades.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402

TARGETS = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
TRAILS = [0.10, 0.15, 0.20, 0.25]       # trailing stop distance below the highest price since T1


def exit_plans(w, entry, stop, t1, t2):
    """Exit plans compared on the same bars (11/12, Kalani asked about a trailing stop after T1).
    Same conventions as the live grader: a bar that opens past a level exits at that open; target
    and stop in the same bar = stop; once a bar's High reaches T1, the moved stop applies from the
    NEXT bar. Returns {plan: pnl %}."""
    def pct(px):
        return round((px - entry) / entry * 100, 1)

    def run(sell_t1, cap, trail):
        """trail None = keep the -20% stop; 0 = stop to entry after T1; x = trail x below the high."""
        hit, hi, st = False, None, stop
        for _, b in w.iterrows():
            o, h, l = float(b["Open"]), float(b["High"]), float(b["Low"])
            if o <= st:
                return o
            if sell_t1 and o >= t1:
                return o
            if cap and o >= cap:
                return o
            if l <= st:
                return st
            if sell_t1 and h >= t1:
                return t1
            if cap and h >= cap:
                return cap
            if not hit and h >= t1:
                hit, hi = True, h
            elif hit:
                hi = max(hi, h)
            if hit and trail is not None:
                st = max(entry, hi * (1 - trail)) if trail else entry
        return float(w["Close"].iloc[-1])

    out = {"now_banger": pct(run(False, t2, None)),            # rule before 11/13
           "sell_all_t1": pct(run(True, None, None)),
           "be_stop": pct(run(False, t2, 0))}                   # built for cards from 11/13
    out["half_be"] = round((out["sell_all_t1"] + out["be_stop"]) / 2, 1)
    for tr in TRAILS:
        out[f"trail{int(tr*100)}"] = pct(run(False, None, tr))        # no Banger cap
        out[f"trail{int(tr*100)}_cap"] = pct(run(False, t2, tr))      # still sells at the Banger
    return out


def main() -> int:
    now = E.now_hst()
    try:
        import schwab as S
        E.PROVIDER = S.provider_if_connected()
    except Exception:  # noqa: BLE001
        E.PROVIDER = None
    grades = R.load_grades()
    out = []
    for c in R.load_locks():
        if c["mode"] not in R.GRADED_MODES or c["status"] != "GO" or c["id"] not in grades:
            continue
        g = grades[c["id"]]
        d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
        s_exp = E.session_for(exp) or E.session_for(d)
        entry, stop = float(c["premium"]), float(c["stop"])
        row = {"card": f'{c["trade_date"]} {c["mode"]} {c["ticker"]} {c["type"]} {float(c["strike"]):g}',
               "entry": entry, "stop_pct": round((stop - entry) / entry * 100, 1),
               "result": g.get("result"), "pnl_pct": g.get("pnl_pct"), "method": g.get("method")}
        bars = R.fetch_contract_bars(c["contract"], d, exp) if c.get("contract") else None
        if bars is None:
            row["error"] = "no bars"
            out.append(row)
            continue
        w = bars[(bars.index >= datetime.fromisoformat(c["locked_at"])) & (bars.index <= s_exp.close_hst)]
        if w.empty:
            row["error"] = "no bars after lock"
            out.append(row)
            continue
        row["source"] = bars.attrs.get("source")
        # actual exit time under the live rules (locked target + stop)
        res, px, t_exit = walk(w, float(c["target"]), stop)
        before = w[w.index <= t_exit]
        hi = float(before["High"].max())
        row["peak_before_exit_pct"] = round((hi - entry) / entry * 100, 1)
        row["peak_at"] = str(before["High"].idxmax())
        row["exit_at"] = str(t_exit)
        row["peak_to_expiry_pct"] = round((float(w["High"].max()) - entry) / entry * 100, 1)
        for tp in TARGETS:
            r2, p2, t2 = walk(w, round(entry * (1 + tp), 2), stop)
            if r2 is None:
                r2 = "WIN" if p2 > entry else "LOSS" if p2 < entry else "FLAT"
            row[f"t{int(tp * 100)}"] = {"result": r2, "pnl_pct": round((p2 - entry) / entry * 100, 1),
                                        "at": str(t2)}
        row["plans"] = exit_plans(w, entry, stop, E.target1_price(entry), float(c["target"]))
        out.append(row)
    summ = {}
    pl = [r["plans"] for r in out if "plans" in r]
    plans = {k: {"n": len(pl), "avg_pnl_pct": round(sum(x[k] for x in pl) / len(pl), 1),
                 "win": sum(x[k] > 0 for x in pl), "loss": sum(x[k] < 0 for x in pl)}
             for k in (pl[0] if pl else {})}
    for tp in TARGETS:
        k = f"t{int(tp * 100)}"
        rs = [r[k] for r in out if k in r]
        summ[k] = {"win": sum(x["result"] == "WIN" for x in rs), "loss": sum(x["result"] == "LOSS" for x in rs),
                   "flat": sum(x["result"] == "FLAT" for x in rs),
                   "total_pnl_pct": round(sum(x["pnl_pct"] for x in rs), 1),
                   "avg_pnl_pct": round(sum(x["pnl_pct"] for x in rs) / len(rs), 1) if rs else None}
    res = {"built_at": now.isoformat(), "cards": out, "summary": summ, "exit_plans": plans}
    p = R.LEDGER / "tests" / "peak_study.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(summ, indent=1))
    print("EXIT PLANS")
    print(json.dumps(plans, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
