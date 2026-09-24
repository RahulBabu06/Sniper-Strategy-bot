from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from sniper_bot.market import Candle


CSV_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "open_bid",
    "open_ask",
    "high_bid",
    "high_ask",
    "low_bid",
    "low_ask",
    "close_bid",
    "close_ask",
    "spread",
    "volume",
]


def write_candles_csv(candles: list[Candle], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": candle.timestamp.isoformat(),
                    "open": _format_number(candle.open_mid),
                    "high": _format_number(candle.high_mid),
                    "low": _format_number(candle.low_mid),
                    "close": _format_number(candle.close_mid),
                    "open_bid": _format_number(candle.open_bid),
                    "open_ask": _format_number(candle.open_ask),
                    "high_bid": _format_number(candle.high_bid),
                    "high_ask": _format_number(candle.high_ask),
                    "low_bid": _format_number(candle.low_bid),
                    "low_ask": _format_number(candle.low_ask),
                    "close_bid": _format_number(candle.close_bid),
                    "close_ask": _format_number(candle.close_ask),
                    "spread": _format_number(candle.spread),
                    "volume": _format_number(candle.volume),
                }
            )


def load_candles_csv(path: str | Path, default_spread: float = 0.0) -> list[Candle]:
    candles: list[Candle] = []
    with Path(path).open(newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            normalized = {_normalize_key(key): value for key, value in row.items() if key is not None}
            timestamp = _parse_timestamp(_first(normalized, "timestamp", "time", "date", "datetime"))
            open_mid = _number(_first(normalized, "open", "open_mid", "o"))
            high_mid = _number(_first(normalized, "high", "high_mid", "h"))
            low_mid = _number(_first(normalized, "low", "low_mid", "l"))
            close_mid = _number(_first(normalized, "close", "close_mid", "c"))
            volume = _number(_first(normalized, "volume", "vol", default="1"))
            spread = _number(_first(normalized, "spread", default=str(default_spread)))
            half_spread = spread / 2
            candles.append(
                Candle(
                    timestamp=timestamp,
                    open_bid=_number(_first(normalized, "open_bid", default=str(open_mid - half_spread))),
                    open_ask=_number(_first(normalized, "open_ask", default=str(open_mid + half_spread))),
                    high_bid=_number(_first(normalized, "high_bid", default=str(high_mid - half_spread))),
                    high_ask=_number(_first(normalized, "high_ask", default=str(high_mid + half_spread))),
                    low_bid=_number(_first(normalized, "low_bid", default=str(low_mid - half_spread))),
                    low_ask=_number(_first(normalized, "low_ask", default=str(low_mid + half_spread))),
                    close_bid=_number(_first(normalized, "close_bid", default=str(close_mid - half_spread))),
                    close_ask=_number(_first(normalized, "close_ask", default=str(close_mid + half_spread))),
                    volume=volume,
                )
            )
    return sorted(candles, key=lambda candle: candle.timestamp)


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _first(row: dict[str, str], *keys: str, default: str | None = None) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    if default is not None:
        return default
    raise ValueError(f"CSV is missing required column: one of {', '.join(keys)}")


def _number(value: str) -> float:
    return float(value.replace(",", "").strip())


def _format_number(value: float) -> str:
    return f"{value:.10g}"


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
