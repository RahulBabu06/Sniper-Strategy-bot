# Historical Tick Replay

## Current CLI Default: Fixed 10-Point Bracket

The default run uses `--exit-mode atr_bracket --tp-mode points --tp-value 10
--sl-mode points --sl-value 10 --size 10`. A full position wins £100 after a
10-point rise or loses £100 after a 10-point fall, before spread or slippage.
Opposite crossovers are ignored while a trade is open.
The default entry mode is a working limit order at Pine's `entryP`, the signal
candle close. A BUY limit fills only when ask reaches the limit; a SELL limit
fills only when bid reaches it. Fills are recorded at the specified limit price.
Selecting TP1 for the whole position is our execution convention, not an order
execution rule in the author's indicator (which only draws target levels).
Scores are recorded but do not select targets or trigger exits. There is no
24-hour timeout in this mode. The final open position closes at the last quote.
The conditional GBP 10 fallback is unused because the supplied Pine defines ATR levels.

```bash
python -m sniper_bot.tick_backtest data/ticks/usdjpy_202501_with_warmup.csv \
  --exit-mode atr_bracket --out-dir backtest-results/usdjpy-2025-01-ticks-atr-bracket
```

The Python `ReplayConfig` default remains `dynamic` for compatibility; the CLI
default is `atr_bracket`. Both the CLI and `ReplayConfig` now default to
`--signal-timing intrabar`. Pass the setting explicitly for reproducible comparisons.

### Signal Timing

- `--signal-timing intrabar`: default behavior. Completed five-minute candles
  remain the indicator history. Every incoming tick updates the current candle's
  OHLC, volume and close, then recalculates EMA, ATR, scores and crossover state.
  An order is submitted after the first tick on which BUY or SELL appears.
- `--signal-timing bar_close`: comparison mode. Wait for a complete candle,
  evaluate its final crossover, and submit on the first tick of the next candle.

Use `--entry-mode pine_limit` (the CLI default) to wait at Pine's `entryP`.
Use `--entry-mode market` only for comparisons with the older next-quote model.
An unfilled limit remains working when new signals arrive; later signals are
recorded as ignored and never replace the original entry. Signals are ignored
while a position is open, and the final unfilled limit is cancelled.
Use `--limit-expiry-minutes 5` to cancel a working limit after one candle.
Omit it to leave the order working until replacement or the end of the data.

Use `--reanchor-on-same-direction-signal` with points-based SL/TP modes to keep
an existing position open while resetting its bracket around every newly
confirmed signal in the same direction. Pending entries are not replaced.

Use `--candles-csv` to make an existing five-minute file the sole indicator
source while retaining tick-level execution. Candle timestamps denote interval
starts, so a row stamped `10:00` becomes available at `10:05`. Applying its final
OHLC values to earlier ticks would be lookahead. The file's canonical midpoint
OHLC drives Pine calculations by default; tick bid/ask drives fills and exits.

Use `--min-entry-score 60` to require the directional Bull Score for BUY or Bear
Score for SELL to be strictly greater than 60%. A value of zero disables this
filter. Filtered signals are recorded as `FILTERED` order events.

### Configurable Take Profit

Append one of these options to the command above:

- `--tp-mode pine`: default, exit 100% at the original ATR-based TP1.
- `--tp-mode cash --tp-value 10`: target GBP 10 gross profit per position.
- `--tp-mode points --tp-value 100`: target 100 configured points per position.

The stop loss has the same parameter pattern:

- `--sl-mode pine`: default ATR stop from the supplied Pine script.
- `--sl-mode cash --sl-value 10`: GBP 10 maximum loss target.
- `--sl-mode points --sl-value 100`: 100-point stop distance.

Cash/points modes require `atr_bracket`. Both use the actual entry fill, including
the entry spread, and execute against the closing-side bid/ask. A quote jumping
past a threshold may realize more than the requested amount.
Pine targets remain signal-anchored. `tp1`/`tp2`/`tp3` retain the indicator levels;
`fixed_tp_price` records the custom target separately. `tp_mode` is also exported.
The fixed threshold is not rounded to an assumed broker price increment.

