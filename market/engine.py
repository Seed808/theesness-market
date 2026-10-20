#!/usr/bin/env python3
"""
TheEsness Market — rules engine (Mauna Kala rules)
==================================================

Deterministic rules engine. No LLM calls. Free/delayed data (yfinance, Cboe delayed CDN)
with optional ThinkorSwim paste-in overrides. Used by runner.py (GitHub Actions) and can
also be run by hand on a computer.

TIME HANDLING — READ THIS FIRST
-------------------------------
* Every time in this script is a timezone-aware datetime.
* HST zone name is "Pacific/Honolulu". The name "America/Honolulu" is NOT a valid IANA zone
  and raises ZoneInfoNotFoundError — that is why the spec's literal string is not used.
* Market hours are NOT hardcoded as 3:30am HST. They are defined in exchange time
  (9:30–16:00 America/New_York) and converted to HST. Hawaii has no DST but New York does, so:
      - US daylight time (Mar–Nov):  open 3:30am HST, close 10:00am HST
      - US standard time (Nov–Mar):  open 4:30am HST, close 11:00am HST
  Hardcoding 3:30 would make every window an hour wrong for ~4 months a year.
* Run  `python premarket.py --check-time`  before relying on the script.

MODES
-----
  --mode premarket   ~30 min before the open. Bias read + CONDITIONAL trade cards.
                     Never sends A+ alerts (the first-5-min-candle rule can't be met pre-open).
  --mode confirm     Open+30 to open+60 min (the institutional window). Re-checks every idea
                     with intraday 1m/5m/15m data, the first 5-min candle and VWAP.
                     Sends the separate A+ alert when all A+ criteria pass.

Usage:  python premarket.py --check-time
        python premarket.py --mode premarket --dry-run
        python premarket.py --mode confirm --tos tos_override.txt
        python premarket.py --mode premarket --scheduled     (exits quietly outside its window)
        python premarket.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# =============================================================================================
# CONFIG — edit here
# =============================================================================================

# --- Secrets: read from environment, never hardcoded ---------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Account & risk -------------------------------------------------------------------------
# Kept OUT of the public repo: set these as GitHub repository secrets (or env vars locally).
def _num_env(name: str, pct: bool = False) -> Optional[float]:
    """Reads a number from a secret, tolerating "$10,000", "10%", spaces. Bad value -> None."""
    raw = (os.environ.get(name) or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        v = float(raw.rstrip("%"))
    except ValueError:
        print(f"[warn] secret {name} is not a number — ignored", file=sys.stderr)
        return None
    if pct and (raw.endswith("%") or v >= 1):   # "10" or "10%" both mean 10%
        v /= 100
    return v


ACCOUNT_SIZE = _num_env("ACCOUNT_SIZE") or 0.0     # USD, settled cash only

# Risk method used for Max Contracts.  RISK IS NOT YET DETERMINED — set one of these.
#   "percent"      -> risk RISK_PCT of ACCOUNT_SIZE per trade (e.g. 0.10 = 10%)
#   "fixed_dollar" -> risk RISK_FIXED_DOLLARS per trade
# While RISK_PCT / RISK_FIXED_DOLLARS is None, cards show "UNSET" for Max Contracts.
RISK_METHOD = "fixed_dollar" if (os.environ.get("RISK_FIXED_DOLLARS") and not os.environ.get("RISK_PCT")) else "percent"
RISK_PCT: Optional[float] = _num_env("RISK_PCT", pct=True)
RISK_FIXED_DOLLARS: Optional[float] = _num_env("RISK_FIXED_DOLLARS")
# Risk per contract = (entry ceiling - stop premium) * 100.  Also capped by settled cash:
# contracts * ceiling * 100 <= ACCOUNT_SIZE.

# --- Target / stop (PLACEHOLDERS — not user rules yet) ---------------------------------------
TARGET_PCT = 0.50                 # target premium = signal premium * (1 + TARGET_PCT)
STOP_PCT = 0.35                   # stop premium   = signal premium * (1 - STOP_PCT)

# --- Entry range rule: floor = signal premium, ceiling = +15–20% ---------------------------
ENTRY_CEILING_PCT = 0.20          # must be between 0.15 and 0.20 per your rule

# --- Files ----------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent     # repository root
WATCHLIST_PATH = BASE_DIR / "watchlist.txt"
IDEA_LOG_PATH = BASE_DIR / "idea_log.csv"             # used only by local CLI runs
HEADLINES_PATH = BASE_DIR / "inputs" / "headlines.txt"  # optional, one headline per line

# --- Timezones ------------------------------------------------------------------------------
HST = ZoneInfo("Pacific/Honolulu")               # Hawaii. No DST.
ET = ZoneInfo("America/New_York")                # Exchange time. Has DST.

# --- Session definition (exchange time) ----------------------------------------------------
RTH_OPEN_ET = time(9, 30)
RTH_CLOSE_ET = time(16, 0)
EARLY_CLOSE_ET = time(13, 0)

# --- Your windows ---------------------------------------------------------------------------
AMATEUR_HOUR_MIN = 30                  # first 30 min after open = amateur hour
INSTITUTIONAL_WINDOW_MIN = (30, 60)    # "4:00–4:30am HST" = open+30 to open+60 (tracks DST)
ACTIVE_WINDOW_END_HST = time(9, 0)     # literal HST, as stated
THURSDAY_REASSESS_HST = time(11, 0)    # literal HST, as stated (note: after the close in summer)
FOMC_DECISION_ET = time(14, 0)         # statement release
FOMC_CLEAR_MIN = 15                    # "decision clears" = statement + 15 min (adjust)

# Scheduled-run guard windows (minutes relative to the open)
PREMARKET_RUN_WINDOW = (-45, -5)
CONFIRM_RUN_WINDOW = (30, 60)

# --- Bias thresholds -----------------------------------------------------------------------
FLAT_BAND_PCT = 0.10       # |change| below this % counts as flat (neither red nor green)
VIX_CONFIRM_PCT = 2.0      # VIX up >= this % confirms bear; down >= this % confirms bull

# --- Filters --------------------------------------------------------------------------------
MIN_AVG_VOLUME = 1_000_000
MIN_OPEN_INTEREST = 500
PREFERRED_MAX_DTE = 7
HARD_MAX_DTE = 14          # 8–14 DTE allowed only with a WIDE-DTE flag; beyond = rejected
MAX_SPREAD_PCT = 10.0      # (ask-bid)/mid * 100
STRONG_OI = 2_000
TIGHT_SPREAD_PCT = 5.0
GAP_CHASE_PCT = 1.0        # gap of >= 1% in the trade's direction = chasing -> reject
MID_RANGE_BAND = (0.35, 0.65)   # position inside prior-day range treated as "mid-range"
NEAR_LEVEL_PCT = 0.30      # within 0.30% of a key level counts as "at the level"
RVOL_CONFIRM = 1.5         # last 5-min volume vs prior 20-bar avg
HIGH_IVP = 60              # earnings 0DTE: IV percentile >= this = stand down (PLACEHOLDER)
A_PLUS_MIN_CONFIDENCE = 8
MAX_CARDS = 5
EXPIRY_PICK = "latest"     # Mon–Thu: "latest" = this week's Friday expiry; "nearest" = soonest after today
RAW_CHAIN_ROWS = 16        # option rows nearest the money saved per ticker per run

# --- Core list (always on). Futures proxies via yfinance. ---------------------------------
CORE_INDEX = ["SPY", "QQQ", "IWM", "DIA"]
FUTURES = {"ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F"}
VIX_SYMBOL = "^VIX"
# Symbols never given option cards
INDICATOR_ONLY = {"VIX", "^VIX"}
SHARES_ONLY = {"RR"}                      # share scalps only — no options
# Funds and indexes have no earnings reports, so the earnings rules don't apply to them.
NO_EARNINGS = {"SPY", "QQQ", "IWM", "DIA", "SPX", "TQQQ", "SMH", "SOXL", "XLF", "XLE", "XLK",
               "GLD", "SLV", "TLT", "IBIT", "TNA", "UVXY"}
YF_SYMBOL_MAP = {"SPX": "^SPX", "VIX": "^VIX"}
CBOE_SYMBOL_MAP = {"SPX": "_SPX"}

# --- Calendars (verify each year) -----------------------------------------------------------
# Source: NYSE Group 2026–2028 holiday & early-close announcement (Business Wire, 2025-12-23)
NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
NYSE_EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
# Source: federalreserve.gov FOMC calendar. Decision day = 2nd day of each meeting.
# 2027 dates are tentative until confirmed at the preceding meeting.
FOMC_DECISION_DAYS = {
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28), date(2027, 6, 9),
    date(2027, 7, 28), date(2027, 9, 15), date(2027, 10, 27), date(2027, 12, 8),
}

HTTP_TIMEOUT = 15
YF_PAUSE_SEC = 0.4         # gentle pacing between tickers


# =============================================================================================
# TIME
# =============================================================================================

def now_hst() -> datetime:
    """The only clock in this script. Aware UTC from the OS, converted to HST."""
    return datetime.now(timezone.utc).astimezone(HST)


@dataclass
class Session:
    trade_date: date
    open_hst: datetime
    close_hst: datetime
    early_close: bool

    def at(self, minutes_after_open: int) -> datetime:
        return self.open_hst + timedelta(minutes=minutes_after_open)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def session_for(d: date) -> Optional[Session]:
    """RTH session for trade date d, expressed in HST. None if the market is closed."""
    if not is_trading_day(d):
        return None
    early = d in NYSE_EARLY_CLOSES
    o = datetime.combine(d, RTH_OPEN_ET, tzinfo=ET).astimezone(HST)
    c = datetime.combine(d, EARLY_CLOSE_ET if early else RTH_CLOSE_ET, tzinfo=ET).astimezone(HST)
    return Session(d, o, c, early)


def next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def is_pre_holiday(d: date) -> bool:
    """Pre-holiday = the next weekday is a market holiday, or today is an early close."""
    if d in NYSE_EARLY_CLOSES:
        return True
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n in NYSE_HOLIDAYS


def fomc_clear_time_hst(d: date) -> Optional[datetime]:
    if d not in FOMC_DECISION_DAYS:
        return None
    t = datetime.combine(d, FOMC_DECISION_ET, tzinfo=ET) + timedelta(minutes=FOMC_CLEAR_MIN)
    return t.astimezone(HST)


def fmt_hst(dt: datetime) -> str:
    return dt.astimezone(HST).strftime("%a %Y-%m-%d %I:%M %p HST")


def check_time() -> None:
    """Prints the current time in HST/UTC/ET and today's session so you can verify."""
    n = now_hst()
    print("=== TIME CHECK ===")
    off_h = n.utcoffset().total_seconds() / 3600
    print(f"HST now : {n.isoformat()}   (UTC offset {off_h:+.0f}h — must be -10h"
          f"{'' if off_h == -10 else '  <-- WRONG, do not trust this run'})")
    print(f"UTC now : {n.astimezone(timezone.utc).isoformat()}")
    et = n.astimezone(ET)
    print(f"ET  now : {et.isoformat()}   (US DST {'ON' if et.dst() else 'OFF'})")
    s = session_for(n.date())
    if s is None:
        print(f"Market CLOSED on {n.date()} (weekend/holiday). Next: {next_trading_day(n.date())}")
        s = session_for(next_trading_day(n.date()))
    print(f"Session : open {fmt_hst(s.open_hst)} | close {fmt_hst(s.close_hst)}"
          f"{' (EARLY CLOSE)' if s.early_close else ''}")
    print(f"Amateur hour      : until {fmt_hst(s.at(AMATEUR_HOUR_MIN))}")
    print(f"Institutional win : {fmt_hst(s.at(INSTITUTIONAL_WINDOW_MIN[0]))} – "
          f"{s.at(INSTITUTIONAL_WINDOW_MIN[1]).strftime('%I:%M %p')}")
    print(f"Pre-market run    : {fmt_hst(s.at(PREMARKET_RUN_WINDOW[0]))} – "
          f"{s.at(PREMARKET_RUN_WINDOW[1]).strftime('%I:%M %p')}")
    print(f"Weekday {s.trade_date.strftime('%A')} | pre-holiday={is_pre_holiday(s.trade_date)}"
          f" | FOMC clear={fomc_clear_time_hst(s.trade_date)}")


