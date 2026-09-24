from __future__ import annotations

import argparse
import json
from pathlib import Path

from sniper_bot.backtest import chronological_split_bounds
from sniper_bot.backtest import print_summary
from sniper_bot.backtest import run_backtest
from sniper_bot.backtest import write_trades_csv
from sniper_bot.backtest import write_results_csv, summary_dict
from sniper_bot.config import load_config
from sniper_bot.csv_loader import load_candles_csv
from sniper_bot.strategy import Direction, SniperStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a chronological train/test split backtest.")
    parser.add_argument("csv_path", help="CSV with timestamp/open/high/low/close columns and optional volume/spread.")
    parser.add_argument("--spread", type=float, default=0.0, help="Default spread if the CSV has no spread column.")
    parser.add_argument("--size", type=float, default=None, help="Cash value per point. Defaults to DEFAULT_SIZE.")
    parser.add_argument("--from-time", default=None, help="Inclusive ISO timestamp for the split window.")
    parser.add_argument("--to-time", default=None, help="Exclusive ISO timestamp for the split window.")
    parser.add_argument("--train-fraction", type=float, default=0.8, help="Chronological train fraction. Default: 0.8.")
    parser.add_argument("--rsi5m-csv", default=None, help="Optional 5-minute CSV used for KhanSaab's 5m RSI.")
    parser.add_argument("--entry-start-time", default=None, help="Local clock time from which trades may open.")
    parser.add_argument("--entry-end-time", default=None, help="Local clock time before which trades may open.")
    parser.add_argument("--entry-timezone", default="UTC", help="Timezone for entry window. Default: UTC.")
    parser.add_argument("--directions", default="BUY,SELL", help="Comma-separated entry directions to take.")
    parser.add_argument("--min-entry-volume", type=float, default=None,
                        help="Require entry candle volume strictly greater than this value.")
    parser.add_argument("--reverse-on-opposite-signal", action="store_true",
                        help="Close and reverse before the fixed bracket completes.")
    parser.add_argument(
        "--intrabar-policy",
        choices=["stop_first", "target_first"],
        default="stop_first",
        help="How to handle candles that touch both SL and TP. Default: stop_first.",
    )
    parser.add_argument("--out-dir", default="backtest-results/split", help="Directory for split outputs.")
    args = parser.parse_args()

    config = load_config()
    candles = load_candles_csv(args.csv_path, default_spread=args.spread)
    rsi5m_candles = load_candles_csv(args.rsi5m_csv, default_spread=args.spread) if args.rsi5m_csv else None
    train_start, split_time, test_end = chronological_split_bounds(
        candles,
        args.train_fraction,
        args.from_time,
        args.to_time,
    )

    strategy = SniperStrategy(config.strategy)
    size = args.size or config.strategy.default_size
    directions = _parse_directions(args.directions)

    train = run_backtest(
        candles,
        strategy,
        size,
        config.strategy.starting_balance,
        train_start.isoformat(),
        split_time.isoformat(),
        rsi5m_candles,
        args.entry_start_time,
        args.entry_end_time,
        args.entry_timezone,
        directions,
        args.intrabar_policy,
        min_entry_volume=args.min_entry_volume,
        reverse_on_opposite_signal=args.reverse_on_opposite_signal,
    )
    test = run_backtest(
        candles,
        strategy,
        size,
        config.strategy.starting_balance,
        split_time.isoformat(),
        args.to_time,
        rsi5m_candles,
        args.entry_start_time,
        args.entry_end_time,
        args.entry_timezone,
        directions,
        args.intrabar_policy,
        min_entry_volume=args.min_entry_volume,
        reverse_on_opposite_signal=args.reverse_on_opposite_signal,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_trades_csv(train.trades, out_dir / "train_trades.csv")
    write_trades_csv(test.trades, out_dir / "test_trades.csv")
    write_results_csv(train, out_dir / "train" / "backtest_results.csv")
    write_results_csv(test, out_dir / "test" / "backtest_results.csv")
    _write_split_summary(out_dir / "summary.json", args.train_fraction, train_start, split_time, test_end, train, test)

    print(f"Train window: {train_start.isoformat()} -> {split_time.isoformat()}")
    print_summary(train)
    print()
    print(f"Test window:  {split_time.isoformat()} -> {(args.to_time or test_end.isoformat())}")
    print_summary(test)


def _parse_directions(value: str) -> set[Direction]:
    directions: set[Direction] = set()
    for item in value.split(","):
        name = item.strip().upper()
        if not name:
            continue
        if name not in {"BUY", "SELL"}:
            raise ValueError("--directions must contain BUY, SELL, or both")
        directions.add(Direction(name))
    if not directions:
        raise ValueError("--directions must contain at least one direction")
    return directions


def _write_split_summary(path: Path, train_fraction, train_start, split_time, test_end, train, test) -> None:
    payload = {
        "train_fraction": train_fraction,
        "test_fraction": 1 - train_fraction,
        "train_start": train_start.isoformat(),
        "split_time": split_time.isoformat(),
        "test_end": test_end.isoformat(),
        "train": summary_dict(train),
        "test": summary_dict(test),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
