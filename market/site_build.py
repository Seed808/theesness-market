"""
Builds the static site from the ledger. Every number shown is read from ledger files
written by runner.py; nothing is entered by hand.

Pages:  index.html (latest trading day) · day/<date>.html · record.html · test.html
"""
from __future__ import annotations

import csv
import html
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import engine as E
from style_css import CSS

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "ledger"
e = html.escape

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700'
         '&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;600&display=swap">')


def page(title: str, body: str, depth: int, active: str) -> str:
    up = "../" * depth
    links = [("board", "index.html", "Board"), ("record", "record.html", "Record"),
             ("backtest", "backtest.html", "Backtest"), ("days", "days.html", "Days"),
             ("test", "test.html", "Test runs")]
    nav = "".join(f'<a href="{up}{h}" class="{"on" if k == active else ""}">{lab}</a>'
                  for k, h, lab in links)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{e(title)}</title>{FONTS}<style>{CSS}</style></head><body><div class="wrap">'
            f'<header class="top"><a class="brand" href="{up}index.html"><b>TheEsness</b> Market</a>'
            f'<nav class="sports">{nav}</nav></header>{body}'
            '<footer>Trade cards are rules-engine output on free, ~15-minute-delayed data — not '
            'trading advice. Cards are locked when posted and graded after the close by the same '
            'code. Nothing is entered by hand. Times are Hawaii (HST).</footer></div></body></html>')


def _t(iso: str) -> str:
    """Every time on the site carries its weekday + date, e.g. 'Wed 11/04 5:43 AM HST'."""
    return E._dt(iso) + " HST"


def _money(x) -> str:
    try:
        return f"${float(x):.2f}"
    except (TypeError, ValueError):
        return "—"


# --------------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------------

def load_runs() -> dict:
    """{date: {"premarket": snap, "confirm": snap, "rescans": [snap, ...] oldest first}}"""
    out = {}
    for p in sorted((LEDGER / "runs").glob("*.json")):
        s = json.loads(p.read_text())
        day = out.setdefault(s["trade_date"], {"rescans": []})
        if s["mode"] == "rescan":
            day["rescans"].append(s)
        else:
            day[s["mode"]] = s
    for day in out.values():
        day["rescans"].sort(key=lambda x: x["ran_at"])
    return out


def graded_snaps(modes: dict) -> list:
    return ([modes["confirm"]] if modes.get("confirm") else []) + modes.get("rescans", [])


def load_marks() -> dict:
    p = LEDGER / "marks.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


MARKS: dict = {}
T1: dict = {}          # ledger/grades_t1.csv by card id
PEAKS: dict = {}       # ledger/peaks.json by card id


def load_t1() -> dict:
    p = LEDGER / "grades_t1.csv"
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def load_peaks() -> dict:
    p = LEDGER / "peaks.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


W_ROWS: list = []       # ledger/watch.csv
W_GRADES: dict = {}     # watch id -> {"T1": row, "T2": row}
W_PEAKS: dict = {}      # ledger/watch_peaks.json


def watch_for_pre(c: dict, kind: str = "premarket") -> dict | None:
    for w in W_ROWS:
        if (w["kind"] == kind and w["trade_date"] == c["trade_date"] and w["ticker"] == c["ticker"]
                and w["type"] == c["type"] and w["expiry"] == c["expiry"]
                and float(w["strike"]) == float(c["strike"])):
            return w
    return None


def _res(g: dict | None, tgt: str) -> str:
    if not g or tgt not in g:
        return '<span class="eyebrow">open</span>'
    r = g[tgt]
    pnl = f' ({float(r["pnl_pct"]):+.1f}%)' if r.get("pnl_pct") not in ("", None) else ""
    when = f' {_t(r["hit_at"])}' if r.get("hit_at") else ""
    return f'<span class="res-{e(r["result"])}">{e(r["result"])}</span>{pnl}<span class="eyebrow">{e(when)}</span>'


