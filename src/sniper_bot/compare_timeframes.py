from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from sniper_bot.backtest import BacktestResult
from sniper_bot.backtest import _parse_directions
from sniper_bot.backtest import run_backtest
from sniper_bot.backtest import write_summary_json
from sniper_bot.backtest import write_trades_csv
from sniper_bot.config import load_config
from sniper_bot.csv_loader import load_candles_csv
from sniper_bot.csv_loader import write_candles_csv
from sniper_bot.dukascopy_client import DukascopyDataError
from sniper_bot.dukascopy_client import download_dukascopy_5m
from sniper_bot.ig_client import IGApiError
from sniper_bot.ig_client import IGClient
from sniper_bot.market import Candle
from sniper_bot.resample import resample_candles
from sniper_bot.strategy import SniperStrategy
from sniper_bot.yahoo_client import YahooDataError
from sniper_bot.yahoo_client import download_yahoo_forex


LOGGER = logging.getLogger("sniper_bot.compare_timeframes")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch real IG 5m candles, resample to higher timeframes, and compare KhanSaab backtests."
    )
    parser.add_argument("epic", nargs="?", help="IG market EPIC. Defaults to the first MARKET_EPICS value.")
    parser.add_argument("--from-time", required=True, help="Inclusive backtest start timestamp.")
    parser.add_argument("--to-time", required=True, help="Exclusive backtest end timestamp.")
    parser.add_argument(
        "--warmup-from-time",
        required=True,
        help="Earlier timestamp used to warm up EMA/RSI/MACD/ADX/VWAP calculations.",
    )
    parser.add_argument(
        "--timeframes",
        default="10,15",
        help="Comma-separated minute timeframes to compare. Default: 10,15.",
    )
    parser.add_argument("--chunk-hours", type=int, default=24, help="IG download chunk size. Default: 24.")
    parser.add_argument(
        "--source",
        choices=["ig", "yahoo", "dukascopy"],
        default="ig",
        help="Candle data source. Default: ig.",
    )
    parser.add_argument("--yahoo-symbol", default="JPY=X", help="Yahoo Finance forex symbol. Default: JPY=X.")
    parser.add_argument("--yahoo-interval", default="5m", help="Yahoo Finance interval. Default: 5m.")
    parser.add_argument("--dukascopy-instrument", default="USDJPY", help="Dukascopy instrument. Default: USDJPY.")
    parser.add_argument(
        "--price-scale",
        type=float,
        default=100.0,
        help="Scale Yahoo FX rates into IG-style spread-bet points. Default: 100.",
    )
    parser.add_argument(
        "--synthetic-spread",
        type=float,
        default=1.0,
        help="Synthetic spread in scaled points for non-IG data. Default: 1.0.",
    )
    parser.add_argument("--out-dir", default="backtest-results", help="Directory for summaries and trade CSVs.")
    parser.add_argument("--data-dir", default="data", help="Directory for downloaded/resampled candle CSVs.")
    parser.add_argument("--force-download", action="store_true", help="Ignore cached 5m CSV and call IG again.")
    parser.add_argument("--entry-start-time", default=None, help="Local clock time from which trades may open, e.g. 01:00.")
    parser.add_argument("--entry-end-time", default=None, help="Local clock time before which trades may open, e.g. 06:00.")
    parser.add_argument("--entry-timezone", default="UTC", help="Timezone for entry window. Default: UTC.")
    parser.add_argument("--directions", default="BUY,SELL", help="Comma-separated entry directions to take. Default: BUY,SELL.")
    parser.add_argument(
        "--intrabar-policy",
        choices=["stop_first", "target_first"],
        default="stop_first",
        help="How to handle candles that touch both SL and TP. Default: stop_first.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    epic = args.epic or config.strategy.market_epics[0]
    start = _parse_time(args.from_time)
    end = _parse_time(args.to_time)
    warmup_start = _parse_time(args.warmup_from_time)
    timeframes = _parse_timeframes(args.timeframes)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.source == "ig":
        source_id = "ig"
    elif args.source == "yahoo":
        source_id = f"yahoo_{_safe_filename(args.yahoo_symbol)}"
    else:
        source_id = f"dukascopy_{_safe_filename(args.dukascopy_instrument)}"
    label = f"{source_id}_{_safe_filename(epic)}_{start:%Y%m%d}_{end:%Y%m%d}"
    five_minute_path = data_dir / f"{label}_5m_warmup.csv"
    if args.source == "ig":
        five_minute_candles = _load_or_download_ig_5m(
            five_minute_path,
            epic,
            warmup_start,
            end,
            args.chunk_hours,
            force_download=args.force_download,
        )
    else:
        if args.source == "yahoo":
            five_minute_candles = _load_or_download_yahoo_5m(
                five_minute_path,
                args.yahoo_symbol,
                args.yahoo_interval,
                warmup_start,
                end,
                args.price_scale,
                args.synthetic_spread,
                force_download=args.force_download,
            )
        else:
            five_minute_candles = _load_or_download_dukascopy_5m(
                five_minute_path,
                args.dukascopy_instrument,
                warmup_start,
                end,
                args.price_scale,
                force_download=args.force_download,
            )

    results: dict[str, BacktestResult] = {}
    strategy = SniperStrategy(config.strategy)
    for timeframe in timeframes:
        chart_candles = resample_candles(five_minute_candles, timeframe)
        chart_path = data_dir / f"{label}_{timeframe}m_warmup.csv"
        write_candles_csv(chart_candles, chart_path)

        result = run_backtest(
            chart_candles,
            strategy,
            config.strategy.default_size,
            config.strategy.starting_balance,
            start.isoformat(),
            end.isoformat(),
            rsi5m_candles=five_minute_candles,
            entry_start_time=args.entry_start_time,
            entry_end_time=args.entry_end_time,
            entry_timezone=args.entry_timezone,
            allowed_entry_directions=_parse_directions(args.directions),
            intrabar_policy=args.intrabar_policy,
        )
        results[f"{timeframe}m"] = result
        write_trades_csv(result.trades, out_dir / f"{label}_{timeframe}m_trades.csv")
        write_summary_json(result, out_dir / f"{label}_{timeframe}m_summary.json")

    comparison_path = out_dir / f"{label}_timeframe_comparison.json"
    comparison_path.write_text(json.dumps(_comparison_payload(results), indent=2))
    _print_comparison(results, comparison_path)


def _load_or_download_ig_5m(
    path: Path,
    epic: str,
    start: datetime,
    end: datetime,
    chunk_hours: int,
    *,
    force_download: bool,
) -> list[Candle]:
    if path.exists() and not force_download:
        candles = load_candles_csv(path)
        if _covers_window(candles, start, end):
            LOGGER.info("using cached real 5m candles from %s", path)
            return candles
        LOGGER.warning("cached %s does not cover the requested window; downloading again", path)

    client = IGClient(load_config().ig)
    try:
        candles = _download_range(client, epic, "MINUTE_5", start, end, chunk_hours)
    except IGApiError as error:
        if error.error_code == "error.public-api.exceeded-account-historical-data-allowance":
            raise SystemExit(
                "IG refused the download because the historical data allowance is exhausted. "
                "No June comparison was run because there is no complete cached real 5m dataset."
            ) from error
        raise

    if not candles:
        raise SystemExit("IG returned no candles. No comparison was run.")

    write_candles_csv(candles, path)
    LOGGER.info("wrote %s real 5m candles to %s", len(candles), path)
    return candles


def _load_or_download_yahoo_5m(
    path: Path,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    price_scale: float,
    spread_points: float,
    *,
    force_download: bool,
) -> list[Candle]:
    if path.exists() and not force_download:
        candles = load_candles_csv(path)
        if _covers_window(candles, start, end):
            LOGGER.info("using cached Yahoo 5m candles from %s", path)
            return candles
        LOGGER.warning("cached %s does not cover the requested window; downloading again", path)

    try:
        candles = download_yahoo_forex(
            symbol,
            interval,
            start,
            end,
            price_scale=price_scale,
            spread_points=spread_points,
        )
    except YahooDataError as error:
        raise SystemExit(f"Yahoo download failed: {error}") from error

    if not candles:
        raise SystemExit("Yahoo returned no candles. No comparison was run.")

    write_candles_csv(candles, path)
    LOGGER.info("wrote %s Yahoo 5m candles to %s", len(candles), path)
    return candles


def _load_or_download_dukascopy_5m(
    path: Path,
    instrument: str,
    start: datetime,
    end: datetime,
    price_scale: float,
    *,
    force_download: bool,
) -> list[Candle]:
    if path.exists() and not force_download:
        candles = load_candles_csv(path)
        if _covers_window(candles, start, end):
            LOGGER.info("using cached Dukascopy 5m candles from %s", path)
            return candles
        LOGGER.warning("cached %s does not cover the requested window; downloading again", path)

    try:
        candles = download_dukascopy_5m(instrument, start, end, price_scale=price_scale)
    except DukascopyDataError as error:
        raise SystemExit(f"Dukascopy download failed: {error}") from error

    if not candles:
        raise SystemExit("Dukascopy returned no candles. No comparison was run.")

    write_candles_csv(candles, path)
    LOGGER.info("wrote %s Dukascopy 5m candles to %s", len(candles), path)
    return candles


def _download_range(
    client: IGClient,
    epic: str,
    resolution: str,
    start: datetime,
    end: datetime,
    chunk_hours: int,
) -> list[Candle]:
    from datetime import timedelta

    candles_by_time: dict[datetime, Candle] = {}
    cursor = start
    delta = timedelta(hours=chunk_hours)
    while cursor < end:
        chunk_end = min(cursor + delta, end)
        chunk = client.historical_prices_range(epic, resolution, cursor, chunk_end)
        LOGGER.info("received %s candles for %s -> %s", len(chunk), cursor.isoformat(), chunk_end.isoformat())
        for candle in chunk:
            candles_by_time[candle.timestamp] = candle
        cursor = chunk_end
    return [candles_by_time[key] for key in sorted(candles_by_time)]


def _covers_window(candles: list[Candle], start: datetime, end: datetime) -> bool:
    if not candles:
        return False
    return candles[0].timestamp <= start and candles[-1].timestamp >= end - timedelta(minutes=5)


def _comparison_payload(results: dict[str, BacktestResult]) -> dict[str, dict[str, float | int]]:
    return {
        timeframe: {
            "trades": len(result.trades),
            "net_cash": result.net_cash,
            "profit_percent": result.profit_percent,
            "win_rate": result.win_rate,
            "max_drawdown_cash": result.max_drawdown_cash,
            "tp1_hit_rate": result.tp1_hit_rate,
            "tp2_hit_rate": result.tp2_hit_rate,
            "tp3_hit_rate": result.tp3_hit_rate,
            "ambiguous_exit_candles": result.ambiguous_exit_candles,
        }
        for timeframe, result in results.items()
    }


def _print_comparison(results: dict[str, BacktestResult], comparison_path: Path) -> None:
    print("Timeframe comparison")
    for timeframe, result in results.items():
        print(
            f"{timeframe}: trades={len(result.trades)}, net_cash={result.net_cash:.2f}, "
            f"profit={result.profit_percent:.3f}%, win_rate={result.win_rate:.1f}%, "
            f"max_drawdown={result.max_drawdown_cash:.2f}"
        )
    print(f"Comparison JSON: {comparison_path}")


def _parse_timeframes(value: str) -> list[int]:
    timeframes = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not timeframes:
        raise ValueError("at least one timeframe is required")
    return timeframes


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")


if __name__ == "__main__":
    main()