At GBP 0.10 per point, GBP 10 and 100 points are equivalent. IG defines one
USD/JPY point as 0.01 in the raw quote. With this dataset's price scale of 100,
the correct `point_size` is therefore 1.0: 100 points is 100 scaled units, or
1.00 in the raw USD/JPY quote.

Reference: https://www.ig.com/uk/help-and-support/articles/681881-forex-spread-betting-product-details

For notebooks:

```python
config = ReplayConfig(exit_mode="atr_bracket", tp_mode="cash", tp_value=10)
```

For a GBP 10 bracket on both sides:

```bash
python -m sniper_bot.tick_backtest data/ticks/usdjpy_202501_with_warmup.csv \
  --exit-mode atr_bracket \
  --signal-timing intrabar \
  --tp-mode cash --tp-value 10 \
  --sl-mode cash --sl-value 10 \
  --out-dir backtest-results/usdjpy-2025-01-ticks-cash-10-bracket
```

For an auditable proof-of-concept inversion, add `--invert-signals`. This makes
indicator BUY signals execute short and SELL signals execute long. It does not
invert prices, scores, indicators, or exit rules. Both `indicator_signal` and the
executed `direction` are retained in `trades.csv`.

## Original Dynamic Mode Rules

The tick engine is separate from the older candle backtester. It uses the public
**Sniper V.02 Community Version by KhanSaab**, inspected on 17 September 2026:
https://www.tradingview.com/script/HBDcHSB7-Sniper-Entry-Exit-with-SL-TP-by-KhanSaab-V-02/

That source has seven-factor bull/bear scores, EMA9/21 crossover signals, and a
persistent last-signal direction. It has no SL/TP levels or STATUS dashboard row.
The engine labels its BUY/SELL triggers as `status`; otherwise status is WAIT.
Unlike the earlier source, the indicator's direction state does not reset on SL.

- Construct five-minute bid candles and recalculate their values on every quote.
- Recalculate from the last CLOSED candle, not from the previous tick's EMA/RMA.
  Only commit indicator state when moving into the next five-minute candle.
- A transition into BUY or SELL queues an entry for the next observation.
  Repeated identical status observations do not submit repeated entries.
  An intervening WAIT followed by BUY/SELL is a fresh event, including intrabar.
- Entries have no volume or directional-score filters. Indicator warm-up must
  finish so ATR and the scores are defined; this is not a trading filter.
- Buy at ask, sell at bid. Close longs at bid and shorts at ask.
- Preserve input order when several observations have the same timestamp.
  `signal_seq` and `entry_seq` identify the exact observations.
- One open position at a time. Opposite statuses are ignored while a position is
  open by default. Add `--reverse-on-opposite-signal` only for older comparisons.
- The user-approved risk extension uses ATR(14) * 1.5 from the signal tick.
  SL is 1R away; TP1/2/3 are 1R/2R/3R away from the signal price. Levels are
  rounded to one decimal in the scaled price units and stay fixed. They are not
  shifted to the next tick's actual fill price.
- On each tick, use current Bull Score for longs and current Bear Score for shorts.
  Five conditions selects TP1; six selects TP2; seven selects TP3. Exit 100%.
- Close at the current executable quote if the directional score is below 5/7,
  or if the bias is STRONG in the opposite direction. No reverse position is
  opened merely because bias changes; an opposite signal is required.
- A lower target already crossed by the current executable quote causes an
  immediate market exit. A higher score can select a more distant target.
- Existing stop and target orders are processed BEFORE target amendments at the
  same quote. Priority is SL, 24-hour deadline, working TP, strong opposite bias,
  low score, then dynamic target adjustment. This prevents moving a target away
  after the quote has already crossed it.
- An entry with a low score still fills, then may immediately close under the
  low-score exit rule. It incurs the spread. This follows status-only entry.
- No trailing stop, partial exits, or entry-session restriction.
- Timeouts execute at the first available quote at or after 24 hours. Market
  closures can delay the fill; `timeout_delay_seconds` records this explicitly.
- At the end of available data, close open positions at the last quote and do not
  invent a next tick for an unfilled entry. These exits are `END_OF_DATA`.

Pine rollback reference:
https://www.tradingview.com/pine-script-docs/language/execution-model/

## Data and Units