def watch_lines(w: dict) -> str:
    """Status + price-at-confirmation + T1/T2 tracking for a watch card (not the official record)."""
    icon = {"CONFIRMED": "✓", "NOT CONFIRMED": "✗", "PASSED": "•", "NOT SCANNED": "?", "BLOCKED": "✗",
            "AT THE OPEN": "⏱"}
    mv = (f' · stock {float(w["stock_move_pct"]):+.1f}% vs prior close'
          if w.get("stock_move_pct") not in ("", None) else "")
    out = (f'<dt>At {_t(w["priced_at"])}</dt><dd><b>{icon.get(w["status"], "")} {e(w["status"].title())}</b>: '
           f'{e(w["reason"])}<span class="eyebrow">{e(mv)}</span></dd>')
    if w.get("premium") in ("", None):
        return out + '<dt>Watch</dt><dd class="eyebrow">no option price at that time — not tracked</dd>'
    lv = E.card_level_stocks({**w, "locked_at": w["priced_at"]})

    def stock(k):
        return f' <span class="eyebrow">(stock ≈ {_money(lv[k])})</span>' if k in lv else ""
    g, pk = W_GRADES.get(w["id"]), W_PEAKS.get(w["id"], {})
    out += (f'<dt>Option then</dt><dd>{_money(w["premium"])} <span class="eyebrow">{_t(w["priced_at"])} · '
            f'{e(w.get("price_source", ""))} · tracked from here</span></dd>'
            f'<dt>Target 1</dt><dd>{_money(w["target1"])}{stock("t1")} · {_res(g, "T1")}</dd>'
            f'<dt>Banger</dt><dd>{_money(w["target"])}{stock("t2")} · {_res(g, "T2")}</dd>'
            f'<dt>Stop</dt><dd>{_money(w["stop"])}{stock("stop")}</dd>')
    if pk.get("last") is not None and not (g and "T2" in g):
        out += (f'<dt>Last</dt><dd>{_money(pk["last"])} ({pk["last_pct"]:+.1f}%) '
                f'<span class="eyebrow">{_t(pk["last_time"])}</span></dd>')
    if pk.get("peak_pct") is not None:
        out += (f'<dt>Went on to</dt><dd>{pk["peak_pct"]:+.1f}% <span class="eyebrow">highest {_t(pk["peak_at"])}'
                f'{" · before the stop" if pk.get("stop_at") else ""} · best possible exit, not a fill</span></dd>')
    return out


def t1_of(c: dict) -> float:
    return float(c.get("target1") or 0) or E.target1_price(float(c["premium"]))


def load_grades() -> dict:
    p = LEDGER / "grades.csv"
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def record_rows(runs: dict, grades: dict) -> list:
    rows = []
    for d, modes in runs.items():
        for snap in graded_snaps(modes):
            for c in snap["cards"]:
                if c["status"] == "GO" and c["id"] in grades:
                    rows.append({**c, **grades[c["id"]]})
    return sorted(rows, key=lambda r: r["locked_at"], reverse=True)


def stats(rows: list) -> dict:
    def tally(rs):
        w = sum(r["result"] == "WIN" for r in rs)
        l = sum(r["result"] == "LOSS" for r in rs)
        f = sum(r["result"] == "FLAT" for r in rs)
        pn = [float(r["pnl_pct"]) for r in rs if r.get("pnl_pct") not in ("", None)]
        return {"w": w, "l": l, "f": f, "pct": (w / (w + l) * 100) if w + l else None,
                "avg": (sum(pn) / len(pn)) if pn else None}
    t1 = [T1[r["id"]] for r in rows if r["id"] in T1]
    t1_tally = tally(t1)
    t1_tally["unknown"] = sum(x["result"] == "UNKNOWN" for x in t1)
    return {"all": tally(rows), "aplus": tally([r for r in rows if r["a_plus"] in (True, "True")]),
            "t1": t1_tally}


# --------------------------------------------------------------------------------------------
# pieces
# --------------------------------------------------------------------------------------------

def tiles(st: dict) -> str:
    def rec(t):
        return f'{t["w"]}-{t["l"]}' + (f'-{t["f"]}' if t["f"] else "")
    def pct(t):
        return "—" if t["pct"] is None else f'{t["pct"]:.1f}%'
    def avg(t):
        return "—" if t["avg"] is None else f'{t["avg"]:+.1f}%'
    a, p, t1 = st["all"], st["aplus"], st["t1"]
    unk = f' · {t1["unknown"]} unknown' if t1.get("unknown") else ""
    return ('<div class="tiles">'
            f'<a class="tile" href="record.html"><span class="k">Target 1 record (+{int(E.TARGET1_PCT * 100)}%)</span>'
            f'<span class="v">{rec(t1)}</span><span class="s">win rate {pct(t1)}{unk}</span></a>'
            f'<a class="tile" href="record.html"><span class="k">Bangers record (+{int(E.TARGET_PCT * 100)}%)</span>'
            f'<span class="v">{rec(a)}</span><span class="s">win rate {pct(a)}</span></a>'
            f'<a class="tile" href="record.html"><span class="k">A+ record</span>'
            f'<span class="v">{rec(p)}</span><span class="s">win rate {pct(p)}</span></a>'
            f'<div class="tile"><span class="k">Avg premium P&amp;L</span>'
            f'<span class="v">{avg(a)}</span><span class="s">per graded card (Banger trade)</span></div></div>')


