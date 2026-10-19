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
             ("days", "days.html", "Days"), ("test", "test.html", "Test runs")]
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
    """{date: {"premarket": snap, "confirm": snap}}"""
    out = {}
    for p in sorted((LEDGER / "runs").glob("*.json")):
        s = json.loads(p.read_text())
        out.setdefault(s["trade_date"], {})[s["mode"]] = s
    return out


def load_grades() -> dict:
    p = LEDGER / "grades.csv"
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def record_rows(runs: dict, grades: dict) -> list:
    rows = []
    for d, modes in runs.items():
        for c in (modes.get("confirm") or {}).get("cards", []):
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
    return {"all": tally(rows), "aplus": tally([r for r in rows if r["a_plus"] in (True, "True")])}


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
    a, p = st["all"], st["aplus"]
    return ('<div class="tiles">'
            f'<a class="tile" href="record.html"><span class="k">Record (GO cards)</span>'
            f'<span class="v">{rec(a)}</span><span class="s">win rate {pct(a)}</span></a>'
            f'<a class="tile" href="record.html"><span class="k">A+ record</span>'
            f'<span class="v">{rec(p)}</span><span class="s">win rate {pct(p)}</span></a>'
            f'<div class="tile"><span class="k">Avg premium P&amp;L</span>'
            f'<span class="v">{avg(a)}</span><span class="s">per graded card</span></div></div>')


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


def card_html(c: dict, g: dict | None) -> str:
    res = ""
    if g:
        pnl = f' ({float(g["pnl_pct"]):+.1f}%)' if g.get("pnl_pct") not in ("", None) else ""
        res = (f'<dt>Result</dt><dd><span class="res-{e(g["result"])}">{e(g["result"])}</span>'
               f'{pnl} <span class="eyebrow">{e(g["method"])}</span></dd>')
    elif c["mode"] == "confirm" and c["status"] == "GO":
        res = '<dt>Result</dt><dd class="eyebrow">graded after the close</dd>'
    aplus = c["a_plus"] in (True, "True")
    badge = '<span class="badge gap2">A+</span>' if aplus else ""
    flags = f'<dt>Flags</dt><dd>{e(c["flags"])}</dd>' if c.get("flags") else ""
    return (f'<div class="card{" aplus" if aplus else ""}">'
            f'<div class="head"><span><span class="tk">{e(c["ticker"])}</span> '
            f'<span class="ty {e(c["type"])}">{e(c["type"])}</span> {badge}</span>'
            f'<span class="conf">{e(str(c["confidence"]))}/10</span></div>'
            f'<div class="status">{e(c["status"])} · locked {_t(c["locked_at"])}</div>'
            f'<dl class="lines">'
            f'<dt>Bias</dt><dd>{e(c["bias"])}</dd>'
            f'<dt>Expiry</dt><dd>{e(c["expiry"])}</dd>'
            f'<dt>Strike</dt><dd>{float(c["strike"]):g}</dd>'
            f'<dt>Entry range</dt><dd>{_money(c["entry_floor"])}–{_money(c["entry_ceiling"])}</dd>'
            f'<dt>Premium</dt><dd>{_money(c["premium"])}</dd>'
            f'<dt>Max contracts</dt><dd class="eyebrow">Telegram only</dd>'
            f'<dt>Target</dt><dd>{_money(c["target"])}</dd>'
            f'<dt>Stop</dt><dd>{_money(c["stop"])}</dd>'
            f'{flags}{res}</dl>'
            f'<div class="why">{e(c["reasoning"])}</div></div>')


def run_section(title: str, s: dict | None, grades: dict, empty: str) -> str:
    if not s:
        return f'<h2>{e(title)}</h2><div class="empty">{e(empty)}</div>'
    cards = "".join(card_html(c, grades.get(c["id"])) for c in s["cards"])
    body = f'<div class="board">{cards}</div>' if cards else \
        '<div class="empty">No quality setups. Every name was rejected by a rule.</div>'
    return (f'<h2>{e(title)}</h2>{body}<p class="note">{s["passing"]} passed the filters, '
            f'{s["rejected"]} rejected. Data is ~15 min delayed unless marked ToS.</p>'
            + (f'<details><summary class="eyebrow">Rejections</summary><ul class="note">'
               + "".join(f"<li>{e(x)}</li>" for x in s["rejections"]) + '</ul></details>'
               if s["rejections"] else ""))


