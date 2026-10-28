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
    return datetime.fromisoformat(iso).astimezone(E.HST).strftime("%I:%M %p HST").lstrip("0")


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
            f'<a class="tile" href="record.html"><span class="k">Target 2 record (+{int(E.TARGET_PCT * 100)}%)</span>'
            f'<span class="v">{rec(a)}</span><span class="s">win rate {pct(a)}</span></a>'
            f'<a class="tile" href="record.html"><span class="k">A+ record</span>'
            f'<span class="v">{rec(p)}</span><span class="s">win rate {pct(p)}</span></a>'
            f'<div class="tile"><span class="k">Avg premium P&amp;L</span>'
            f'<span class="v">{avg(a)}</span><span class="s">per graded card (T2 trade)</span></div></div>')


def bias_panel(s: dict) -> str:
    b = s["bias"]
    fav = {"P": "puts favored", "C": "calls favored"}.get(b["favored"], "no favored side")
    clean = "clean match" if b["clean"] else "not clean"
    notes = "".join(f"<li>{e(n)}</li>" for n in b["notes"])
    flags = "".join(f"<li>{e(f)}</li>" for f in s["flags"])
    hl = "".join(f"<li>{e(h)}</li>" for h in s.get("headlines") or [])
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
            f'<dt>Target 2</dt><dd>{_money(c["target"])}{stock("t2")}{hit("t2_at")}</dd>'
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


def price_lines_premarket(c: dict) -> str:
    """Pre-market: stock options aren't trading yet, so the premium is the prior close's bid/ask
    mid. Entry, target and stop come from the live price once the confirmation read runs."""
    return (f'<dt>Premium</dt><dd>{_money(c["premium"])} '
            f'<span class="eyebrow">last close · options not trading yet</span></dd>'
            f'<dt>Entry · target · stop</dt><dd class="eyebrow">set at the open</dd>')


def card_html(c: dict, g: dict | None) -> str:
    res = ""
    if g:
        pnl = f' ({float(g["pnl_pct"]):+.1f}%)' if g.get("pnl_pct") not in ("", None) else ""
        res = (f'<dt>Result (T2)</dt><dd><span class="res-{e(g["result"])}">{e(g["result"])}</span>'
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
               f'<dt>Ends</dt><dd class="eyebrow">at Target 2, stop, or {exp_day} close</dd>')
    aplus = c["a_plus"] in (True, "True")
    badge = '<span class="badge gap2">A+</span>' if aplus else ""
    flags = f'<dt>Flags</dt><dd>{e(c["flags"])}</dd>' if c.get("flags") else ""
    return (f'<div class="card{" aplus" if aplus else ""}">'
            f'<div class="head"><span><span class="tk">{e(c["ticker"])}</span> '
            f'<span class="ty {e(c["type"])}">{e(c["type"])}</span> {badge}</span>'
            f'<span class="conf">{e(str(c["confidence"]))}/10</span></div>'
            + (f'<div class="px">Stock {_money(c["underlying"])} '
               f'<span class="eyebrow">as of {e(str(c.get("underlying_asof") or ""))}</span></div>'
               if c.get("underlying") not in (None, "") else "")
            + f'<div class="status">{e(c["status"])} · locked {_t(c["locked_at"])}</div>'
            f'<dl class="lines">'
            f'<dt>Bias</dt><dd>{e(c["bias"])}</dd>'
            f'<dt>Expiry</dt><dd>{e(date.fromisoformat(c["expiry"]).strftime("%a %b %-d"))}</dd>'
            f'<dt>Strike</dt><dd>{float(c["strike"]):g}</dd>'
            + (price_lines_premarket(c) if c["mode"] == "premarket" else price_lines(c)) +
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
            + run_section("Pre-market watch — conditional, not graded", modes.get("premarket"),
                          grades, "Pre-market read didn't run this day."))


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
    return (f'Next pre-market read: {s.at(-75).strftime("%a %I:%M %p")}–'
            f'{s.open_hst.strftime("%I:%M %p")} HST (open {s.open_hst.strftime("%I:%M %p")}).')


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
    global MARKS, T1, PEAKS
    MARKS, T1, PEAKS = load_marks(), load_t1(), load_peaks()
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
             f'<th>T1</th><th>T2 result</th><th>Went on to</th><th>How (T2)</th></tr></thead><tbody>{trs}</tbody></table></div>') if rows \
        else '<div class="empty">No graded cards yet.</div>'
    rec_body = (f'<h1>Locked record</h1>{tiles(st)}<p class="lede">Only GO cards from the confirmation '
                'read and the morning re-scans are graded. Entry is the locked signal premium. Each card is '
                f'scored twice with the same −{int(E.STOP_PCT * 100)}% stop: Target 1 (+{int(E.TARGET1_PCT * 100)}%) '
                f'and Target 2 (+{int(E.TARGET_PCT * 100)}%). Exit is the first of target or stop on the contract\'s '
                'minute prices (both in one bar counts as a stop; a bar that opens past a level exits at that '
                'open), otherwise the last price at the close of the expiry day. "Went on to" is the highest '
                'contract price while the trade was alive — the best possible exit, not a fill. Cards from '
                '10/20–10/23 have no minute prices left: T1 counts as hit when T2 was hit, or when a saved run '
                'quote showed it; otherwise it is "unknown" and not counted as a loss. Mon–Thu cards may be held overnight up to the last '
                'trading day before a weekend or holiday; Friday cards are 0DTE. Pre-market cards are conditional and never graded.</p>' + table)
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