def bias_panel(s: dict) -> str:
    b = s["bias"]
    fav = {"P": "puts favored", "C": "calls favored"}.get(b["favored"], "no favored side")
    clean = "clean match" if b["clean"] else "not clean"
    notes = "".join(f"<li>{e(n)}</li>" for n in b["notes"])
    flags = "".join(f"<li>{e(f)}</li>" for f in s["flags"])
    hl = "".join(f"<li>{e(h)}</li>" for h in s.get("headlines") or [])
    hist = []
    if s["mode"] in ("confirm", "rescan"):
        try:
            import biaswatch
            hist = biaswatch.load(date.fromisoformat(s["trade_date"])).get("history", [])
        except Exception:  # noqa: BLE001
            hist = []
    if len(hist) > 1:                         # the day's bias changed: newest big, earlier ones small
        cur = hist[-1]
        older = "".join(f'<div class="eyebrow">was {e(h["regime"])}'
                        f'{"" if h["regime"] == "CHOP" else (" (clean)" if h["clean"] else " (not clean)")}'
                        f' · {_t(h["at"])}</div>' for h in reversed(hist[:-1]))
        return (f'<div class="bias"><div class="eyebrow">Bias · now</div>'
                f'<div class="reg">{e(cur["regime"])}</div>'
                f'<div>since {_t(cur["at"])} · {e(cur["why"])}</div>{older}'
                f'<div class="eyebrow" style="margin-top:6px">Intraday changes are display only this week.</div>'
                f'<ul>{notes}</ul><div class="eyebrow" style="margin-top:10px">Flags</div><ul>{flags}</ul>'
                + (f'<div class="eyebrow" style="margin-top:10px">Headlines</div><ul>{hl}</ul>' if hl else "")
                + '</div>')
    return (f'<div class="bias"><div class="eyebrow">Bias · {e(s["mode"])} read at {_t(s["ran_at"])}'
            f'</div><div class="reg">{e(b["regime"])}</div><div>{e(fav)} · {e(clean)}</div>'
            f'<ul>{notes}</ul><div class="eyebrow" style="margin-top:10px">Flags</div><ul>{flags}</ul>'
            + (f'<div class="eyebrow" style="margin-top:10px">Headlines</div><ul>{hl}</ul>' if hl else "")
            + '</div>')


def price_lines(c: dict) -> str:
    st = E.card_level_stocks(c)
    pk = PEAKS.get(c["id"], {})

    def stock(key):
        return (f' <span class="eyebrow">(stock ≈ {_money(st[key])})</span>' if key in st else "")

    def hit(at_key):
        return (f' <span class="res-WIN">✓ {_t(pk[at_key])}</span>' if pk.get(at_key) else "")
    return (f'<dt>Entry range</dt><dd>{_money(c["entry_floor"])}–{_money(c["entry_ceiling"])}</dd>'
            f'<dt>Premium</dt><dd>{_money(c["premium"])}</dd>'
            f'<dt>Max contracts</dt><dd class="eyebrow">Telegram only</dd>'
            f'<dt>Target 1</dt><dd>{_money(t1_of(c))}{stock("t1")}{hit("t1_at")}</dd>'
            f'<dt>Banger</dt><dd>{_money(c["target"])}{stock("t2")}{hit("t2_at")}</dd>'
            f'<dt>Stop</dt><dd>{_money(c["stop"])}{stock("stop")}</dd>'
            + ('<dd class="eyebrow" style="grid-column:1/-1">stock ≈ = estimate if reached the day the '
               'card was locked; later in the week the stock has to move further</dd>' if st else ""))


def tracking_lines(c: dict) -> str:
    """T1 result, how high the contract went, and the break-even side number."""
    pk, g1 = PEAKS.get(c["id"], {}), T1.get(c["id"])
    out = ""
    if g1:
        r = g1["result"]
        lab = {"WIN": f"✓ hit +{int(E.TARGET1_PCT * 100)}%", "LOSS": "✗ stopped before T1",
               "UNKNOWN": "unknown", "UNGRADABLE": "no price data"}.get(r, r)
        when = f' {_t(g1["hit_at"])}' if g1.get("hit_at") and r == "WIN" else ""
        out += (f'<dt>Target 1</dt><dd><span class="res-{e(r)}">{e(lab)}</span>{e(when)} '
                f'<span class="eyebrow">{e(g1["method"])}</span></dd>')
    if pk.get("peak_pct") is not None:
        end = "frozen at expiry close" if pk.get("final") else "updates until expiry close"
        if pk.get("stop_at"):
            end = "before the stop"
        out += (f'<dt>Went on to</dt><dd>{pk["peak_pct"]:+.1f}% <span class="eyebrow">highest '
                f'contract price {_t(pk["peak_at"])} · {e(end)} · best possible exit, not a fill</span></dd>')
    if pk.get("be"):
        b = pk["be"]
        out += (f'<dt>BE stop</dt><dd class="eyebrow">if the stop had moved to entry after T1: '
                f'{e(b["result"])} ({b["pnl_pct"]:+.1f}%) · {e(b["how"])} — tracking only</dd>')
    return out


OPEN_PX: dict = {}      # pre-market card id -> price at the open (ledger/open/<date>.json)


def load_open() -> dict:
    out = {}
    for p in sorted((LEDGER / "open").glob("*.json")):
        try:
            out.update(json.loads(p.read_text()).get("cards", {}))
        except ValueError:
            continue
    return out


def price_lines_premarket(c: dict) -> str:
    """Pre-market: stock options aren't trading yet, so the premium is the prior close's bid/ask
    mid. Re-priced at the open (price only), then judged at the confirmation read."""
    o = OPEN_PX.get(c["id"])
    at_open = ""
    if o:
        stock = f' · stock {_money(o["underlying"])}' if o.get("underlying") else ""
        when = datetime.fromisoformat(o["time"]).astimezone(E.HST)
        stamp = _t(o["time"]) if when.date().isoformat() == c["trade_date"] else \
            f'{when.strftime("%a")} {_t(o["time"])} (last trade, no trade yet today)'
        at_open = (f'<dt>At the open</dt><dd>{_money(o["price"])} <span class="eyebrow">{e(stamp)}'
                   f'{e(stock)} · price only, amateur hour</span></dd>')
    return (f'<dt>Premium</dt><dd>{_money(c["premium"])} '
            f'<span class="eyebrow">last close · options not trading yet</span></dd>' + at_open +
            f'<dt>Entry · target · stop</dt><dd class="eyebrow">set at the confirmation read</dd>')


