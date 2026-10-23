"""
Schwab Trader API — MARKET DATA ONLY. Read-only. Nothing here can see accounts or place orders.

Sources for the endpoints and login flow (Schwab's own docs are behind the developer login):
  * schwab-py (github.com/alexgolec/schwab-py): authorize URL
    https://api.schwabapi.com/v1/oauth/authorize, token URL https://api.schwabapi.com/v1/oauth/token,
    market data under /marketdata/v1 (quotes, chains, pricehistory). Its docs: "Schwab provides
    price history for equities and ETFs. It does not provide price history for options, futures,
    or any other instruments." and "Tokens are only good for seven days."
  * schwabpy (github.com/jaycollett/schwabpy): token requests use HTTP Basic (app key:secret),
    form fields grant_type=authorization_code|refresh_token; the code may contain %40 (= "@").

Every function returns None on ANY problem, and the engine then falls back to Yahoo — so Schwab
can never stop a run. Response fields are read defensively (several possible names).

Token storage: the repo is public, so tokens are kept ENCRYPTED in auth/schwab_token.enc. The
key is derived from the SCHWAB_APP_SECRET GitHub secret; without that secret the file is useless.
auth/schwab_status.json holds only non-secret status (login time, expiry, last error).

Secrets used: SCHWAB_APP_KEY, SCHWAB_APP_SECRET.

CLI:
  python market/schwab.py login "<full https://127.0.0.1/?code=... URL>"
  python market/schwab.py status
  python market/schwab.py test            (one quote, one chain, one history call; prints shapes only)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

API = "https://api.schwabapi.com"
AUTH_URL = API + "/v1/oauth/authorize"
TOKEN_URL = API + "/v1/oauth/token"
CALLBACK = "https://127.0.0.1"
REFRESH_LIFETIME = timedelta(days=7)     # per schwab-py docs; Schwab may expire it sooner or later
MIN_INTERVAL = 0.55                      # seconds between calls (stays under ~120/min)
TIMEOUT = 15

REPO = Path(__file__).resolve().parent.parent
AUTH_DIR = REPO / "auth"
TOKEN_FILE = AUTH_DIR / "schwab_token.enc"
STATUS_FILE = AUTH_DIR / "schwab_status.json"

APP_KEY = os.environ.get("SCHWAB_APP_KEY", "").strip()
APP_SECRET = os.environ.get("SCHWAB_APP_SECRET", "").strip()

# Schwab symbol for our names. Futures/index formats are not confirmed; if Schwab rejects them
# the engine uses Yahoo for those.
SYMBOL_MAP = {"ES": "/ES", "NQ": "/NQ", "YM": "/YM", "VIX": "$VIX", "SPX": "$SPX"}


def configured() -> bool:
    return bool(APP_KEY and APP_SECRET)


def login_link() -> str:
    return f"{AUTH_URL}?client_id={APP_KEY}&redirect_uri={CALLBACK}"


# ------------------------------------------------------------------ encrypted token store

def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"theesness-market:schwab:" + APP_SECRET.encode()).digest())
    return Fernet(key)


def _load() -> Optional[dict]:
    if not (configured() and TOKEN_FILE.exists()):
        return None
    try:
        return json.loads(_fernet().decrypt(TOKEN_FILE.read_bytes()))
    except Exception:  # noqa: BLE001  (wrong secret / corrupt file)
        return None


def _save(tok: dict) -> None:
    AUTH_DIR.mkdir(exist_ok=True)
    TOKEN_FILE.write_bytes(_fernet().encrypt(json.dumps(tok).encode()))


def _status_write(**kw) -> dict:
    AUTH_DIR.mkdir(exist_ok=True)
    st = status()
    st.update(kw)
    STATUS_FILE.write_text(json.dumps(st, indent=1))
    return st


def status() -> dict:
    st = {}
    if STATUS_FILE.exists():
        try:
            st = json.loads(STATUS_FILE.read_text())
        except ValueError:
            st = {}
    st["configured"] = configured()
    if st.get("expires_at"):
        left = datetime.fromisoformat(st["expires_at"]) - datetime.now(timezone.utc)
        st["hours_left"] = round(left.total_seconds() / 3600, 1)
    return st


# ------------------------------------------------------------------ token requests

def _token_request(form: dict) -> Optional[dict]:
    import requests
    basic = base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()
    try:
        r = requests.post(TOKEN_URL, data=form, timeout=TIMEOUT,
                          headers={"Authorization": f"Basic {basic}",
                                   "Content-Type": "application/x-www-form-urlencoded"})
    except Exception as e:  # noqa: BLE001
        _status_write(last_error=f"token request: {type(e).__name__}")
        return None
    if r.status_code != 200:
        # Never log the body in full; it can echo request data.
        _status_write(last_error=f"token HTTP {r.status_code}: {r.text[:120]}")
        return None
    return r.json()


def code_from_url(url: str) -> Optional[str]:
    """Pulls the code out of the https://127.0.0.1/?code=...&session=... address (decodes %40)."""
    q = parse_qs(urlparse(url.strip()).query)
    code = (q.get("code") or [None])[0]
    return code.replace("%40", "@") if code else None


