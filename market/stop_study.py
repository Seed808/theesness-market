"""
Stop-loss study: regrades every locked GO card under several stop levels, using each contract's
real 5-minute prices from lock time to its expiry-day close (or now, if still open).
Target stays at the current TARGET_PCT. Writes ledger/stop_study.json. Changes nothing else.

Same walk as the live grader: first bar to touch target -> WIN, first to touch stop -> LOSS,
both in one bar -> LOSS. Neither by the end -> last price (still OPEN if not yet expired).
Also records each card's worst drawdown and best gain while it was open.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import runner as R  # noqa: E402

STOPS = [0.15, 0.20, 0.25, 0.30, 0.35]


def walk(w, entry, target, stop):
    for t, b in w.iterrows():
        o = float(b["Open"])
        if o <= stop:
            return "LOSS", o, t          # gapped through the stop: exit at the open
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
    cards, out = [], []
    for c in R.load_locks():
        if c["mode"] in R.GRADED_MODES and c["status"] == "GO":
            cards.append(c)
    for c in cards:
        d, exp = date.fromisoformat(c["trade_date"]), date.fromisoformat(c["expiry"])
        s_exp = E.session_for(exp) or E.session_for(d)
        end = min(s_exp.close_hst, now)
        bars = R.fetch_contract_bars(c["contract"], d, min(exp, now.date())) if c.get("contract") else None
        row = {"id": c["id"], "card": f'{c["ticker"]} {c["type"]} {float(c["strike"]):g}',
               "locked_at": c["locked_at"], "entry": float(c["premium"])}
        if bars is None:
            row["error"] = "no 5m bars"
            out.append(row)
            continue
        w = bars[(bars.index >= datetime.fromisoformat(c["locked_at"])) & (bars.index <= end)]
        if w.empty:
            row["error"] = "no bars after lock"
            out.append(row)
            continue
        entry = row["entry"]
        target = round(entry * (1 + E.TARGET_PCT), 2)
        row["worst_pct"] = round((float(w["Low"].min()) - entry) / entry * 100, 1)
        row["best_pct"] = round((float(w["High"].max()) - entry) / entry * 100, 1)
        finished = now >= s_exp.close_hst
        for sp in STOPS:
            stop = round(entry * (1 - sp), 2)
            res, px, t = walk(w, entry, target, stop)
            if res is None:
                res = ("WIN" if px > entry else "LOSS" if px < entry else "FLAT") if finished else "OPEN"
            row[f"stop{int(sp * 100)}"] = {"result": res, "pnl_pct": round((px - entry) / entry * 100, 1),
                                           "at": t.isoformat()}
        out.append(row)

    summary = {}
    for sp in STOPS:
        k = f"stop{int(sp * 100)}"
        rs = [r[k] for r in out if k in r]
        done = [x for x in rs if x["result"] != "OPEN"]
        summary[k] = {"win": sum(x["result"] == "WIN" for x in done),
                      "loss": sum(x["result"] == "LOSS" for x in done),
                      "flat": sum(x["result"] == "FLAT" for x in done),
                      "open": sum(x["result"] == "OPEN" for x in rs),
                      "total_pnl_pct": round(sum(x["pnl_pct"] for x in done), 1),
                      "avg_pnl_pct": round(sum(x["pnl_pct"] for x in done) / len(done), 1) if done else None}
    res = {"built_at": now.isoformat(), "target_pct": E.TARGET_PCT * 100, "summary": summary, "cards": out}
    (R.LEDGER / "stop_study.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