def pre_header(c: dict) -> str:
    """Pre-market card header showing its LATEST state (3:22 read -> open -> confirmation read)."""
    w, o = watch_for_pre(c), OPEN_PX.get(c["id"])
    stock, when = c.get("underlying"), str(c.get("underlying_asof") or "")
    if o and o.get("underlying"):
        stock, when = o["underlying"], f'{_t(o["time"])} (open)'
    if w and w.get("underlying") not in ("", None):
        stock, when = w["underlying"], f'{_t(w["priced_at"])} (confirmation read)'
    was = (f' · was {_money(c["underlying"])} at {_t(c["locked_at"])}'
           if c.get("underlying") not in (None, "") and stock != c.get("underlying") else "")
    px = (f'<div class="px">Stock {_money(stock)} <span class="eyebrow">as of {e(when)}{e(was)}</span></div>'
          if stock not in (None, "") else "")
    if w:
        icon = {"CONFIRMED": "✓", "NOT CONFIRMED": "✗", "PASSED": "•", "NOT SCANNED": "?"}.get(w["status"], "")
        st = f'{icon} {w["status"]} at {_t(w["priced_at"])} — {w["reason"]}'
    elif o:
        st = f'Re-priced at the open {_t(o["time"])} · confirmation read at {_t(E.session_for(date.fromisoformat(c["trade_date"])).at(E.AMATEUR_HOUR_MIN).isoformat())}'
    else:
        st = f'{c["status"]} · called {_t(c["locked_at"])}'
    return px + f'<div class="status">{e(st)} · called {_t(c["locked_at"])}</div>' if (w or o) else \
        px + f'<div class="status">{e(st)}</div>'


def card_html(c: dict, g: dict | None) -> str:
    res = ""
    if g:
        pnl = f' ({float(g["pnl_pct"]):+.1f}%)' if g.get("pnl_pct") not in ("", None) else ""
        res = (f'<dt>Result (Banger)</dt><dd><span class="res-{e(g["result"])}">{e(g["result"])}</span>'
               f'{pnl} <span class="eyebrow">{e(g["method"])}</span></dd>')
    elif c["mode"] in ("confirm", "rescan") and c["status"] == "GO":
        exp_day = date.fromisoformat(c["expiry"]).strftime("%a %-m/%-d")
        m = MARKS.get(c["id"])
        px = ""
        if m:
            rng = (f' · range since entry {_money(m["low"])}–{_money(m["high"])}'
                   if m.get("high") is not None else "")
            when = f'as of {_t(m["last_time"])}' if m.get("last_time") else "latest quote"
            px = (f' last {_money(m["last"])} ({m["pnl_pct"]:+.1f}%) '
                  f'<span class="eyebrow">{when}{rng}</span>')
        res = (f'<dt>Status</dt><dd><span class="res-INPLAY">IN PLAY</span>{px}</dd>'
               f'<dt>Ends</dt><dd class="eyebrow">at the Banger, stop, or {exp_day} close</dd>')
    aplus = c["a_plus"] in (True, "True")
    badge = '<span class="badge gap2">A+</span>' if aplus else ""
    flags = f'<dt>Flags</dt><dd>{e(c["flags"])}</dd>' if c.get("flags") else ""
    official = c["mode"] in ("confirm", "rescan") and c["status"] == "GO"
    cls = "card" + (" official" if official else "") + (" aplus" if aplus else "")
    tag = '<span class="offtag">OFFICIAL</span>' if official else ""
    if c.get("mover"):
        tag += f'<span class="offtag mm" title="Schwab Market Movers: {e(c["mover"])}">MM</span>'
    return (f'<div class="{cls}">'
            f'<div class="head"><span><span class="tk">{e(c["ticker"])}</span>{tag} '
            f'<span class="ty {e(c["type"])}">{e(c["type"])}</span> {badge}</span>'
            f'<span class="conf">{e(str(c["confidence"]))}/10</span></div>'
            + (pre_header(c) if c["mode"] == "premarket" else
               (f'<div class="px">Stock {_money(c["underlying"])} '
                f'<span class="eyebrow">as of {e(E._day(c["locked_at"]))} {e(str(c.get("underlying_asof") or ""))}</span></div>'
                if c.get("underlying") not in (None, "") else "")
               + f'<div class="status">{e(c["status"])} · locked {_t(c["locked_at"])}</div>')
            + f'<dl class="lines">'
            f'<dt>Bias</dt><dd>{e(c["bias"])}</dd>'
            f'<dt>Expiry</dt><dd>{e(date.fromisoformat(c["expiry"]).strftime("%a %b %-d"))}</dd>'
            f'<dt>Strike</dt><dd>{float(c["strike"]):g}</dd>'
            + ((price_lines_premarket(c)
                + (('<dt>From the open</dt><dd class="eyebrow">tracked from the open price (amateur hour)</dd>'
                    + watch_lines(watch_for_pre(c, "preopen"))) if watch_for_pre(c, "preopen") else "")
                + (('<dt>From 5:00 read</dt><dd class="eyebrow">tracked from the confirmation read</dd>'
                    + watch_lines(watch_for_pre(c))) if watch_for_pre(c) else ""))
               if c["mode"] == "premarket" else price_lines(c)) +
            f'{flags}{res}'
            + (tracking_lines(c) if c["mode"] in ("confirm", "rescan") else "") + '</dl>'
            f'<div class="why">{e(c["reasoning"])}</div></div>')


