from __future__ import annotations

import argparse
import json

from sniper_bot.config import load_config
from sniper_bot.csv_loader import load_candles_csv
from sniper_bot.khansaab_indicator import calculate_khansaab_bars


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the latest KhanSaab indicator output from a candle CSV.")
    parser.add_argument("csv_path", help="CSV with timestamp/open/high/low/close columns.")
    args = parser.parse_args()

    config = load_config()
    candles = load_candles_csv(args.csv_path)
    bars = calculate_khansaab_bars(candles, config.strategy)
    if not bars:
        raise RuntimeError("not enough candles to calculate KhanSaab output")
    print(json.dumps(format_output(bars[-1]), indent=2))


def format_output(bar) -> dict[str, object]:
    return {
        "Status": bar.status,
        "Trend Strength": bar.trend_strength,
        "BULL SCORE": round(bar.bull_score, 2),
        "BEAR SCORE": round(bar.bear_score, 2),
        "MARKET BIAS": bar.bias,
        "SL": bar.stop_price,
        "Entry": bar.entry_price,
        "TP1": bar.targets[0],
        "TP2": bar.targets[1],
        "TP3": bar.targets[2],
        "TP4": bar.targets[3],
        "TP5": bar.targets[4],
    }


if __name__ == "__main__":
    main()