# =============================================================================================
# DATA MODELS
# =============================================================================================

@dataclass
class Quote:
    symbol: str
    last: Optional[float] = None
    prev_close: Optional[float] = None
    source: str = "delayed"

    @property
    def chg_pct(self) -> Optional[float]:
        if self.last is None or not self.prev_close:
            return None
        return (self.last - self.prev_close) / self.prev_close * 100


@dataclass
class OptionQuote:
    expiry: date
    right: str          # "C" or "P"
    strike: float
    bid: Optional[float]
    ask: Optional[float]
    oi: Optional[int]
    iv: Optional[float] = None
    delta: Optional[float] = None
    source: str = "yfinance"
    contract: str = ""          # OCC-style symbol, used for grading

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if not m:
            return None
        return (self.ask - self.bid) / m * 100


@dataclass
class Technicals:
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    prev_close: Optional[float] = None
    pm_high: Optional[float] = None
    pm_low: Optional[float] = None
    last: Optional[float] = None
    last_time: Optional[datetime] = None     # timestamp of the bar that gave `last`
    avg_volume: Optional[float] = None
    trend: dict = field(default_factory=dict)        # {"1m": "up"/"down"/"flat", ...}
    vwap: Optional[float] = None
    first5_open: Optional[float] = None
    first5_close: Optional[float] = None
    rvol: Optional[float] = None


@dataclass
class Overrides:
    """ThinkorSwim paste-in values. Anything set here beats delayed data for this run."""
    quotes: dict = field(default_factory=dict)      # SYM -> {"last":..,"prev":..}
    ivp: dict = field(default_factory=dict)         # SYM -> IV percentile
    earnings: dict = field(default_factory=dict)    # SYM -> date
    options: list = field(default_factory=list)     # (SYM, OptionQuote)


@dataclass
class Bias:
    regime: str                 # BEAR / ROTATION / BULL / MIXED / UNKNOWN
    favored: Optional[str]      # "P", "C" or None
    puts_blocked: bool
    vix_confirms: bool
    index_confirms: bool
    notes: list

    @property
    def clean(self) -> bool:
        return self.favored is not None and self.vix_confirms and self.index_confirms


@dataclass
class Idea:
    ticker: str
    type: str                  # CALL / PUT
    bias: str
    expiry: str
    strike: float
    entry_floor: float
    entry_ceiling: float
    premium: float
    max_contracts: str
    target: float
    stop: float
    reasoning: str
    confidence: int
    a_plus: bool = False
    status: str = "CONDITIONAL"
    flags: str = ""
    data_source: str = ""
    contract: str = ""
    underlying: Optional[float] = None    # stock price when the card was made
    underlying_asof: str = ""             # time of that price (HST) or "ToS"

    def price_tag(self) -> str:
        if self.underlying is None:
            return ""
        return f" @ ${self.underlying:,.2f} ({self.underlying_asof})"

    def card(self) -> str:
        return (f"{self.ticker}{self.price_tag()} | {self.type} | {self.bias} | {self.expiry} | {self.strike:g} | "
                f"${self.entry_floor:.2f}–${self.entry_ceiling:.2f} | ${self.premium:.2f} | "
                f"{self.max_contracts} | ${self.target:.2f} | ${self.stop:.2f} | "
                f"{self.reasoning} | {self.confidence}/10"
                + (f"\n   status: {self.status}" + (f" | flags: {self.flags}" if self.flags else "")))


# =============================================================================================
# TOS OVERRIDE PARSER
# =============================================================================================
# Text format, one item per line ('#' = comment). Prices from your ToS screen.
#   ES last=6712.25 prev=6740.00          futures / index / stock quote
#   VIX last=18.40 prev=17.10
#   NVDA ivp=72                           IV percentile (from ToS)
#   NVDA earnings=2026-10-22              earnings date
#   SPY 2026-10-19 P 670 bid=1.10 ask=1.14 oi=12000 iv=0.18 delta=-0.42
# Or a JSON file with the same keys: {"quotes":{"ES":{"last":..,"prev":..}}, "ivp":{..},
#   "earnings":{"NVDA":"2026-10-22"}, "options":[{"symbol":"SPY","expiry":"2026-10-19",
#   "right":"P","strike":670,"bid":1.1,"ask":1.14,"oi":12000}]}

_OPT_LINE = re.compile(r"^(\S+)\s+(\d{4}-\d{2}-\d{2})\s+([CP])\s+([\d.]+)\s*(.*)$", re.I)


def _kv(text: str) -> dict:
    return {k.lower(): v for k, v in re.findall(r"(\w+)=([^\s]+)", text)}


