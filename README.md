# Sniper Strategy Bot

Python implementation of a first-pass sniper-style forex spread betting bot for IG.

It is designed to start safely:

- `DEMO` environment by default
- `DRY_RUN=true` by default
- explicit spread filter before entries
- one open position per market direction check
- ATR-based stop and target distances

## Why Python

IG exposes REST and streaming APIs over HTTP/JSON. Python is the most practical first language for this bot because it has strong HTTP tooling, data analysis libraries when we add backtesting, and simple deployment paths for scheduled workers.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` with your IG demo API key, username, password, account id if needed, and the forex EPICs you want to trade.

## Run once

```bash
set -a
source .env
set +a
sniper-bot --once
```

## Run continuously

```bash
set -a
source .env
set +a
sniper-bot
```

## Backtest CSV Data

Download 5-minute candles from IG first:

```bash
sniper-download-prices CS.D.EURUSD.MINI.IP --resolution MINUTE_5 --points 1000 --out data/eurusd_5m.csv
```

Then backtest the CSV:

```bash
sniper-backtest data/EURUSD_5m.csv \
  --spread 0.0001 \
  --trades-out backtest-results/eurusd_trades.csv \
  --summary-out backtest-results/eurusd_summary.json
```

For parameter work, use a chronological train/test split so later candles stay out-of-sample:

```bash
sniper-split-backtest data/USDJPY_5m.csv \
  --train-fraction 0.8 \
  --entry-start-time 09:00 \
  --entry-end-time 19:00 \
  --entry-timezone Europe/London \
  --out-dir backtest-results/usdjpy_split_80_20
