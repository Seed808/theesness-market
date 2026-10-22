# TheEsness Market

A self-running pre-market options board, built like
[TheEsness Predictions](https://seed808.github.io/theesness-data/). GitHub Actions runs the
Mauna Kala rules engine on a timer, locks the trade cards, grades them after the close, and
publishes the site to GitHub Pages.

Site: https://seed808.github.io/theesness-market/

## What happens each trading day (all times HST)

| When | What |
|---|---|
| last 30 min before the open | **Pre-market read.** ES/NQ/YM + VIX + SPY/QQQ bias, watchlist scan, up to 5 *conditional* cards. Not graded. |
| open + 30 min (first run after) | **Confirmation read.** Re-checks with 1m/5m/15m trend, first 5-minute candle, VWAP, relative volume. Up to 5 locked cards. A+ cards trigger a Telegram alert. |
| every ~10 min until 9:00am | **Re-scans.** Posts and locks only NEW setups (a ticker + direction already posted today is skipped). Graded like confirmation cards. |
| after the close | **Grading** of every GO card (WIN / LOSS / FLAT from the contract's 5-minute prices), **saves the day's 1-minute bars**, and **re-runs the bias backtest**. |

**Expiry / holding rule.** Mon–Thu: no 0DTE — contracts expire between tomorrow and the last
trading day before the market next closes (normally Friday), so they can be held overnight but
never over a weekend or holiday. Friday and pre-holiday days: 0DTE only. Cards are graded at
their target or stop as soon as either is hit, otherwise at the expiry day's close.

**Target / stop.** Target = signal premium +50%. Stop = signal premium −20% (it was −35% for
cards locked before 2026-10-22; locked cards keep the stop they were posted with).
Run `study` from Actions → market → Run workflow to regrade every card at 15/20/25/30/35% stops
(writes `ledger/stop_study.json`).

Every card shows the stock price it was made from and that price's time, so you can check it
against your own screen. Data is ~15 minutes delayed.

The open is 3:30am HST while the US is on daylight time and 4:30am HST otherwise; the code
works this out each day from New York time.

GitHub's timer is best-effort (runs can be late or skipped), so the workflow wakes every
10 minutes and the runner only does what is still due.

## Backtest

`backtest.html` replays the bias rules on every past day Yahoo still has 5-minute bars for and
checks whether SPY / QQQ moved the favored way first after amateur hour. It tests the bias
rules on the stock only — free data has no past option prices. The option rows saved in
`data/raw/` each run are what will make card-level backtests possible later.

## Files

| Path | What it is |
|---|---|
| `market/engine.py` | The rules (bias, filters, A+ criteria, entry range, sizing). `python market/engine.py --selftest` checks them. |
| `market/runner.py` | Decides what's due, locks cards, grades them. |
| `market/site_build.py` | Writes the pages from the ledger. |
| `market/backtest.py` | Bias backtest. |
| `ledger/runs/` | One locked file per day per read. Never rewritten. |
| `ledger/locks.csv`, `ledger/grades.csv` | Flat record of every card and every grade. |
| `ledger/tests/` | Hand-started test runs (never graded). |
| `ledger/backtest.json` | Latest bias backtest (rebuilt once a day after the close). |
| `data/raw/`, `data/bars/` | Saved market data for backtesting. |
| `watchlist.txt` | Tickers to scan. Edit on GitHub to add/remove. |
| `inputs/tos_override.txt` | Optional ThinkorSwim numbers — only used when dated today. |
| `inputs/headlines.txt` | Optional headlines — only used when dated today. |

## Settings (GitHub → Settings → Secrets and variables → Actions → New repository secret)

All optional. They stay private even though the repo is public.

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Reports + A+ alerts to your phone |
| `ACCOUNT_SIZE` | Settled cash, e.g. `10000` |
| `RISK_PCT` *or* `RISK_FIXED_DOLLARS` | Risk per trade, e.g. `0.10` or `500` |

Max Contracts is only ever sent to Telegram; it never appears on the public site.

## Test it any time

Actions tab → **market** → **Run workflow** → pick `premarket` or `confirm` → Run.
Results appear on the site's **Test runs** page within a few minutes.

## Yearly upkeep

Holiday, early-close and FOMC dates in `market/engine.py` cover 2026–2027.