def parse_overrides(text: str) -> Overrides:
    ov = Overrides()
    text = text.strip()
    if not text:
        return ov
    if text.startswith("{"):
        j = json.loads(text)
        for s, q in j.get("quotes", {}).items():
            ov.quotes[s.upper()] = {"last": q.get("last"), "prev": q.get("prev")}
        ov.ivp = {k.upper(): float(v) for k, v in j.get("ivp", {}).items()}
        ov.earnings = {k.upper(): date.fromisoformat(v) for k, v in j.get("earnings", {}).items()}
        for o in j.get("options", []):
            ov.options.append((o["symbol"].upper(), OptionQuote(
                date.fromisoformat(o["expiry"]), o["right"].upper(), float(o["strike"]),
                o.get("bid"), o.get("ask"), o.get("oi"), o.get("iv"), o.get("delta"), "ToS")))
        return ov
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _OPT_LINE.match(line)
        if m:
            sym, exp, right, strike, rest = m.groups()
            kv = _kv(rest)
            f = lambda k: float(kv[k]) if k in kv else None
            ov.options.append((sym.upper(), OptionQuote(
                date.fromisoformat(exp), right.upper(), float(strike), f("bid"), f("ask"),
                int(float(kv["oi"])) if "oi" in kv else None, f("iv"), f("delta"), "ToS")))
            continue
        sym, _, rest = line.partition(" ")
        kv = _kv(rest)
        sym = sym.upper()
        if "last" in kv or "prev" in kv:
            ov.quotes[sym] = {"last": float(kv["last"]) if "last" in kv else None,
                              "prev": float(kv["prev"]) if "prev" in kv else None}
        if "ivp" in kv:
            ov.ivp[sym] = float(kv["ivp"])
        if "earnings" in kv:
            ov.earnings[sym] = date.fromisoformat(kv["earnings"])
    return ov


# =============================================================================================
# DATA FETCH (free/delayed). Every function returns None/empty on failure — never raises.
# =============================================================================================

def _yf():
    import yfinance as yf  # imported lazily so --check-time and --selftest work without it
    return yf