def day_body(d: str, modes: dict, grades: dict) -> str:
    sess = E.session_for(date.fromisoformat(d))
    when = (f'Open {sess.open_hst.strftime("%I:%M %p").lstrip("0")} · close '
            f'{sess.close_hst.strftime("%I:%M %p").lstrip("0")} HST'
            + (" · early close" if sess.early_close else "")) if sess else ""
    lead = modes.get("confirm") or modes.get("premarket")
    return (f'<h1>{e(date.fromisoformat(d).strftime("%a %b %-d, %Y"))}</h1>'
            f'<p class="lede">{e(when)}</p>'
            + (bias_panel(lead) if lead else "")
            + run_section("Confirmed — after the first 5-minute candle", modes.get("confirm"), grades,
                          "Confirmation read hasn't run yet (due 30 minutes after the open).")
            + run_section("Pre-market watch — conditional, not graded", modes.get("premarket"),
                          grades, "Pre-market read didn't run this day."))


def next_run_note(now: datetime) -> str:
    d = now.date()
    s = E.session_for(d)
    if s is None or now >= s.open_hst:
        s = E.session_for(E.next_trading_day(d))
    return (f'Next pre-market read: {s.at(-75).strftime("%a %I:%M %p")}–'
            f'{s.open_hst.strftime("%I:%M %p")} HST (open {s.open_hst.strftime("%I:%M %p")}).')


# --------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------

def build(out: Path, now: datetime) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "day").mkdir(parents=True)
    (out / ".nojekyll").touch()
    runs, grades = load_runs(), load_grades()
    rows = record_rows(runs, grades)
    st = stats(rows)
    days = sorted(runs, reverse=True)

    for d in days:
        (out / "day" / f"{d}.html").write_text(page(d, day_body(d, runs[d], grades), 1, "days"))

    if days:
        latest = days[0]
        body = (tiles(st) + day_body(latest, runs[latest], grades)
                + f'<p class="note">{e(next_run_note(now))} Page built {e(E.fmt_hst(now))}.</p>')
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
        f'<td><span class="res-{e(r["result"])}">{e(r["result"])}</span></td>'
        f'<td class="mono">{e(r["method"])}</td></tr>' for r in rows)
    table = ('<div class="tbl"><table><thead><tr><th>Date</th><th>Ticker</th><th>Type</th><th>Expiry'
             '</th><th>Strike</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Conf</th><th>A+</th>'
             f'<th>Result</th><th>How</th></tr></thead><tbody>{trs}</tbody></table></div>') if rows \
        else '<div class="empty">No graded cards yet.</div>'
    rec_body = (f'<h1>Locked record</h1>{tiles(st)}<p class="lede">Only GO cards from the confirmation '
                'read are graded. Entry is the locked signal premium. Exit is the first of target or '
                'stop on 5-minute bars (both in one bar counts as a stop), otherwise the day\'s last '
                'price. Pre-market cards are conditional and never graded.</p>' + table)
    (out / "record.html").write_text(page("Record — TheEsness Market", rec_body, 0, "record"))

    links = "".join(f'<a href="day/{d}.html">{e(d)}</a>' for d in days)
    days_body = ('<h1>Days</h1>' + (f'<nav class="sports weeks">{links}</nav>' if days
                                    else '<div class="empty">No runs yet.</div>'))
    (out / "days.html").write_text(page("Days — TheEsness Market", days_body, 0, "days"))

    tests = sorted((LEDGER / "tests").glob("*.json"), reverse=True)[:10]
    tb = ""
    for p in tests:
        s = json.loads(p.read_text())
        tb += (f'<h2>Test {e(s["mode"])} — {e(E.fmt_hst(datetime.fromisoformat(s["ran_at"])))}</h2>'
               + bias_panel(s) + run_section("Cards", s, {}, ""))
    test_body = ('<h1>Test runs</h1><p class="lede">Started by hand from GitHub (Actions → market → '
                 'Run workflow). These ignore the clock, are never graded, and never count toward '
                 'the record.</p>' + (tb or '<div class="empty">No test runs yet.</div>'))
    (out / "test.html").write_text(page("Test runs — TheEsness Market", test_body, 0, "test"))