def run_section(title: str, s: dict | None, grades: dict, empty: str) -> str:
    if not s:
        return f'<h2>{e(title)}</h2><div class="empty">{e(empty)}</div>'
    cards = "".join(card_html(c, grades.get(c["id"])) for c in s["cards"])
    body = f'<div class="board">{cards}</div>' if cards else \
        '<div class="empty">No quality setups. Every name was rejected by a rule.</div>'
    return (f'<h2>{e(title)}</h2>{body}<p class="note">{s["passing"]} passed the filters, '
            f'{s["rejected"]} rejected. Data: {e(s.get("data_note") or "~15 min delayed (Yahoo)")}.</p>'
            + (f'<details><summary class="eyebrow">Rejections</summary><ul class="note">'
               + "".join(f"<li>{e(x)}</li>" for x in s["rejections"]) + '</ul></details>'
               if s["rejections"] else ""))


def rescan_section(rescans: list, grades: dict) -> str:
    if not rescans:
        return ""
    cards = [c for snap in reversed(rescans) for c in snap["cards"]]
    body = ('<div class="board">' + "".join(card_html(c, grades.get(c["id"])) for c in cards)
            + '</div>') if cards else '<div class="empty">No new setups since the confirmation read.</div>'
    return (f'<h2>New during the morning — newest first</h2>{body}<p class="note">{len(rescans)} '
            f're-scan(s), last at {_t(rescans[-1]["ran_at"])}. A ticker and direction already posted '
            'today is not posted again.</p>')


def in_play_section(runs: dict, grades: dict) -> str:
    cards = [c for modes in runs.values() for snap in graded_snaps(modes) for c in snap["cards"]
             if c["status"] == "GO" and c["id"] not in grades]
    if not cards:
        return ""
    cards.sort(key=lambda c: c["locked_at"])
    return ('<h2>In play — open cards</h2><p class="note">Locked GO cards that have not hit their target '
            'or stop and have not reached their expiry-day close. Prices are the latest available and '
            'refresh every run.</p><div class="board">'
            + "".join(card_html(c, None) for c in cards) + '</div>')


def gapchase_section(d: str) -> str:
    rows = [w for w in W_ROWS if w["kind"] == "gapchase" and w["trade_date"] == d]
    if not rows:
        return ""
    cards = ""
    for w in rows:
        cards += (f'<div class="card"><div class="head"><span><span class="tk">{e(w["ticker"])}</span> '
                  f'<span class="ty {e(w["type"])}">{e(w["type"])}</span></span>'
                  f'<span class="eyebrow">not a signal</span></div>'
                  f'<div class="px">Stock {_money(w["underlying"])} <span class="eyebrow">as of {e(_t(w["priced_at"]))}</span></div>'
                  f'<div class="status">blocked {e(_t(w["called_at"]))}</div>'
                  f'<dl class="lines"><dt>Expiry</dt><dd>{e(date.fromisoformat(w["expiry"]).strftime("%a %b %-d"))}</dd>'
                  f'<dt>Strike</dt><dd>{float(w["strike"]):g}</dd>{watch_lines(w)}</dl></div>')
    return ('<h2>Blocked by gap chase only — tracked for comparison</h2><p class="note">Setups that passed '
            'every rule except gap chase, priced when they were blocked and tracked to T1/Banger/stop. Not trade '
            'signals and not in the official record — they show whether the gap-chase rule saves or costs '
            f'money.</p><div class="board">{cards}</div>')


def day_body(d: str, modes: dict, grades: dict) -> str:
    sess = E.session_for(date.fromisoformat(d))
    when = (f'Open {sess.open_hst.strftime("%I:%M %p").lstrip("0")} · close '
            f'{sess.close_hst.strftime("%I:%M %p").lstrip("0")} HST'
            + (" · early close" if sess.early_close else "")) if sess else ""
    rescans = modes.get("rescans", [])
    lead = (rescans[-1] if rescans else None) or modes.get("confirm") or modes.get("premarket")
    return (f'<h1>{e(date.fromisoformat(d).strftime("%a %b %-d, %Y"))}</h1>'
            f'<p class="lede">{e(when)}</p>'
            + (bias_panel(lead) if lead else "")
            + rescan_section(rescans, grades)
            + run_section("Confirmed — after the first 5-minute candle", modes.get("confirm"), grades,
                          "Confirmation read hasn't run yet (due 30 minutes after the open).")
            + run_section("Pre-market watch — not in the official record", modes.get("premarket"),
                          grades, "Pre-market read didn't run this day.")
            + gapchase_section(d))