def fetch_quote(symbol: str, yf_symbol: Optional[str] = None) -> Quote:
    """Last price (incl. pre-market / overnight futures) vs prior regular-session close."""
    q = Quote(symbol)
    try:
        t = _yf().Ticker(yf_symbol or symbol)
        daily = t.history(period="7d", interval="1d", auto_adjust=False)
        intr = t.history(period="2d", interval="5m", prepost=True, auto_adjust=False)
        if daily is None or daily.empty:
            return q
        today_et = now_hst().astimezone(ET).date()
        dd = daily.copy()
        dd.index = [i.date() for i in dd.index.tz_convert(ET)] if dd.index.tz is not None \
            else [i.date() for i in dd.index]
        prior = dd[[d < today_et for d in dd.index]]
        q.prev_close = float(prior["Close"].iloc[-1]) if not prior.empty else None
        if intr is not None and not intr.empty:
            q.last = float(intr["Close"].dropna().iloc[-1])
        else:
            q.last = float(daily["Close"].iloc[-1])
    except Exception as e:  # noqa: BLE001
        print(f"[warn] quote {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
    return q


def _ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def _trend(df) -> str:
    """down: close < EMA9 < EMA21 ; up: close > EMA9 > EMA21 ; else flat."""
    if df is None or len(df) < 25:
        return "flat"
    c = df["Close"]
    e9, e21 = _ema(c, 9).iloc[-1], _ema(c, 21).iloc[-1]
    last = c.iloc[-1]
    if last < e9 < e21:
        return "down"
    if last > e9 > e21:
        return "up"
    return "flat"


def fetch_technicals(symbol: str, session: Session, mode: str) -> Technicals:
    tech = Technicals()
    try:
        import pandas as pd
        t = _yf().Ticker(YF_SYMBOL_MAP.get(symbol, symbol))
        daily = t.history(period="3mo", interval="1d", auto_adjust=False)
        if daily is None or daily.empty:
            return tech
        daily.index = daily.index.tz_convert(ET) if daily.index.tz is not None else daily.index
        d_dates = [i.date() for i in daily.index]
        prior = daily[[d < session.trade_date for d in d_dates]]
        if prior.empty:
            return tech
        tech.prev_high = float(prior["High"].iloc[-1])
        tech.prev_low = float(prior["Low"].iloc[-1])
        tech.prev_close = float(prior["Close"].iloc[-1])
        tech.avg_volume = float(prior["Volume"].tail(50).mean())

        m1 = t.history(period="5d", interval="1m", prepost=True, auto_adjust=False)
        if m1 is None or m1.empty:
            return tech
        m1 = m1.tz_convert(ET)
        tech.last = float(m1["Close"].iloc[-1])
        tech.last_time = m1.index[-1].to_pydatetime().astimezone(HST)
        open_et = session.open_hst.astimezone(ET)
        today = m1[m1.index.date == session.trade_date]
        pm = today[today.index < open_et]
        if not pm.empty:
            tech.pm_high, tech.pm_low = float(pm["High"].max()), float(pm["Low"].min())

        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        m5 = m1.resample("5min").agg(agg).dropna()
        m15 = m1.resample("15min").agg(agg).dropna()
        tech.trend = {"5m": _trend(m5), "15m": _trend(m15)}

        if mode == "confirm":
            rth = today[today.index >= open_et]
            tech.trend["1m"] = _trend(rth if len(rth) >= 25 else m1)
            if not rth.empty and rth["Volume"].sum() > 0:
                tp = (rth["High"] + rth["Low"] + rth["Close"]) / 3
                tech.vwap = float((tp * rth["Volume"]).sum() / rth["Volume"].sum())
            f5 = rth[rth.index < open_et + timedelta(minutes=5)]
            if len(f5) >= 1:
                tech.first5_open = float(f5["Open"].iloc[0])
                tech.first5_close = float(f5["Close"].iloc[-1])
            r5 = rth.resample("5min").agg(agg).dropna()
            if len(r5) >= 3:
                base = r5["Volume"].iloc[:-1].tail(20).mean()
                tech.rvol = float(r5["Volume"].iloc[-1] / base) if base else None
        _ = pd  # keep import referenced
    except Exception as e:  # noqa: BLE001
        print(f"[warn] technicals {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
    return tech


_OCC = re.compile(r"(\d{6})([CP])(\d{8})$")


def fetch_cboe_chain(symbol: str) -> list:
    """Cboe delayed CDN. Field names are parsed defensively; returns [] if the shape differs."""
    import requests
    sym = CBOE_SYMBOL_MAP.get(symbol, symbol)
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    out = []
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        rows = (r.json().get("data") or {}).get("options") or []
        for row in rows:
            m = _OCC.search(str(row.get("option", "")))
            if not m:
                continue
            ymd, right, k = m.groups()
            num = lambda key: (float(row[key]) if row.get(key) not in (None, "") else None)
            oi = row.get("open_interest")
            out.append(OptionQuote(datetime.strptime(ymd, "%y%m%d").date(), right,
                                   int(k) / 1000, num("bid"), num("ask"),
                                   int(oi) if oi not in (None, "") else None,
                                   num("iv"), num("delta"), "cboe",
                                   str(row.get("option", "")).lstrip("_")))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cboe {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    return out


def fetch_yf_chain(symbol: str, expiry: date) -> list:
    out = []
    try:
        t = _yf().Ticker(YF_SYMBOL_MAP.get(symbol, symbol))
        ch = t.option_chain(expiry.isoformat())
        for right, df in (("C", ch.calls), ("P", ch.puts)):
            for _, r in df.iterrows():
                f = lambda k: (None if r.get(k) is None or (isinstance(r.get(k), float)
                                                          and math.isnan(r.get(k))) else r.get(k))
                out.append(OptionQuote(expiry, right, float(r["strike"]),
                                       f("bid"), f("ask"),
                                       int(f("openInterest")) if f("openInterest") is not None else None,
                                       f("impliedVolatility"), None, "yfinance",
                                       str(r.get("contractSymbol") or "")))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] yf chain {symbol} {expiry}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def fetch_expiries(symbol: str) -> list:
    try:
        return sorted(date.fromisoformat(s)
                      for s in _yf().Ticker(YF_SYMBOL_MAP.get(symbol, symbol)).options)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] expiries {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def fetch_earnings_date(symbol: str) -> Optional[date]:
    """yfinance calendar. Returns None when unknown (unknown is treated as a flag, not a pass)."""
    try:
        cal = _yf().Ticker(symbol).calendar
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, date):
                return v
    except Exception as e:  # noqa: BLE001
        print(f"[warn] earnings {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
    return None


def load_headlines() -> list:
    if HEADLINES_PATH.exists():
        return [l.strip() for l in HEADLINES_PATH.read_text().splitlines() if l.strip()][:8]
    return []


def load_watchlist(path: Path = WATCHLIST_PATH) -> list:
    """One ticker per line, '#' comments. Core index ETFs are always included."""
    names = []
    if path.exists():
        for line in path.read_text().splitlines():
            s = line.split("#", 1)[0].strip().upper()
            if s and s not in names:
                names.append(s)
    for c in CORE_INDEX:
        if c not in names:
            names.insert(0, c)
    return names


# =============================================================================================
# RULES ENGINE (pure functions — no network; covered by --selftest)
# =============================================================================================

def _sign(chg: Optional[float]) -> str:
    if chg is None:
        return "unknown"
    if chg <= -FLAT_BAND_PCT:
        return "red"
    if chg >= FLAT_BAND_PCT:
        return "green"
    return "flat"


def compute_bias(es: Quote, nq: Quote, ym: Quote, vix: Quote, spy: Quote, qqq: Quote) -> Bias:
    """
    Your ES/YM rules, in order:
      1. ES red  AND YM red                  → BEAR, puts favored
      2. YM green AND (NQ red OR ES red)     → ROTATION, puts allowed, may fade midday
      3. ES green AND YM green               → BULL, NO put setups (hard block)
      else                                   → MIXED / UNKNOWN, no favored side (no A+)
    Confirmations (needed for a "clean" match / A+):
      VIX: BEAR/ROTATION need VIX up >= VIX_CONFIRM_PCT; BULL needs VIX down >= VIX_CONFIRM_PCT.
      SPY & QQQ overnight: both must be the same color as the favored direction.
    """
    s_es, s_nq, s_ym = _sign(es.chg_pct), _sign(nq.chg_pct), _sign(ym.chg_pct)
    s_vix, s_spy, s_qqq = vix.chg_pct, _sign(spy.chg_pct), _sign(qqq.chg_pct)
    notes = [f"ES {s_es} ({_pct(es.chg_pct)}), NQ {s_nq} ({_pct(nq.chg_pct)}), "
             f"YM {s_ym} ({_pct(ym.chg_pct)})"]
    if s_es == "red" and s_ym == "red":
        regime, fav, block = "BEAR", "P", False
        notes.append("ES+YM red → broad bear, puts favored")
    elif s_ym == "green" and (s_nq == "red" or s_es == "red"):
        regime, fav, block = "ROTATION", "P", False
        notes.append("YM green + NQ/ES red → rotation; puts may fade midday")
    elif s_es == "green" and s_ym == "green":
        regime, fav, block = "BULL", "C", True
        notes.append("ES+YM green → NO put setups")
    elif "unknown" in (s_es, s_ym):
        regime, fav, block = "UNKNOWN", None, False
        notes.append("Futures data missing → no bias, no A+")
    else:
        regime, fav, block = "MIXED", None, False
        notes.append("No ES/YM rule matched → no favored side, no A+")

    vix_ok = False
    if s_vix is not None and fav == "P":
        vix_ok = s_vix >= VIX_CONFIRM_PCT
    elif s_vix is not None and fav == "C":
        vix_ok = s_vix <= -VIX_CONFIRM_PCT
    notes.append(f"VIX {'n/a' if vix.last is None else f'{vix.last:.2f}'} ({_pct(s_vix)}) "
                 f"→ {'confirms' if vix_ok else 'does not confirm'}")

    want = {"P": "red", "C": "green"}.get(fav or "", None)
    idx_ok = want is not None and s_spy == want and s_qqq == want
    notes.append(f"SPY {s_spy} ({_pct(spy.chg_pct)}), QQQ {s_qqq} ({_pct(qqq.chg_pct)}) "
                 f"→ {'confirms' if idx_ok else 'does not confirm'}")
    return Bias(regime, fav, block, vix_ok, idx_ok, notes)


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.2f}%"


def day_rules(d: date) -> dict:
    """Weekday-driven rules. Pre-holiday days get Friday rules."""
    friday_rules = d.weekday() == 4 or is_pre_holiday(d)
    return {
        "zero_dte_only": friday_rules,
        "no_overnight": friday_rules,
        "thursday_reassess": d.weekday() == 3,
        "fomc_day": d in FOMC_DECISION_DAYS,
        "pre_holiday": is_pre_holiday(d),
    }


def hold_limit_day(d: date) -> date:
    """Last trading day before the market next closes (weekend OR holiday). Normally Friday;
    Thursday if Friday is a holiday; the day before a mid-week holiday."""
    last = d
    while is_trading_day(last + timedelta(days=1)):
        last += timedelta(days=1)
    return last


def pick_expiry(expiries: list, today: date, rules: dict) -> tuple:
    """
    Your holding rule:
      Friday / pre-holiday  -> 0DTE only (bought and closed the same day).
      Mon–Thu               -> NO 0DTE. Expiry between tomorrow and the last trading day
                               before the market next closes (weekend or holiday), so a
                               position can be held overnight but never over a weekend/holiday. EXPIRY_PICK chooses which one (default: the latest,
                               i.e. Friday — the most time for the move).
    Returns (expiry, flag) or (None, reason).
    """
    valid = [e for e in expiries if e >= today]
    if rules["zero_dte_only"]:
        return (today, "") if today in valid else (None, "no 0DTE expiry (Friday/pre-holiday rule)")
    last = hold_limit_day(today)
    week = [e for e in valid if today < e <= last]
    if not week:
        return None, f"no expiry between tomorrow and {last.strftime('%a %-m/%-d')}"
    return (max(week) if EXPIRY_PICK == "latest" else min(week)), ""


def pick_strike(chain: list, expiry: date, right: str, spot: float) -> Optional[OptionQuote]:
    """First out-of-the-money strike (nearest to spot on the OTM side)."""
    side = [o for o in chain if o.expiry == expiry and o.right == right]
    if right == "C":
        otm = sorted([o for o in side if o.strike >= spot], key=lambda o: o.strike)
    else:
        otm = sorted([o for o in side if o.strike <= spot], key=lambda o: -o.strike)
    return otm[0] if otm else None


def liquidity_check(opt: OptionQuote) -> tuple:
    """(ok, reason). Rejects OI < 500, no bid, or spread wider than MAX_SPREAD_PCT."""
    if opt.oi is None or opt.oi < MIN_OPEN_INTEREST:
        return False, f"OI {opt.oi} < {MIN_OPEN_INTEREST}"
    if not opt.bid or opt.bid <= 0 or opt.mid is None:
        return False, "no bid"
    if opt.spread_pct is None or opt.spread_pct > MAX_SPREAD_PCT:
        return False, f"spread {opt.spread_pct:.1f}% > {MAX_SPREAD_PCT}%"
    return True, ""


def gap_chase(tech: Technicals, right: str) -> bool:
    """Gap of >= GAP_CHASE_PCT already in the trade's direction = chasing."""
    if tech.last is None or not tech.prev_close:
        return False
    g = (tech.last - tech.prev_close) / tech.prev_close * 100
    return (right == "P" and g <= -GAP_CHASE_PCT) or (right == "C" and g >= GAP_CHASE_PCT)


def key_levels(tech: Technicals) -> dict:
    lv = {"PDH": tech.prev_high, "PDL": tech.prev_low, "PMH": tech.pm_high,
          "PML": tech.pm_low, "VWAP": tech.vwap}
    return {k: v for k, v in lv.items() if v}


def near_level(tech: Technicals) -> Optional[str]:
    if tech.last is None:
        return None
    for name, lvl in key_levels(tech).items():
        if abs(tech.last - lvl) / lvl * 100 <= NEAR_LEVEL_PCT:
            return name
    return None


def level_read(tech: Technicals, right: str) -> tuple:
    """
    Direction-aware levels within NEAR_LEVEL_PCT of price. Returns (good, bad):
      good = a level that backs the trade: for a CALL, a level just BELOW price (support:
             reclaimed VWAP, PDL/PML held, or PDH/PMH already broken); for a PUT, a level just
             ABOVE price (resistance).
      bad  = a level the trade runs straight into: a CALL just below a level overhead
             (resistance), a PUT just above a level underneath (support).
    """
    good = bad = None
    if tech.last is None:
        return None, None
    for name, lvl in key_levels(tech).items():
        if abs(tech.last - lvl) / lvl * 100 > NEAR_LEVEL_PCT:
            continue
        below = lvl <= tech.last
        if (right == "C") == below:
            good = good or name
        else:
            bad = bad or name
    return good, bad


def mid_range(tech: Technicals) -> bool:
    """Inside the middle band of the prior-day range AND not at any key level."""
    if None in (tech.last, tech.prev_high, tech.prev_low) or tech.prev_high <= tech.prev_low:
        return False
    pos = (tech.last - tech.prev_low) / (tech.prev_high - tech.prev_low)
    return MID_RANGE_BAND[0] < pos < MID_RANGE_BAND[1] and near_level(tech) is None


def trend_aligned(tech: Technicals, right: str, mode: str) -> bool:
    want = "down" if right == "P" else "up"
    frames = ["1m", "5m", "15m"] if mode == "confirm" else ["5m", "15m"]
    return all(tech.trend.get(f) == want for f in frames)


def first5_confirms(tech: Technicals, right: str) -> Optional[bool]:
    """First 5-min candle color must match the trade (red for puts, green for calls)."""
    if tech.first5_open is None or tech.first5_close is None:
        return None
    return tech.first5_close < tech.first5_open if right == "P" else tech.first5_close > tech.first5_open


def occ_symbol(sym: str, opt: OptionQuote) -> str:
    """OCC-style contract symbol, e.g. SPY261019P00670000 (Yahoo's format for equity options)."""
    return f"{sym}{opt.expiry.strftime('%y%m%d')}{opt.right}{int(round(opt.strike * 1000)):08d}"


def max_contracts(entry_ceiling: float, stop: float) -> str:
    """Placeholder sizing. See RISK_METHOD in CONFIG."""
    if not ACCOUNT_SIZE:
        return "UNSET"
    if RISK_METHOD == "percent" and RISK_PCT:
        risk_dollars = ACCOUNT_SIZE * RISK_PCT
    elif RISK_METHOD == "fixed_dollar" and RISK_FIXED_DOLLARS:
        risk_dollars = RISK_FIXED_DOLLARS
    else:
        return "UNSET"
    per = max((entry_ceiling - stop) * 100, 0.01)
    by_risk = math.floor(risk_dollars / per)
    by_cash = math.floor(ACCOUNT_SIZE / (entry_ceiling * 100))   # settled-cash cap
    return str(max(min(by_risk, by_cash), 0))


def earnings_check(sym: str, earn: Optional[date], expiry: date, today: date,
                   ivp: Optional[float]) -> tuple:
    """
    (hard_reject_reason or "", flag or "")
    * Earnings after today and on/before expiry -> reject (no overnight earnings holds).
    * Earnings today on a 0DTE -> need IV percentile (paste from ToS). Missing or >= HIGH_IVP
      -> stand down.
    * Earnings date unknown -> flag "EARNINGS?" (blocks A+, not the card).
    """
    if earn is None:
        return "", "EARNINGS?"
    if today < earn <= expiry:
        return "earnings before expiry (no overnight earnings hold)", ""
    if earn == today and expiry == today:
        if ivp is None:
            return "earnings 0DTE: IV percentile missing -> stand down (paste ivp from ToS)", ""
        if ivp >= HIGH_IVP:
            return f"earnings 0DTE: IVP {ivp:.0f} >= {HIGH_IVP} -> stand down", ""
        return "", f"EARNINGS-0DTE IVP {ivp:.0f}"
    if earn == today and expiry > today:
        return "earnings today, expiry later (no overnight earnings hold)", ""
    return "", ""


def evaluate(sym: str, right: str, bias: Bias, tech: Technicals, opt: Optional[OptionQuote],
             expiry_flag: str, earn_result: tuple, rules: dict, mode: str,
             now: datetime, session: Session) -> tuple:
    """Returns (Idea or None, list_of_reject_reasons). Pure function."""
    rejects = []
    if right == "P" and bias.puts_blocked:
        rejects.append("ES+YM green: no put setups")
    if tech.avg_volume is None or tech.avg_volume < MIN_AVG_VOLUME:
        rejects.append(f"avg vol {tech.avg_volume or 0:,.0f} < {MIN_AVG_VOLUME:,}")
    if gap_chase(tech, right):
        rejects.append("gap chase")
    if mid_range(tech):
        rejects.append("mid-range")
    if earn_result[0]:
        rejects.append(earn_result[0])
    if opt is None:
        rejects.append("no contract")
    else:
        ok, why = liquidity_check(opt)
        if not ok:
            rejects.append(why)
    f5 = first5_confirms(tech, right) if mode == "confirm" else None
    if mode == "confirm" and f5 is False:
        rejects.append("first 5-min candle does not confirm")
    # After the open, every GO needs 1m/5m/15m all pointing the trade's way (A+ trend rule
    # applied to every card, so a card can't score its way in without the trend).
    if mode == "confirm" and tech.trend and not trend_aligned(tech, right, mode):
        tf = "/".join(f"{k} {tech.trend.get(k, '?')}" for k in ("1m", "5m", "15m"))
        rejects.append(f"trend not aligned ({tf})")
    good_lvl, bad_lvl = level_read(tech, right)
    if bad_lvl:
        rejects.append(f"{'call into resistance' if right == 'C' else 'put into support'} ({bad_lvl})")
    if rejects:
        return None, rejects

    # --- Scoring (deterministic) ---
    lvl = good_lvl
    aligned = trend_aligned(tech, right, mode)
    favored_side = bias.favored == right
    clean = bias.clean and favored_side
    vol_ok = tech.rvol is not None and tech.rvol >= RVOL_CONFIRM
    inst = session.at(INSTITUTIONAL_WINDOW_MIN[0]) <= now <= session.at(INSTITUTIONAL_WINDOW_MIN[1])
    strong_liq = opt.oi >= STRONG_OI and opt.spread_pct <= TIGHT_SPREAD_PCT
    score = 5
    reasons = [f"{bias.regime}"]
    if clean:
        score += 1; reasons.append("bias clean")
    elif not favored_side:
        score -= 1; reasons.append("against/no favored side")
    if aligned:
        score += 1; reasons.append("trend aligned")
    if lvl:
        score += 1; reasons.append(f"{lvl} {'support' if right == 'C' else 'resistance'}")
    if vol_ok:
        score += 1; reasons.append(f"RVOL {tech.rvol:.1f}")
    if inst and mode == "confirm":
        score += 1; reasons.append("institutional window")
    if strong_liq:
        score += 1; reasons.append("strong liquidity")
    if expiry_flag:
        score -= 1
    if bias.regime == "ROTATION" and right == "P":
        reasons.append("exit before midday fade")
    score = max(1, min(10, score))

    flags = [f for f in (expiry_flag, earn_result[1]) if f]
    if rules["fomc_day"]:
        clr = fomc_clear_time_hst(session.trade_date)
        flags.append(f"FOMC: no entry before {clr.strftime('%I:%M %p HST')}")
    if rules["no_overnight"]:
        flags.append("0DTE only / no overnight")
    elif opt is not None and opt.expiry > session.trade_date:
        flags.append(f"hold OK until {opt.expiry.strftime('%a %-m/%-d')} close · close before the weekend")
    if now < session.at(AMATEUR_HOUR_MIN):
        flags.append(f"amateur hour until {session.at(AMATEUR_HOUR_MIN).strftime('%I:%M %p')}")

    premium = round(opt.mid, 2)
    ceiling = round(premium * (1 + ENTRY_CEILING_PCT), 2)
    target = round(premium * (1 + TARGET_PCT), 2)
    stop = round(premium * (1 - STOP_PCT), 2)
    realistic = opt.ask is not None and opt.ask <= ceiling

    fomc_blocking = rules["fomc_day"] and now < fomc_clear_time_hst(session.trade_date)
    a_plus = (mode == "confirm" and clean and strong_liq and aligned and lvl is not None
              and vol_ok and f5 is True and score >= A_PLUS_MIN_CONFIDENCE and realistic
              and not fomc_blocking and "EARNINGS?" not in flags
              and not any(f.startswith("WIDE-DTE") for f in flags))

    if mode == "premarket":
        status = "CONDITIONAL — needs first 5-min candle confirmation"
    elif fomc_blocking:
        status = "WAIT — FOMC"
    elif now > datetime.combine(session.trade_date, ACTIVE_WINDOW_END_HST, tzinfo=HST):
        status = "PAST ACTIVE WINDOW"
    else:
        status = "GO" if realistic else "WAIT — ask above entry ceiling"

    idea = Idea(sym, "PUT" if right == "P" else "CALL", bias.regime, opt.expiry.isoformat(),
                opt.strike, premium, ceiling, premium, max_contracts(ceiling, stop), target, stop,
                ", ".join(reasons), score, a_plus, status, "; ".join(flags), opt.source,
                opt.contract or occ_symbol(sym, opt), round(tech.last, 2),
                tech.last_time.strftime("%I:%M %p HST").lstrip("0") if tech.last_time else "ToS")
    return idea, []


# =============================================================================================
# TELEGRAM
# =============================================================================================

def _chunks(text: str, limit: int = 3900) -> list:
    """Split on blank lines so a card (and its HTML tags) is never cut in half."""
    out, cur = [], ""
    for block in text.split("\n\n"):
        add = block if not cur else cur + "\n\n" + block
        if len(add) <= limit:
            cur = add
        else:
            if cur:
                out.append(cur)
            cur = block[:limit]
    return out + ([cur] if cur else [])


def send_telegram(text: str, dry_run: bool = False, html: bool = False) -> bool:
    """html=True uses Telegram's HTML formatting (<b>, <i>, <code>). If Telegram rejects the
    formatting, the same message is re-sent as plain text so nothing is lost."""
    if dry_run or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        if not dry_run:
            print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead",
                  file=sys.stderr)
        print("\n" + (re.sub(r"</?[bic](?:ode)?>", "", text) if html else text) + "\n")
        return dry_run
    import html as _html
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok_all = True
    for chunk in _chunks(text):
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "disable_web_page_preview": "true"}
        if html:
            data["parse_mode"] = "HTML"
        for attempt in range(5):
            try:
                r = requests.post(url, data=data, timeout=HTTP_TIMEOUT)
                if r.status_code == 200:
                    break
                if r.status_code == 429:
                    wait = (r.json().get("parameters") or {}).get("retry_after", 5)
                    _time.sleep(int(wait) + 1)
                    continue
                if r.status_code == 400 and data.get("parse_mode"):
                    data.pop("parse_mode")
                    data["text"] = _html.unescape(re.sub(r"</?[bic](?:ode)?>", "", chunk))
                    continue
                print(f"[warn] telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] telegram: {type(e).__name__}: {e}", file=sys.stderr)
            _time.sleep(2 ** attempt)
        else:
            ok_all = False
        _time.sleep(1.1)   # stay under per-chat rate limits
    return ok_all