def login(redirect_url: str) -> bool:
    code = code_from_url(redirect_url)
    if not code:
        _status_write(last_error="no ?code= in the pasted address")
        return False
    t = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK})
    return _store_new(t, fresh_login=True)


def _store_new(t: Optional[dict], fresh_login: bool = False) -> bool:
    if not t or not t.get("access_token"):
        return False
    now = datetime.now(timezone.utc)
    old = _load() or {}
    tok = {
        "access_token": t["access_token"],
        "access_expires_at": (now + timedelta(seconds=int(t.get("expires_in", 1800)))).isoformat(),
        "refresh_token": t.get("refresh_token") or old.get("refresh_token"),
        "login_at": now.isoformat() if fresh_login else old.get("login_at", now.isoformat()),
    }
    _save(tok)
    login_at = datetime.fromisoformat(tok["login_at"])
    _status_write(connected=True, login_at=tok["login_at"],
                  expires_at=(login_at + REFRESH_LIFETIME).isoformat(),
                  last_ok=now.isoformat(), last_error=None)
    return True


_ACCESS: Optional[str] = None


def access_token() -> Optional[str]:
    """Valid access token, refreshing it (30-min life) with the stored refresh token as needed."""
    global _ACCESS
    if _ACCESS:
        return _ACCESS
    tok = _load()
    if not tok or not tok.get("refresh_token"):
        return None
    if datetime.fromisoformat(tok["access_expires_at"]) - datetime.now(timezone.utc) > timedelta(minutes=3):
        _ACCESS = tok["access_token"]
        return _ACCESS
    t = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    if not _store_new(t):
        _status_write(connected=False)
        return None
    _ACCESS = _load()["access_token"]
    return _ACCESS


# ------------------------------------------------------------------ market data GET

_LAST_CALL = [0.0]
CALLS = {"ok": 0, "fail": 0}


def get(path: str, params: dict) -> Optional[dict]:
    import requests
    global _ACCESS
    tok = access_token()
    if not tok:
        return None
    for attempt in range(3):
        wait = MIN_INTERVAL - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()
        try:
            r = requests.get(API + path, params=params, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        except Exception:  # noqa: BLE001
            CALLS["fail"] += 1
            return None
        if r.status_code == 200:
            CALLS["ok"] += 1
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401 and attempt == 0:
            _ACCESS = None
            tok = access_token()
            if not tok:
                break
            continue
        break
    CALLS["fail"] += 1
    return None


def _num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "NaN"):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# ------------------------------------------------------------------ data functions

def quotes(names: list) -> dict:
    """{our_name: (last, prev_close)} for every name Schwab answers. Batched."""
    out = {}
    syms = {SYMBOL_MAP.get(n, n): n for n in names}
    keys = list(syms)
    for i in range(0, len(keys), 40):
        j = get("/marketdata/v1/quotes", {"symbols": ",".join(keys[i:i + 40])})
        if not isinstance(j, dict):
            continue
        for sym, body in j.items():
            if not isinstance(body, dict):
                continue
            q = body.get("quote", body)
            last = _num(q, "lastPrice", "mark", "closePrice")
            prev = _num(q, "closePrice", "previousClose")
            if sym in syms and last and prev:
                out[syms[sym]] = (last, prev)
    return out


