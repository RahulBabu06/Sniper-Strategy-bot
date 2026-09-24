from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sniper_bot.config import load_config
from sniper_bot.csv_loader import load_candles_csv
from sniper_bot.khansaab_indicator import calculate_khansaab_bars


FIELDS = [
    "timestamp",
    "status",
    "trend_strength",
    "bull_score",
    "bear_score",
    "market_bias",
    "entry",
    "sl",
    "tp1",
    "tp2",
    "tp3",
    "tp4",
    "tp5",
    "ema9",
    "ema21",
    "ema50",
    "vwap",
    "atr",
    "rsi",
    "rsi5m",
    "macd_main",
    "macd_signal",
    "adx",
    "volume_average",
    "last_signal_state",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "tp4_hit",
    "tp5_hit",
    "is_retest",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export KhanSaab indicator values for every candle in a CSV.")
    parser.add_argument("csv_path", help="Input candle CSV.")
    parser.add_argument("--out", required=True, help="Output indicator CSV.")
    args = parser.parse_args()

    config = load_config()
    candles = load_candles_csv(args.csv_path)
    bars = calculate_khansaab_bars(candles, config.strategy)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "timestamp": bar.candle.timestamp.isoformat(),
                    "status": bar.status,
                    "trend_strength": bar.trend_strength,
                    "bull_score": _number(bar.bull_score),
                    "bear_score": _number(bar.bear_score),
                    "market_bias": bar.bias,
                    "entry": _number(bar.entry_price),
                    "sl": _number(bar.stop_price),
                    "tp1": _number(bar.targets[0]),
                    "tp2": _number(bar.targets[1]),
                    "tp3": _number(bar.targets[2]),
                    "tp4": _number(bar.targets[3]),
                    "tp5": _number(bar.targets[4]),
                    "ema9": _number(bar.ema9),
                    "ema21": _number(bar.ema21),
                    "ema50": _number(bar.ema50),
                    "vwap": _number(bar.vwap),
                    "atr": _number(bar.atr),
                    "rsi": _number(bar.rsi),
                    "rsi5m": _number(bar.rsi5m),
                    "macd_main": _number(bar.macd_main),
                    "macd_signal": _number(bar.macd_signal),
                    "adx": _number(bar.adx),
                    "volume_average": _number(bar.volume_average),
                    "last_signal_state": bar.last_signal_state,
                    "tp1_hit": bar.target_hits[0],
                    "tp2_hit": bar.target_hits[1],
                    "tp3_hit": bar.target_hits[2],
                    "tp4_hit": bar.target_hits[3],
                    "tp5_hit": bar.target_hits[4],
                    "is_retest": bar.is_retest,
                }
            )
    print(f"Wrote {len(bars)} indicator rows to {output}")


def _number(value: float) -> str:
    return f"{value:.10g}"


if __name__ == "__main__":
    main()