# --- Telegram layout -------------------------------------------------------------------------

def _h(x) -> str:
    import html as _html
    return _html.escape(str(x), quote=False)


def _short_t(dt: datetime) -> str:
    return dt.astimezone(HST).strftime("%I:%M %p").lstrip("0")


def tg_card(i: "Idea") -> str:
    """One trade card as a short block. Same fields as the pipe format, one idea per line."""
    exp = datetime.strptime(i.expiry, "%Y-%m-%d").strftime("%a %-m/%-d")
    head = f"<b>{_h(i.ticker)} {_h(i.type)} {i.strike:g}</b> · exp {exp} · <b>{i.confidence}/10</b>"
    if i.a_plus:
        head += " · <b>A+</b>"
    reasons = ", ".join(x for x in i.reasoning.split(", ") if x != i.bias)
    lines = [
        head,
        (f"Stock ${i.underlying:,.2f} ({_h(i.underlying_asof)})" if i.underlying is not None
         else "Stock price n/a"),
        f"Entry ${i.entry_floor:.2f}–${i.entry_ceiling:.2f} · Premium ${i.premium:.2f}",
        f"Target ${i.target:.2f} · Stop ${i.stop:.2f} · Max {_h(i.max_contracts)}",
        f"Bias {_h(i.bias)} · {_h(reasons)}",
        f"<i>{_h(i.status)}</i>",
    ]
    if i.flags:
        lines.append(f"<i>{_h(i.flags)}</i>")
    return "\n".join(lines)