def source_note() -> str:
    p = REPO / "auth" / "schwab_status.json"
    try:
        st = json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        st = {}
    if st.get("connected") and st.get("expires_at"):
        exp = datetime.fromisoformat(st["expires_at"]).astimezone(E.HST)
        return f"Data: Schwab live (login good until {exp.strftime('%a %-m/%-d %I:%M %p')} HST)."
    return "Data: Yahoo, ~15 min delayed."


def next_run_note(now: datetime) -> str:
    d = now.date()
    s = E.session_for(d)
    if s is None or now >= s.open_hst:
        s = E.session_for(E.next_trading_day(d))
    return (f'Next pre-market read: {s.at(-20).strftime("%a %I:%M %p")}–'
            f'{s.open_hst.strftime("%I:%M %p")} HST (open {s.open_hst.strftime("%I:%M %p")}).')


def baseline_tiles() -> str:
    """Official vs everything: average result per card if exited at T1 or held for T2."""
    def stat(rows):
        res = [r for r in rows if r and r["result"] in ("WIN", "LOSS", "FLAT")]
        if not res:
            return "—", "no graded cards yet"
        w = sum(r["result"] == "WIN" for r in res)
        avg = sum(float(r["pnl_pct"]) for r in res) / len(res)
        return f"{w}-{len(res) - w}", f"win rate {w / len(res) * 100:.0f}% · avg {avg:+.1f}%/card"
    base = [w for w in W_ROWS if w["kind"] == "baseline"]
    out = ""
    for tgt, lab in (("T1", f"+{int(E.TARGET1_PCT * 100)}%"), ("T2", f"+{int(E.TARGET_PCT * 100)}%")):
        rec, s = stat([W_GRADES.get(w["id"], {}).get(tgt) for w in base])
        out += (f'<div class="tile"><span class="k">Baseline: every liquid setup · {"Banger" if tgt == "T2" else tgt} ({lab})</span>'
                f'<span class="v">{rec}</span><span class="s">{s} · {len(base)} tracked, graded after expiry</span></div>')
    return out


def watch_record() -> str:
    def tally(kind, tgt):
        rs = [W_GRADES.get(w["id"], {}).get(tgt) for w in W_ROWS if w["kind"] == kind]
        rs = [r for r in rs if r]
        wn = sum(r["result"] == "WIN" for r in rs)
        ln = sum(r["result"] == "LOSS" for r in rs)
        pct = f"{wn / (wn + ln) * 100:.0f}%" if wn + ln else "—"
        return f"{wn}-{ln}", pct
    tiles_ = ""
    for kind, label in (("preopen", "Pre-market from the open"), ("premarket", "Pre-market from 5:00 read"),
                        ("gapchase", "Gap-chase blocked")):
        for tgt, lab in (("T1", f"+{int(E.TARGET1_PCT * 100)}%"), ("T2", f"+{int(E.TARGET_PCT * 100)}%")):
            rec, pct = tally(kind, tgt)
            tiles_ += (f'<div class="tile"><span class="k">{label} · {"Banger" if tgt == "T2" else tgt} ({lab})</span>'
                       f'<span class="v">{rec}</span><span class="s">win rate {pct}</span></div>')
    rows = sorted([w for w in W_ROWS if w["kind"] != "baseline"], key=lambda w: w["priced_at"], reverse=True)
    trs = "".join(
        f'<tr><td class="mono">{e(w["trade_date"])}</td><td>{e({"premarket": "Pre-market (5:00 read)", "preopen": "Pre-market (open)"}.get(w["kind"], "Gap chase"))}</td>'
        f'<td>{e(w["ticker"])}</td><td>{e(w["type"])}</td><td>{float(w["strike"]):g}</td>'
        f'<td>{e(w["status"].title())}</td><td>{_money(w["premium"]) if w.get("premium") not in ("", None) else "—"}</td>'
        f'<td>{_res(W_GRADES.get(w["id"]), "T1")}</td><td>{_res(W_GRADES.get(w["id"]), "T2")}</td>'
        f'<td>{"" if W_PEAKS.get(w["id"], {}).get("peak_pct") is None else f"{W_PEAKS[w['id']]['peak_pct']:+.1f}%"}</td></tr>'
        for w in rows)
    table = ('<div class="tbl"><table><thead><tr><th>Date</th><th>Kind</th><th>Ticker</th><th>Type</th>'
             '<th>Strike</th><th>Status</th><th>Priced at</th><th>T1</th><th>Banger</th><th>Went on to</th></tr></thead>'
             f'<tbody>{trs}</tbody></table></div>') if rows else '<div class="empty">Nothing tracked yet.</div>'
    tiles_ += baseline_tiles()
    return ('<h2>Watch records — not trade signals</h2><p class="note">Kept apart from the official record. '
            '<b>Pre-market</b>: every pre-market card, tracked twice — from the open price (amateur hour; shows what '
            'that rule costs or saves) and from the confirmation read (whether or not it was confirmed). <b>Gap-chase blocked</b>: setups rejected only by the gap-chase rule, priced when '
            'blocked. <b>Baseline</b>: every setup at the confirmation read with a liquid contract, called or not '
            '(answers whether the rules beat taking everything); graded once after expiry. '
            'Same T1/Banger/−20% stop rules as real cards. Started 11/2.</p>'
            f'<div class="tiles">{tiles_}</div>{table}')