Source: Dukascopy historical bid/ask ticks, downloaded with dukascopy-node 1.50.0.
Tick data schema: https://github.com/Leo4815162342/dukascopy-node/tree/master/examples/with-tick-data

The January run reads 30 December 2024 through 31 January 2025, using December
observations only for warm-up. Empty hours are not filled with artificial ticks.
The data-quality report lists hourly counts and gaps over five minutes. A listed
gap is not automatically a download failure; it may be a market closure.

Current USD/JPY spread-bet quote conversion:

- Raw FX prices multiplied by 100; `--price-scale 100`.
- A raw price of 156.039 is stored as 15603.9.
- One IG USD/JPY point is 0.01 raw, or 1.0 scaled unit; `--point-size 1`.
- The current comparison uses GBP 1 per point; `--size 1`.

These units are explicit simulation settings, not a claim about IG's contract
specification. Verify the instrument's contract point definition before broker use.

Indicators use the candle file's canonical midpoint OHLC by default. Bid/ask
ticks remain the execution prices. The volume is a provider quote-volume proxy,
not consolidated traded FX volume. `--price-basis bid|ask` remains available for
comparisons, but changing the feed or basis can change signals and scores. VWAP resets at UTC midnight by
default; use `--session-timezone` to change the daily boundary. Matching TradingView
exactly also requires matching its symbol/feed, volume convention, and session.

No financing, commissions, margin liquidation, network latency, or slippage beyond
observed quote changes is modeled. Both sides of the quoted spread ARE included.

## Run

For crossover-only exits, add `--exit-mode opposite_signal_only` together with
`--reverse-on-opposite-signal`. This disables SL, TP, all score/bias exits and the
24-hour limit. Each opposite crossover queues a close-and-reverse at the next
observed quote. Same-direction signals do not add
positions. Fixed reference levels remain in exports but are inactive (`active_tp`
is zero). An open position at the end is still liquidated as `END_OF_DATA`.
Use a distinct `--out-dir` to preserve previous results.

Download the historical observations (including two warm-up days):

```bash
npx dukascopy-node@1.50.0 -i usdjpy -from 2024-12-30 -to 2025-02-01 \
  -t tick -v -fl -f csv -dir data/ticks -fn usdjpy_202501_with_warmup \
  -bs 10 -bp 1000 -r 3 -rp 1000
```

```bash
.venv/bin/python -m sniper_bot.tick_backtest \
  data/ticks/usdjpy_202501_with_warmup.csv \
  --from-time 2025-01-01T00:00:00+00:00 \
  --to-time 2025-02-01T00:00:00+00:00 \
  --candles-csv data/dukascopy_USDJPY_2025_5m_warmup.csv \
  --entry-mode pine_limit --limit-expiry-minutes 5 \
  --exit-mode atr_bracket --tp-mode pine --sl-mode pine \
  --size 1 --point-size 1 --price-scale 100 --price-basis mid
```

Default output folder: `backtest-results/usdjpy-2025-01-ticks/`.

- `tick_results.csv.gz`: one observation per tick with signals, scores, quotes,
  equity, active position, pending/fill/amendment events, and completed positions.
- `backtest_results.csv`: last tick of each five-minute candle, accumulating that
  candle's events and exits. `timestamp` is its actual last quote; `candle_timestamp`
  is its bucket start. Account return is recalculated between exported rows.
- `trades.csv`: one complete position per row, including signal and execution
  timestamps/sequences, fixed levels, entry/exit scores, active TP at exit, P&L,
  and reason. It has a different schema from the legacy candle trade export.
- `summary.json`: results, configuration, source hash, and assumptions.
- `data_quality.json`: observation counts and gaps.

```python
import pandas as pd

folder = "../backtest-results/usdjpy-2025-01-ticks/"
ticks = pd.read_csv(folder + "tick_results.csv.gz")  # large; chunksize is also supported
bars = pd.read_csv(folder + "backtest_results.csv")
trades = pd.read_csv(folder + "trades.csv")
```

The legacy root `backtest_results.csv` is not overwritten by the tick run. The
tick exports have their own schema; the legacy `trade_diagnostics` helper is not
the parser for these exports. Read `trades.csv` directly for per-trade analysis.