def tg_bias(r: "ScanResult") -> str:
    b, q = r.bias, r.quotes
    fav = {"P": "puts favored", "C": "calls favored"}.get(b.favored, "no favored side")

    def c(k):
        x = q.get(k)
        if x is None or x.chg_pct is None:
            return "n/a"
        return f"{x.chg_pct:+.2f}%" + (" flat" if k in ("ES", "NQ", "YM") and _sign(x.chg_pct) == "flat" else "")
    vix = q.get("VIX")
    vix_px = "n/a" if vix is None or vix.last is None else f"{vix.last:.2f}"
    rule = b.notes[1] if len(b.notes) > 1 else ""
    return (f"<b>BIAS: {_h(b.regime)}</b> · {fav} · {'clean' if b.clean else 'not clean'}\n"
            f"ES {c('ES')} · NQ {c('NQ')} · YM {c('YM')}\n"
            f"VIX {vix_px} ({c('VIX')}) · SPY {c('SPY')} · QQQ {c('QQQ')}\n"
            f"{_h(rule)}\n"
            f"VIX {'confirms' if b.vix_confirms else 'does not confirm'} · "
            f"SPY/QQQ {'confirm' if b.index_confirms else 'do not confirm'}")


def tg_report(r: "ScanResult", cards: Optional[list] = None, prefix: str = "") -> str:
    s = r.session
    cards = r.top if cards is None else cards
    title = {"premarket": "PRE-MARKET", "confirm": "CONFIRMATION", "rescan": "RE-SCAN"}.get(r.mode, r.mode.upper())
    parts = [(f"<b>{_h(prefix)}</b>\n" if prefix else "")
             + f"<b>{title}</b> · {r.ran_at.strftime('%a %-m/%-d')} {_short_t(r.ran_at)} HST\n"
             f"Open {_short_t(s.open_hst)} · Close {_short_t(s.close_hst)} HST"
             + (" (early close)" if s.early_close else ""),
             tg_bias(r)]
    flags = [f for f in r.flags if not f.startswith(("Amateur hour", "No damaged"))]
    if flags:
        parts.append("<b>FLAGS</b>\n" + "\n".join(f"• {_h(f)}" for f in flags))
    if r.headlines:
        parts.append("<b>HEADLINES</b>\n" + "\n".join(f"• {_h(h)}" for h in r.headlines))
    parts += [tg_card(i) for i in cards] if cards else ["<b>No quality setups.</b>"]
    foot = f"{len(r.ideas)} passed · {len(r.skipped)} rejected · data ~15 min delayed"
    if "UNSET" in {i.max_contracts for i in cards}:
        foot += "\nMax contracts UNSET — add ACCOUNT_SIZE + RISK_PCT secrets"
    parts.append(f"<i>{foot}</i>")
    return "\n\n".join(parts)


# =============================================================================================
# LOGGING
# =============================================================================================

LOG_FIELDS = ["timestamp_hst", "mode"] + list(Idea.__dataclass_fields__.keys())


def log_ideas(ideas: list, mode: str, path: Optional[Path] = None) -> None:
    path = path or IDEA_LOG_PATH
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        ts = now_hst().isoformat()
        for i in ideas:
            w.writerow({"timestamp_hst": ts, "mode": mode, **asdict(i)})


# =============================================================================================
# RUN
# =============================================================================================

def in_run_window(session: Session, mode: str, now: datetime) -> bool:
    lo, hi = PREMARKET_RUN_WINDOW if mode == "premarket" else CONFIRM_RUN_WINDOW
    return session.at(lo) <= now <= session.at(hi)


def apply_quote_override(q: Quote, ov: Overrides, key: str) -> Quote:
    o = ov.quotes.get(key)
    if o:
        q.last = o["last"] if o.get("last") is not None else q.last
        q.prev_close = o["prev"] if o.get("prev") is not None else q.prev_close
        q.source = "ToS"
    return q


@dataclass
class ScanResult:
    mode: str
    ran_at: datetime
    session: Session
    rules: dict
    bias: Bias
    ideas: list          # every idea that passed the hard filters, best first
    skipped: list        # "SYM side: reasons"
    flags: list
    quotes: dict         # name -> Quote, for the report
    headlines: list
    raw: dict = field(default_factory=dict)   # per-ticker inputs, saved for backtesting

    @property
    def top(self) -> list:
        return self.ideas[:MAX_CARDS]


def day_flags(session: Session, rules: dict) -> list:
    flags = []
    if rules["fomc_day"]:
        flags.append(f"FOMC DAY — no entry before "
                     f"{fomc_clear_time_hst(session.trade_date).strftime('%I:%M %p HST')}")
    if rules["zero_dte_only"]:
        flags.append("Friday/pre-holiday rules: 0DTE only, no overnight")
    if rules["thursday_reassess"]:
        flags.append(f"THURSDAY: reassess all open contracts by "
                     f"{THURSDAY_REASSESS_HST.strftime('%I:%M %p')} HST")
    flags.append(f"Amateur hour until {session.at(AMATEUR_HOUR_MIN).strftime('%I:%M %p')}; "
                 f"institutional {session.at(INSTITUTIONAL_WINDOW_MIN[0]).strftime('%I:%M')}–"
                 f"{session.at(INSTITUTIONAL_WINDOW_MIN[1]).strftime('%I:%M %p')} HST")
    flags.append("No damaged contracts overnight. No earnings holds overnight.")
    return flags