```

The CSV needs `timestamp`, `open`, `high`, `low`, and `close` columns. `volume` and `spread` are optional.

The backtester uses the same indicator entry logic as the live bot:

- enter on the signal candle close after EMA 9/21 crosses
- place a working limit entry at the KhanSaab signal price
- use a fixed 10-point stop and 10-point take profit by default
- exit on the full-allocation target selected by `TAKE_PROFIT_ALLOCATIONS`, or on SL
- if SL and TP are both touched in one candle, count SL first for conservative results

## Per-Candle Results

For historical bid/ask tick replay with five-minute indicators recalculated on
every observation, see [Tick Replay](docs/tick-backtest.md). That engine supports
next-tick crossover entries, dynamic score-selected targets, and 24-hour exits.

Use `--min-entry-volume 2000` with either backtest command to permit BUY and SELL
entries only when the signal candle's raw volume is strictly greater than 2000.
Omitting the option disables this filter. All candles still update indicators,
equity, stops, and targets. Opposite signals can still close a position on a
low-volume candle, but cannot open the reverse position unless volume qualifies.
The CSV includes `min_entry_volume` and `entry_volume_allowed` for auditing.
Omit entry start/end times to allow entries at any time of day.

`sniper-backtest` now always writes `backtest_results.csv` in the current directory.
Use `--results-out path/to/backtest_results.csv` to choose another location.
The Python result exposes a pandas DataFrame as `result.dataframe`.
The split backtester writes separate `train/backtest_results.csv` and
`test/backtest_results.csv` files within its output directory.

Each row represents the position **after processing an input candle**. Five-minute
input produces five-minute observations, including flat periods outside the entry
session. Timestamps preserve the input candle labels in UTC; order events within
an OHLC candle have no known exact fill time. Missing candles and weekends are not
filled with invented prices. `--from-time` is inclusive and `--to-time` exclusive.

Key columns:

| Columns | Meaning |
| --- | --- |
| `timestamp`, `open`, `high`, `low`, `close`, `volume`, `spread` | Source candle values, preserving the input price scale. |
| `position`, `position_direction` | End-of-candle exposure: 1/BUY, -1/SELL, or 0/FLAT. |
| `position_fraction`, `stake_per_point` | Remaining fraction of the original position and its GBP/point stake. |
| `trade_id`, `entry_price`, `stop_price`, `tp1`-`tp3`, `tp1_hit`-`tp3_hit` | Position identifier, actual execution levels, and actual target fills. |
| `orders` | JSON array of this candle's simulated placements, fills, amendments, and cancellations, with order/trade IDs, sides, prices, fractions, and stakes. Exit fills also include P&L. |
| `active_orders` | JSON array of protective stop and target orders still working after the candle. |
| `closed_positions` | JSON array of positions closed during the candle, with exit reasons and final returns; retains the old position when a reversal opens a new one. |
| `position_realized_pnl_cash`, `position_unrealized_pnl_cash`, `position_total_pnl_cash` | GBP profit already taken, remaining exposure marked at the candle close, and their sum. |
| `position_return` | Total position P&L / original price exposure, as a decimal. Exposure = abs(entry price / point size * original GBP-per-point stake). This is not return on margin. |
| `position_r_multiple` | Total position P&L / original cash risk to the stop. |
| `realized_pnl_cash`, `unrealized_pnl_cash`, `balance`, `equity` | Cumulative realised profit (including partial exits), open unrealised profit, cash balance, and marked equity. |
| `account_return` | Current equity / previous row's equity - 1, as a decimal. The first row uses starting balance; a zero previous equity produces a blank return. |
| `cumulative_account_return` | (Equity - starting balance) / starting balance, as a decimal. |
| `drawdown_cash`, `drawdown_return` | GBP decline from the highest candle equity and its negative decimal return. |
| `status`, `signal_direction`, `signal_reason`, `entry_time_allowed` | Raw indicator signal, filtered decision, explanation, and entry-session eligibility. |
| `bull_score`, `bear_score`, `entry_bull_score`, `entry_bear_score` | Current and entry-time scores, on the original 0-100 scale. |
| `market_bias`, `trend_strength`, `ema9`, `ema21`, `ema50`, `atr`, `rsi`, `rsi5m`, `vwap`, `macd_main`, `macd_signal`, `adx`, `volume_average` | Indicator diagnostics. |

For example, a `position_return` of `0.01` means a 1% return on original price
exposure. Partial exits remain included in this return. On a closure candle,
`position` is zero but the position metrics retain its final result. On a reversal,
the main metrics describe the new position and `closed_positions` describes the old
one. Subsequent flat rows have zero position P&L and return, with blank entry levels.
`ambiguous_exit_candles` counts SL/TP conflicts accumulated by the positions shown
on the row, not an account-wide cumulative total.

The export uses midpoint levels and close-of-candle entries, configurable SL/TP
ordering, and forced closure at the last in-window candle. The simplified fixed
outcome model caps stop fills at exactly −10 points even after a price gap; live
non-guaranteed stops can slip beyond that level. Cash calculations retain fractional pence
so partial exits reconcile with trade and account totals; display rounding is not
applied to the ledger. Summary drawdown includes unrealised candle-close P&L.
Indicator warm-up values are unavailable until enough observations exist, and
entries are suppressed during warm-up. It does not add bid/ask execution costs, financing, margin
limits, or broker order submissions. The yearly Dukascopy bid-derived file has
zero spread. Equity can become negative because the existing simulation has no
margin stop-out rule.

## Strategy

The first version is intentionally conservative and configurable:

- fresh signal candles are detected from EMA 9/21 crossovers
- retests are tracked when price pulls back into the EMA 9/21 ribbon after a signal
- a 7-factor bull/bear dashboard scores VWAP, RSI, MACD, EMA trend, ADX, volume candle direction, and 5m RSI
- `FIXED_BRACKET_POINTS=10` defines a symmetric 10-point stop and target around
  the KhanSaab signal price
- the broker order attaches the one target whose `TAKE_PROFIT_ALLOCATIONS` value
  is `1`; live mode rejects partial multi-target plans because one IG position
  cannot reproduce the candle backtest's synthetic partial exits
- optional spread filtering can be enabled with `USE_SPREAD_FILTER=true`
- `DEFAULT_SIZE` is the spread-bet stake in GBP per point, defaulting to `10`,
  so a 10-point win is +£100 and a 10-point loss is −£100 before costs
- backtest take profits close 100% at TP1 by default (`TAKE_PROFIT_ALLOCATIONS=1,0,0`); SL and opposite-signal exits still apply

The public Pine source does **not** use its dashboard scores to gate BUY/SELL
labels. Accordingly, `APPLY_SCORE_ENTRY_FILTERS=false` is the default and both
live and candle backtest entries follow the EMA 9/21 crossover labels. Set it to
`true` only to opt into the older Python score-filter extension; then long
entries use `MIN_BULL_SCORE_LONG`, while short entries use
`MIN_BEAR_SCORE_SHORT` and `REQUIRE_SHORT_BEAR_DOMINANCE`.

The supplied Pine is an `indicator()`, not a `strategy()`: it defines no stop,
target, fill, sizing, or performance rules. The fixed 10-point bracket in this
repository is an explicit execution assumption and cannot be expected to
reproduce discretionary manual results without an exact manual exit protocol.
The live runner evaluates completed candles only and de-duplicates each signal
candle for the lifetime of the process. Use the tick replay when intrabar entry
timing and bid/ask execution need to be modelled.

This is a foundation, not financial advice. Validate in IG demo, add backtests, and review every EPIC's margin, minimum size, expiry, and dealing rules before risking capital.

## IG Diagnostics

Check which gateway and credentials the app is using without printing secrets:

```bash
sniper-check-ig --epic CS.D.EURUSD.MINI.IP
```

If IG returns `error.security.api-key-invalid`, revoke the exposed/old key, create a fresh key in the matching Demo or Live API Keys tab, and make sure `IG_ENV` matches that key.

## Hosting Later

The backtester can write `backtest-results/*.json` summaries. A public website should read those generated summaries, never `.env`, API keys, or raw broker tokens.

## Results API

Start the local API:

```bash
sniper-api --host 127.0.0.1 --port 8000
```

Public-safe endpoints:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/results?name=eurusd
curl http://127.0.0.1:8000/api/trades?name=eurusd
```

Refresh data and rerun a backtest:

```bash
curl -X POST http://127.0.0.1:8000/api/backtests/run \
  -H 'Content-Type: application/json' \
  -d '{"name":"eurusd","epic":"CS.D.EURUSD.MINI.IP","resolution":"MINUTE_5","points":1000}'
```

Before hosting publicly, set `API_ADMIN_TOKEN` and require `Authorization: Bearer <token>` for refresh calls.
