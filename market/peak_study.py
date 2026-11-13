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
    Every plan: -20% stop until T1. Bars are processed in order; a bar that opens past a level exits
    at that open; target and stop in the same bar = stop. Returns {plan: pnl %}."""
    def pct(px):
        return round((px - entry) / entry * 100, 1)
    last = float(w["Close"].iloc[-1])
    out = {}
    r1, p1, at1 = walk(w, t1, stop)
    r2, p2, _ = walk(w, t2, stop)
    out["now_banger"] = pct(p2)                                   # rule before 11/13
    out["sell_all_t1"] = pct(p1)
    if r1 != "WIN":                                               # never reached T1: every plan is the same
        for k in ["be_stop", "half_be"] + [f"trail{int(x*100)}{s}" for x in TRAILS for s in ("", "_cap")]:
            out[k] = pct(p1)
        return out
    after = w[w.index > at1]
    if after.empty:
        rb, pb = None, last
    else:
        rb, pb, _ = walk(after, t2, entry)
    out["be_stop"] = pct(pb)
    out["half_be"] = round((pct(t1) + pct(pb)) / 2, 1)
    for tr in TRAILS:
        for cap in (False, True):
            hi = max(float(w.loc[at1, "High"]), t1)
            px = None
            for _, b in after.iterrows():
                st = max(entry, hi * (1 - tr))                    # never below entry once T1 hit
                o = float(b["Open"])
                if o <= st:
                    px = o; break
                if cap and o >= t2:
                    px = o; break
                if float(b["Low"]) <= st:
                    px = st; break
                if cap and float(b["High"]) >= t2:
                    px = t2; break
                hi = max(hi, float(b["High"]))
            out[f"trail{int(tr*100)}{'_cap' if cap else ''}"] = pct(px if px is not None else last)
    return out


def walk(w, target, stop):
    for t, b in w.iterrows():
        o = float(b["Open"])
        if o <= stop:
            return "LOSS", o, t
        if o >= target:
            return "WIN", o, t
        hit_t, hit_s = b["High"] >= target, b["Low"] <= stop
        if hit_s:
            return "LOSS", stop, t
        if hit_t:
            return "WIN", target, t
    return None, float(w["Close"].iloc[-1]), w.index[-1]


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