def scan(mode: str, session: Session, ov: Overrides, now: Optional[datetime] = None) -> ScanResult:
    """Fetch data, apply every rule, return all passing ideas. No sending, no writing."""
    now = now or now_hst()
    rules = day_rules(session.trade_date)

    # --- Bias inputs ---
    q = {k: apply_quote_override(fetch_quote(k, v), ov, k) for k, v in FUTURES.items()}
    q["VIX"] = apply_quote_override(fetch_quote("VIX", VIX_SYMBOL), ov, "VIX")
    q["SPY"] = apply_quote_override(fetch_quote("SPY"), ov, "SPY")
    q["QQQ"] = apply_quote_override(fetch_quote("QQQ"), ov, "QQQ")
    bias = compute_bias(q["ES"], q["NQ"], q["YM"], q["VIX"], q["SPY"], q["QQQ"])

    # --- Scan ---
    ideas, skipped, raw = [], [], {}
    for sym in load_watchlist():
        if sym in INDICATOR_ONLY or sym in SHARES_ONLY:
            continue
        tech = fetch_technicals(sym, session, mode)
        if sym in ov.quotes and ov.quotes[sym].get("last") is not None:
            tech.last, tech.last_time = ov.quotes[sym]["last"], None   # shown as "ToS"
        if tech.last is None:
            skipped.append(f"{sym}: no data"); continue

        sides = [bias.favored] if bias.favored else ["C", "P"]
        expiries = fetch_expiries(sym)
        expiry, eflag = pick_expiry(expiries, session.trade_date, rules)
        if expiry is None:
            skipped.append(f"{sym}: {eflag}"); continue
        chain = [o for s, o in ov.options if s == sym]
        if not any(o.expiry == expiry for o in chain):
            chain = fetch_cboe_chain(sym) or []
            if not any(o.expiry == expiry for o in chain):
                chain = fetch_yf_chain(sym, expiry)
        earn = ov.earnings.get(sym) or (None if sym in NO_EARNINGS
                                        else fetch_earnings_date(sym))
        earn_res = ("", "") if sym in NO_EARNINGS else \
            earnings_check(sym, earn, expiry, session.trade_date, ov.ivp.get(sym))
        near = sorted((o for o in chain if o.expiry == expiry),
                      key=lambda o: abs(o.strike - tech.last))[:RAW_CHAIN_ROWS]
        raw[sym] = {"tech": {k: (v.isoformat() if isinstance(v, datetime) else v)
                             for k, v in asdict(tech).items()},
                    "expiry": expiry.isoformat(), "earnings": str(earn) if earn else None,
                    "chain": [[o.right, o.strike, o.bid, o.ask, o.oi, o.iv, o.source]
                              for o in near]}
        for right in sides:
            opt = pick_strike(chain, expiry, right, tech.last)
            idea, why = evaluate(sym, right, bias, tech, opt, eflag, earn_res, rules, mode,
                                 now, session)
            if idea:
                ideas.append(idea)
            else:
                skipped.append(f"{sym} {right}: {', '.join(why)}")
        _time.sleep(YF_PAUSE_SEC)

    ideas.sort(key=lambda i: (-i.confidence, i.ticker))
    return ScanResult(mode, now, session, rules, bias, ideas, skipped,
                      day_flags(session, rules), q, load_headlines(), raw)


def format_report(r: ScanResult) -> str:
    s = r.session
    lines = [f"THEESNESS MARKET {r.mode.upper()} — {fmt_hst(r.ran_at)}",
             f"Open {s.open_hst.strftime('%I:%M %p')} / close {s.close_hst.strftime('%I:%M %p')} HST"
             + (" (EARLY CLOSE)" if s.early_close else ""),
             "", f"BIAS: {r.bias.regime} | favored: "
             f"{ {'P': 'PUTS', 'C': 'CALLS'}.get(r.bias.favored, 'none')} | clean: {r.bias.clean}"]
    lines += [f"• {n}" for n in r.bias.notes]
    lines += ["", "FLAGS:"] + [f"• {f}" for f in r.flags]
    if r.headlines:
        lines += ["", "HEADLINES (pasted):"] + [f"• {h}" for h in r.headlines]
    lines += ["", "Ticker | Type | Bias | Expiry | Strike | Entry Range | Premium | Max Contracts "
                  "| Target | Stop | Reasoning | Confidence"]
    lines += [i.card() for i in r.top] if r.top else ["No quality setups."]
    if "UNSET" in {i.max_contracts for i in r.top}:
        lines.append("\n(Max Contracts UNSET — set ACCOUNT_SIZE and RISK_PCT or RISK_FIXED_DOLLARS.)")
    lines.append(f"\nData: delayed ~15 min unless marked ToS. {len(r.ideas)} passing, "
                 f"{len(r.skipped)} rejected.")
    return "\n".join(lines)


def run(mode: str, dry_run: bool, scheduled: bool, ov: Overrides) -> int:
    """Local CLI run (on your own computer)."""
    now = now_hst()
    session = session_for(now.date())
    if session is None:
        print(f"Market closed {now.date()} (weekend/holiday). Nothing to do.")
        return 0
    if scheduled and not in_run_window(session, mode, now):
        print(f"{fmt_hst(now)}: outside {mode} window for today's open "
              f"({fmt_hst(session.open_hst)}). Exiting.")
        return 0
    r = scan(mode, session, ov, now)
    send_telegram(format_report(r), dry_run)
    for i in r.top:
        if i.a_plus:
            send_telegram(f"A+ ALERT — {fmt_hst(now)}\n{i.card()}", dry_run)
    if r.ideas:
        log_ideas(r.ideas, mode)
    if dry_run and r.skipped:
        print("Rejected:\n  " + "\n  ".join(r.skipped))
    return 0


# =============================================================================================
# SELF-TEST (synthetic data, no network)
# =============================================================================================

