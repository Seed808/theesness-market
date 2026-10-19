# TheEsness Market

A self-running pre-market options board, built like
[TheEsness Predictions](https://seed808.github.io/theesness-data/). GitHub Actions runs the
Mauna Kala rules engine on a timer, locks the trade cards, grades them after the close, and
publishes the site to GitHub Pages.

Site: https://seed808.github.io/theesness-market/

## What happens each trading day (all times HST)

| When | What |
|---|---|
| ~open − 75 min to the open | **Pre-market read.** ES/NQ/YM + VIX + SPY/QQQ bias, watchlist scan, up to 3 *conditional* cards. Not graded. |
| open + 30 to open + 120 min | **Confirmation read.** Re-checks with 1m/5m/15m trend, first 5-minute candle, VWAP, relative volume. Up to 3 cards, locked. A+ cards trigger a Telegram alert. |
| after the close | **Grading.** Each GO card from the confirmation read is marked WIN / LOSS / FLAT from the contract's 5-minute prices. |

The open is 3:30am HST while the US is on daylight time and 4:30am HST otherwise; the code
works this out each day from New York time.

GitHub's timer is best-effort (runs can be late or skipped), so the workflow wakes every
10 minutes and the runner only does what is still due.

## Files

| Path | What it is |
|---|---|
| `market/engine.py` | The rules (bias, filters, A+ criteria, entry range, sizing). `python market/engine.py --selftest` checks them. |
| `market/runner.py` | Decides what's due, locks cards, grades them. |
| `market/site_build.py` | Writes the pages from the ledger. |
| `ledger/runs/` | One locked file per day per read. Never rewritten. |
| `ledger/locks.csv`, `ledger/grades.csv` | Flat record of every card and every grade. |
| `ledger/tests/` | Hand-started test runs (never graded). |
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