def t1_cell(cid: str) -> str:
    g = T1.get(cid)
    if not g:
        return '<span class="eyebrow">open</span>'
    r = g["result"]
    return f'<span class="res-{e(r)}">{e(r)}</span>'


def peak_cell(cid: str) -> str:
    pk = PEAKS.get(cid, {})
    return "" if pk.get("peak_pct") is None else f'{pk["peak_pct"]:+.1f}%'


# --------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------

def build(out: Path, now: datetime) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "day").mkdir(parents=True)
    (out / ".nojekyll").touch()
    runs, grades = load_runs(), load_grades()
    global MARKS, T1, PEAKS, W_ROWS, W_GRADES, W_PEAKS
    MARKS, T1, PEAKS = load_marks(), load_t1(), load_peaks()
    global OPEN_PX
    OPEN_PX = load_open()
    try:
        import watch as W
        W_ROWS, W_GRADES, W_PEAKS = W.load_watch(), W.load_watch_grades(), W.load_watch_peaks()
    except Exception as ex:  # noqa: BLE001
        print(f"[warn] watch data: {type(ex).__name__}: {ex}")
    rows = record_rows(runs, grades)
    st = stats(rows)
    days = sorted(runs, reverse=True)

    for d in days:
        (out / "day" / f"{d}.html").write_text(page(d, day_body(d, runs[d], grades), 1, "days"))

    if days:
        latest = days[0]
        body = (tiles(st) + in_play_section(runs, grades) + day_body(latest, runs[latest], grades)
                + f'<p class="note">{e(next_run_note(now))} {e(source_note())} Page built {e(E.fmt_hst(now))}.</p>')
    else:
        body = (tiles(st) + '<h1>No runs yet</h1><div class="empty">'
                f'{e(next_run_note(now))}</div><p class="note">Page built {e(E.fmt_hst(now))}.</p>')
    (out / "index.html").write_text(page("TheEsness Market", body, 0, "board"))

    trs = "".join(
        f'<tr><td class="mono">{e(r["trade_date"])}</td><td>{e(r["ticker"])}</td>'
        f'<td>{e(r["type"])}</td><td class="mono">{e(r["expiry"])}</td><td>{float(r["strike"]):g}</td>'
        f'<td>{_money(r["premium"])}</td><td>{_money(r["exit_premium"])}</td>'
        f'<td>{"" if r["pnl_pct"] in ("", None) else f"{float(r["pnl_pct"]):+.1f}%"}</td>'
        f'<td>{e(str(r["confidence"]))}</td><td>{"A+" if r["a_plus"] in (True, "True") else ""}</td>'
        f'<td>{t1_cell(r["id"])}</td>'
        f'<td><span class="res-{e(r["result"])}">{e(r["result"])}</span></td>'
        f'<td>{peak_cell(r["id"])}</td>'
        f'<td class="mono">{e(r["method"])}</td></tr>' for r in rows)
    table = ('<div class="tbl"><table><thead><tr><th>Date</th><th>Ticker</th><th>Type</th><th>Expiry'
             '</th><th>Strike</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Conf</th><th>A+</th>'
             f'<th>T1</th><th>Banger result</th><th>Went on to</th><th>How (Banger)</th></tr></thead><tbody>{trs}</tbody></table></div>') if rows \
        else '<div class="empty">No graded cards yet.</div>'
    rec_body = (f'<h1>Locked record</h1>{tiles(st)}<p class="lede">Only GO cards from the confirmation '
                'read and the morning re-scans are graded. Entry is the locked signal premium. Each card is '
                f'scored twice with the same −{int(E.STOP_PCT * 100)}% stop: Target 1 (+{int(E.TARGET1_PCT * 100)}%) '
                f'and the Banger (+{int(E.TARGET_PCT * 100)}%). Exit is the first of target or stop on the contract\'s '
                'minute prices (both in one bar counts as a stop; a bar that opens past a level exits at that '
                'open), otherwise the last price at the close of the expiry day. "Went on to" is the highest '
                'contract price while the trade was alive — the best possible exit, not a fill. Cards from '
                '10/20–10/23 have no minute prices left: T1 counts as hit when the Banger was hit, or when a saved run '
                'quote showed it; otherwise it is "unknown" and not counted as a loss. Mon–Thu cards may be held overnight up to the last '
                'trading day before a weekend or holiday; Friday cards are 0DTE. Pre-market cards are conditional and never graded.</p>' + table)
    rec_body += watch_record()
    (out / "record.html").write_text(page("Record — TheEsness Market", rec_body, 0, "record"))

    links = "".join(f'<a href="day/{d}.html">{e(d)}</a>' for d in days)
    days_body = ('<h1>Days</h1>' + (f'<nav class="sports weeks">{links}</nav>' if days
                                    else '<div class="empty">No runs yet.</div>'))
    (out / "days.html").write_text(page("Days — TheEsness Market", days_body, 0, "days"))

    tests = sorted((LEDGER / "tests").glob("*.json"), reverse=True)
    tb, shown = "", 0
    for p in tests:
        try:
            s = json.loads(p.read_text())
        except ValueError:
            continue
        if "mode" not in s or "bias" not in s or shown >= 10:    # skip study/diag files
            continue
        shown += 1
        tb += (f'<h2>Test {e(s["mode"])} — {e(E.fmt_hst(datetime.fromisoformat(s["ran_at"])))}</h2>'
               + bias_panel(s) + run_section("Cards", s, {}, ""))
    test_body = ('<h1>Test runs</h1><p class="lede">Started by hand from GitHub (Actions → market → '
                 'Run workflow). These ignore the clock, are never graded, and never count toward '
                 'the record.</p>' + (tb or '<div class="empty">No test runs yet.</div>'))
    (out / "test.html").write_text(page("Test runs — TheEsness Market", test_body, 0, "test"))
    (out / "backtest.html").write_text(page("Backtest — TheEsness Market", backtest_body(), 0, "backtest"))