def selftest() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    # Time
    s_summer = session_for(date(2026, 10, 19))
    s_winter = session_for(date(2026, 11, 2))
    check("summer open 3:30 HST", s_summer.open_hst.time() == time(3, 30))
    check("summer close 10:00 HST", s_summer.close_hst.time() == time(10, 0))
    check("winter open 4:30 HST", s_winter.open_hst.time() == time(4, 30))
    check("institutional window summer 4:00", s_summer.at(30).time() == time(4, 0))
    check("open is tz-aware HST", s_summer.open_hst.utcoffset() == timedelta(hours=-10))
    check("Thanksgiving closed", session_for(date(2026, 11, 26)) is None)
    check("Black Friday early close 8:00 HST",
          session_for(date(2026, 11, 27)).close_hst.time() == time(8, 0))
    check("Wed 11/25/2026 is pre-holiday", is_pre_holiday(date(2026, 11, 25)))
    check("pre-holiday -> 0DTE only", day_rules(date(2026, 11, 25))["zero_dte_only"])
    check("Friday -> 0DTE only", day_rules(date(2026, 10, 23))["zero_dte_only"])
    check("Monday not 0DTE-only", not day_rules(date(2026, 10, 19))["zero_dte_only"])
    check("FOMC 10/28 clear 8:15 HST",
          fomc_clear_time_hst(date(2026, 10, 28)).time() == time(8, 15))

    # Bias
    Q = lambda p: Quote("x", 100 + p, 100)
    b = compute_bias(Q(-0.5), Q(-0.6), Q(-0.4), Q(+5), Q(-0.4), Q(-0.5))
    check("ES+YM red -> BEAR puts, clean", b.regime == "BEAR" and b.favored == "P" and b.clean)
    b = compute_bias(Q(-0.3), Q(-0.5), Q(+0.3), Q(+3), Q(-0.2), Q(-0.4))
    check("YM green + NQ/ES red -> ROTATION", b.regime == "ROTATION" and b.favored == "P")
    b = compute_bias(Q(+0.4), Q(+0.5), Q(+0.3), Q(-4), Q(+0.3), Q(+0.4))
    check("ES+YM green -> BULL, puts blocked", b.regime == "BULL" and b.puts_blocked and b.clean)
    b = compute_bias(Q(-0.5), Q(-0.6), Q(-0.4), Q(+0.5), Q(-0.4), Q(-0.5))
    check("VIX not up enough -> not clean", b.regime == "BEAR" and not b.clean)
    b = compute_bias(Q(+0.05), Q(0), Q(-0.05), Q(0), Q(0), Q(0))
    check("flat futures -> MIXED, no favored", b.regime == "MIXED" and b.favored is None)
    b = compute_bias(Quote("ES"), Q(0), Q(0), Q(0), Q(0), Q(0))
    check("missing ES -> UNKNOWN", b.regime == "UNKNOWN")

    # Expiry
    mon = date(2026, 10, 19)
    fri = date(2026, 10, 23)
    exps = [mon, date(2026, 10, 21), fri, date(2026, 10, 26), date(2026, 10, 30)]
    check("Mon: no 0DTE, picks this Friday", pick_expiry(exps, mon, day_rules(mon))[0] == fri)
    thu = date(2026, 10, 22)
    check("Thu: picks Friday (1 DTE)", pick_expiry(exps, thu, day_rules(thu))[0] == fri)
    check("Fri: 0DTE", pick_expiry(exps, fri, day_rules(fri))[0] == fri)
    check("Fri without 0DTE -> none",
          pick_expiry([date(2026, 10, 26)], fri, day_rules(fri))[0] is None)
    check("Mon: next-week-only expiries -> none (no weekend hold)",
          pick_expiry([mon, date(2026, 10, 26)], mon, day_rules(mon))[0] is None)
    check("Good Friday week: hold limit Thu 4/2", hold_limit_day(date(2026, 3, 30)) == date(2026, 4, 2))
    check("Thanksgiving week Tue: hold limit Wed 11/25 (no holiday hold)",
          hold_limit_day(date(2026, 11, 24)) == date(2026, 11, 25))
    check("Normal Mon: hold limit Fri", hold_limit_day(mon) == fri)
    check("Wed before Thanksgiving: Friday rules (0DTE)",
          pick_expiry([date(2026, 11, 25), date(2026, 11, 27)], date(2026, 11, 25),
                      day_rules(date(2026, 11, 25)))[0] == date(2026, 11, 25))

    # Liquidity
    good = OptionQuote(mon, "P", 670, 1.10, 1.14, 12000)
    check("liquid contract passes", liquidity_check(good)[0])
    check("OI 400 rejected", not liquidity_check(OptionQuote(mon, "P", 670, 1.1, 1.14, 400))[0])
    check("wide spread rejected",
          not liquidity_check(OptionQuote(mon, "P", 670, 1.0, 1.4, 9000))[0])

    # Strike
    chain = [OptionQuote(mon, "P", k, 1, 1.05, 1000) for k in (668, 669, 670, 671)]
    check("put strike = first OTM below spot", pick_strike(chain, mon, "P", 670.4).strike == 670)

    # Earnings
    check("earnings mid-hold rejected",
          earnings_check("X", date(2026, 10, 20), date(2026, 10, 23), mon, None)[0] != "")
    check("earnings 0DTE no IVP -> stand down", earnings_check("X", mon, mon, mon, None)[0] != "")
    check("earnings 0DTE IVP 80 -> stand down", earnings_check("X", mon, mon, mon, 80)[0] != "")
    check("earnings 0DTE IVP 30 -> ok", earnings_check("X", mon, mon, mon, 30)[0] == "")

    # Gap / mid-range
    t = Technicals(prev_high=105, prev_low=95, prev_close=100, last=98.5)
    check("gap down 1.5% + put = chase", gap_chase(t, "P"))
    check("gap down + call != chase", not gap_chase(t, "C"))
    t2 = Technicals(prev_high=110, prev_low=90, prev_close=100, last=100.0)
    check("mid of range rejected", mid_range(t2))
    t2.vwap = 100.1
    check("mid of range at VWAP allowed", not mid_range(t2))

    # Full evaluate: A+ path in confirm mode
    bias = compute_bias(Q(-0.5), Q(-0.6), Q(-0.4), Q(+5), Q(-0.4), Q(-0.5))
    tech = Technicals(prev_high=675, prev_low=660, prev_close=672, last=670.4, avg_volume=6e7,
                      trend={"1m": "down", "5m": "down", "15m": "down"}, vwap=670.6,
                      first5_open=671.5, first5_close=670.8, rvol=2.0)
    now = s_summer.at(35)
    idea, why = evaluate("SPY", "P", bias, tech, good, "", ("", ""), day_rules(mon), "confirm",
                         now, s_summer)
    check("A+ idea produced", idea is not None and idea.a_plus and idea.confidence >= 8)
    check("card carries stock price", idea and idea.underlying == 670.4
          and "@ $670.40" in idea.card())
    check("entry ceiling = +20%", idea and abs(idea.entry_ceiling - round(idea.premium * 1.2, 2)) < 1e-9)
    idea2, _ = evaluate("SPY", "P", bias, tech, good, "", ("", ""), day_rules(mon), "premarket",
                        s_summer.at(-20), s_summer)
    check("premarket never A+", idea2 is not None and not idea2.a_plus
          and idea2.status.startswith("CONDITIONAL"))
    tech_bad = Technicals(**{**asdict(tech), "first5_close": 672.0})
    _, why = evaluate("SPY", "P", bias, tech_bad, good, "", ("", ""), day_rules(mon), "confirm",
                      now, s_summer)
    check("green first candle blocks put", "first 5-min candle does not confirm" in why)
    bull = compute_bias(Q(+0.4), Q(+0.5), Q(+0.3), Q(-4), Q(+0.3), Q(+0.4))
    _, why = evaluate("SPY", "P", bull, tech, good, "", ("", ""), day_rules(mon), "confirm",
                      now, s_summer)
    check("BULL blocks puts", "ES+YM green: no put setups" in why)
    fomc = date(2026, 10, 28)
    tech_flat = Technicals(**{**asdict(tech), "trend": {"1m": "flat", "5m": "down", "15m": "down"}})
    _, why = evaluate("SPY", "P", bias, tech_flat, good, "", ("", ""), day_rules(mon), "confirm",
                      now, s_summer)
    check("confirm: 1m flat -> trend not aligned", any(w.startswith("trend not aligned") for w in why))
    up = {"1m": "up", "5m": "up", "15m": "up"}
    t_res = Technicals(prev_high=7740.0, prev_low=7680, prev_close=7700, last=7735.0, avg_volume=6e7,
                       trend=up, vwap=7700.0, first5_open=7700, first5_close=7710, rvol=2.0)
    check("call just under PDH -> into resistance", level_read(t_res, "C") == (None, "PDH"))
    t_sup = Technicals(**{**asdict(t_res), "prev_high": 7733.0})
    check("call just above broken PDH -> support", level_read(t_sup, "C") == ("PDH", None))
    check("put just above PDH-as-support -> into support", level_read(t_sup, "P") == (None, "PDH"))
    sf = session_for(fomc)
    idea3, _ = evaluate("SPY", "P", bias, tech, OptionQuote(fomc, "P", 670, 1.10, 1.14, 12000),
                        "", ("", ""), day_rules(fomc), "confirm", sf.at(35), sf)
    check("FOMC day: WAIT, not A+", idea3 and idea3.status == "WAIT — FOMC" and not idea3.a_plus)

    # Overrides
    ov = parse_overrides("ES last=6712.25 prev=6740\nNVDA ivp=72\nNVDA earnings=2026-10-22\n"
                         "SPY 2026-10-19 P 670 bid=1.10 ask=1.14 oi=12000  # note")
    check("override quote parsed", ov.quotes["ES"]["last"] == 6712.25)
    check("override ivp/earnings parsed",
          ov.ivp["NVDA"] == 72 and ov.earnings["NVDA"] == date(2026, 10, 22))
    check("override option parsed", ov.options[0][1].strike == 670 and ov.options[0][1].oi == 12000)

    # Sizing
    global RISK_PCT, ACCOUNT_SIZE
    saved = (RISK_PCT, ACCOUNT_SIZE)
    ACCOUNT_SIZE, RISK_PCT = 10_000.0, None
    check("sizing UNSET without risk", max_contracts(1.32, 0.72) == "UNSET")
    ACCOUNT_SIZE, RISK_PCT = 0.0, 0.10
    check("sizing UNSET without account size", max_contracts(1.32, 0.72) == "UNSET")
    ACCOUNT_SIZE, RISK_PCT = 10_000.0, 0.10
    check("sizing 10% of 10k, (1.32-0.72)*100 risk -> 16", max_contracts(1.32, 0.72) == "16")
    RISK_PCT, ACCOUNT_SIZE = saved

    print(f"\n{fails} failure(s)")
    return 1 if fails else 0


# =============================================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Mauna Kala pre-market options assistant")
    p.add_argument("--mode", choices=["premarket", "confirm"], default="premarket")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending Telegram")
    p.add_argument("--scheduled", action="store_true",
                   help="exit quietly unless now is inside this mode's run window")
    p.add_argument("--tos", metavar="FILE", help="ThinkorSwim override file (text or JSON)")
    p.add_argument("--tos-paste", action="store_true",
                   help="paste ToS values on stdin, end with Ctrl-D (Ctrl-Z Enter on Windows)")
    p.add_argument("--check-time", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.check_time:
        check_time(); return 0
    if a.selftest:
        return selftest()
    text = ""
    if a.tos:
        text = Path(a.tos).read_text()
    elif a.tos_paste:
        print("Paste ToS values, then Ctrl-D:", file=sys.stderr)
        text = sys.stdin.read()
    try:
        ov = parse_overrides(text)
    except Exception as e:  # noqa: BLE001
        print(f"[error] could not parse ToS overrides: {e}", file=sys.stderr)
        return 2
    return run(a.mode, a.dry_run, a.scheduled, ov)


if __name__ == "__main__":
    sys.exit(main())