def _candles(j) -> Optional["object"]:
    import pandas as pd
    if not isinstance(j, dict) or not j.get("candles"):
        return None
    rows = j["candles"]
    df = pd.DataFrame({
        "Open": [_num(c, "open") for c in rows], "High": [_num(c, "high") for c in rows],
        "Low": [_num(c, "low") for c in rows], "Close": [_num(c, "close") for c in rows],
        "Volume": [_num(c, "volume") or 0 for c in rows]},
        index=pd.to_datetime([int(c["datetime"]) for c in rows], unit="ms", utc=True))
    return df.dropna(subset=["Close"]).tz_convert("America/New_York").sort_index()


def history_frames(name: str, now: datetime):
    """(daily_3mo, minute_5d_with_extended_hours) DataFrames shaped like yfinance's, or None."""
    sym = SYMBOL_MAP.get(name, name)
    daily = _candles(get("/marketdata/v1/pricehistory", {
        "symbol": sym, "periodType": "month", "period": 3, "frequencyType": "daily", "frequency": 1}))
    if daily is None:
        return None
    end = int(now.timestamp() * 1000)
    start = int((now - timedelta(days=6)).timestamp() * 1000)
    m1 = _candles(get("/marketdata/v1/pricehistory", {
        "symbol": sym, "frequencyType": "minute", "frequency": 1, "startDate": start, "endDate": end,
        "needExtendedHoursData": "true"}))
    if m1 is None:
        return None
    return daily, m1


def chain(name: str, start: date, end: date, strike_count: int = 40) -> Optional[list]:
    """Option contracts expiring start..end as engine OptionQuote objects (source 'schwab')."""
    import engine as E
    j = get("/marketdata/v1/chains", {
        "symbol": SYMBOL_MAP.get(name, name), "contractType": "ALL", "strikeCount": strike_count,
        "includeUnderlyingQuote": "false", "fromDate": start.isoformat(), "toDate": end.isoformat()})
    if not isinstance(j, dict):
        return None
    out = []
    for mkey, right in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        for exp_key, strikes in (j.get(mkey) or {}).items():
            try:
                exp = date.fromisoformat(str(exp_key)[:10])
            except ValueError:
                continue
            for k, contracts in (strikes or {}).items():
                for c in contracts or []:
                    try:
                        strike = float(c.get("strikePrice", k))
                    except (TypeError, ValueError):
                        continue
                    oi = _num(c, "openInterest")
                    iv = _num(c, "volatility")
                    out.append(E.OptionQuote(
                        exp, right, strike, _num(c, "bid", "bidPrice"), _num(c, "ask", "askPrice"),
                        int(oi) if oi is not None else None,
                        iv / 100 if iv and iv > 3 else iv, _num(c, "delta"), "schwab",
                        str(c.get("symbol", "")).replace(" ", "")))
    return out or None


# ------------------------------------------------------------------ provider for the engine

class Provider:
    """Plugged into engine.PROVIDER when Schwab is connected. Each method: data or None."""

    def __init__(self):
        self._q = {}

    def prefetch_quotes(self, names):
        self._q.update(quotes(names))

    def quote(self, name):
        if name not in self._q:
            self._q.update(quotes([name]))
        return self._q.get(name)

    def frames(self, name, now):
        return history_frames(name, now)

    def chain(self, name, start, end):
        return chain(name, start, end)


def provider_if_connected() -> Optional[Provider]:
    if configured() and access_token():
        return Provider()
    return None


# ------------------------------------------------------------------ CLI

def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if not configured():
        print("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set")
        return 2
    if cmd == "login":
        ok = login(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SCHWAB_REDIRECT_URL", ""))
        st = status()
        print("LOGIN OK" if ok else f"LOGIN FAILED: {st.get('last_error')}")
        return 0 if ok else 1
    if cmd == "test":
        ok = bool(access_token())
        print("token:", "ok" if ok else "none")
        if ok:
            q = quotes(["SPY", "ES", "VIX"])
            print("quotes answered for:", sorted(q))
            f = history_frames("SPY", datetime.now(timezone.utc))
            print("SPY history rows (daily, 1m):", None if f is None else (len(f[0]), len(f[1])))
            today = date.today()
            c = chain("SPY", today, today + timedelta(days=7))
            print("SPY chain contracts:", None if c is None else len(c),
                  "sample fields ok:", bool(c and c[0].bid is not None and c[0].oi is not None))
        print("status:", {k: v for k, v in status().items() if k != "last_error"},
              "last_error:", status().get("last_error"))
        return 0
    st = status()
    print(json.dumps(st, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