def backtest_body() -> str:
    p = LEDGER / "backtest.json"
    intro = ('<h1>Bias backtest</h1><p class="lede">Replays the ES/YM + VIX + SPY/QQQ bias rules '
             'on every past day Yahoo still has 5-minute bars for, reading the market 30 minutes before '
             'the open exactly like the live pre-market read. Outcome: from the end of amateur hour '
             '(open+30) to open+150 min, did SPY / QQQ move the favored way by the threshold before '
             'moving the other way? This tests the bias rules on the stock, not the option cards — '
             'free data has no past option prices. Card-level backtests become possible as option '
             'data saved by each run accumulates.</p>')
    if not p.exists():
        return intro + '<div class="empty">First backtest runs after today\'s close.</div>'
    bt = json.loads(p.read_text())
    if bt.get("error"):
        return intro + f'<div class="empty">Last attempt failed: {e(bt["error"])}</div>'
    pr = bt["params"]
    meta = (f'<p class="note">Range {e(bt["range"][0])} to {e(bt["range"][1])} · {len(bt["days"])} days · '
            f'threshold {pr["move_pct"]}% · window open+{pr["start_min"]} to open+{pr["end_min"]} min · '
            f'flat band ±{pr["flat_band_pct"]}% · VIX confirm {pr["vix_confirm_pct"]}% · '
            f'{bt["unscored_days"]} day(s) had no favored side and are not scored · VIX had a pre-market '
            f'price on {bt["vix_premarket_bar_days"]} of {len(bt["days"])} days · built '
            f'{e(E.fmt_hst(datetime.fromisoformat(bt["built_at"])))}.</p>') if bt.get("range") else ""

    def cell(t):
        pct = "—" if t["win_pct"] is None else f'{t["win_pct"]:.1f}%'
        return f'<td>{t["win"]}-{t["loss"]} <span class="eyebrow">({t["none"]} none)</span></td><td>{pct}</td>'
    srows = "".join(f'<tr><td>{e(x["regime"])}</td><td>{"clean" if x["clean"] else "not clean"}</td>'
                    f'<td>{x["SPY"]["days"]}</td>{cell(x["SPY"])}{cell(x["QQQ"])}</tr>' for x in bt["summary"])
    summary = ('<h2>By regime</h2><div class="tbl"><table><thead><tr><th>Regime</th><th>Match</th>'
               '<th>Days</th><th>SPY W-L</th><th>SPY win%</th><th>QQQ W-L</th><th>QQQ win%</th></tr>'
               f'</thead><tbody>{srows}</tbody></table></div>') if srows else \
        '<div class="empty">No scored days yet.</div>'

    def ch(x, k):
        v = x["chg"].get(k)
        return "—" if v is None else f"{v:+.2f}%"
    drows = "".join(
        f'<tr><td class="mono">{e(x["date"])}</td><td>{e(x["regime"])}</td>'
        f'<td>{ {"P": "puts", "C": "calls"}.get(x["favored"], "—")}</td><td>{"yes" if x["clean"] else ""}</td>'
        + "".join(f'<td>{ch(x, k)}</td>' for k in ("ES", "NQ", "YM", "VIX"))
        + f'<td><span class="res-{e(x["SPY"])}">{e(x["SPY"])}</span></td>'
        f'<td><span class="res-{e(x["QQQ"])}">{e(x["QQQ"])}</span></td></tr>' for x in bt["days"])
    days = ('<h2>Every day</h2><div class="tbl"><table><thead><tr><th>Date</th><th>Regime</th><th>Favored</th>'
            '<th>Clean</th><th>ES</th><th>NQ</th><th>YM</th><th>VIX</th><th>SPY</th><th>QQQ</th></tr></thead>'
            f'<tbody>{drows}</tbody></table></div>')
    return intro + meta + summary + days
