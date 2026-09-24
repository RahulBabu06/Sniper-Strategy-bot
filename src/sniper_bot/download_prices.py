from __future__ import annotations

import argparse
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sniper_bot.config import load_config
from sniper_bot.csv_loader import write_candles_csv


LOGGER = logging.getLogger("sniper_bot.download_prices")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download IG historical prices to CSV.")
    parser.add_argument(
        "epic",
        nargs="?",
        help="IG market EPIC. Defaults to the first value in MARKET_EPICS.",
    )
    parser.add_argument(
        "--resolution",
        default="MINUTE_5",
        help="IG price resolution. Default: MINUTE_5.",
    )
    parser.add_argument(
        "--points",
        type=int,
        default=1000,
        help="Number of candles to request. Default: 1000.",
    )
    parser.add_argument(
        "--from-time",
        default=None,
        help="Inclusive UTC ISO timestamp for range downloads, e.g. 2026-05-31T23:00:00+00:00.",
    )
    parser.add_argument(
        "--to-time",
        default=None,
        help="Exclusive UTC ISO timestamp for range downloads, e.g. 2026-06-30T23:00:00+00:00.",
    )
    parser.add_argument(
        "--chunk-hours",
        type=int,
        default=24,
        help="Chunk size for range downloads. Default: 24.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path. Default: data/<epic>_<resolution>.csv.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from sniper_bot.ig_client import IGClient

    config = load_config()
    epic = args.epic or config.strategy.market_epics[0]
    output = args.out or f"data/{_safe_filename(epic)}_{args.resolution.lower()}.csv"

    client = IGClient(config.ig)
    if args.from_time and args.to_time:
        start = _parse_time(args.from_time)
        end = _parse_time(args.to_time)
        LOGGER.info(
            "downloading range epic=%s resolution=%s start=%s end=%s chunk_hours=%s",
            epic,
            args.resolution,
            start.isoformat(),
            end.isoformat(),
            args.chunk_hours,
        )
        candles = _download_range(client, epic, args.resolution, start, end, args.chunk_hours)
    else:
        LOGGER.info("downloading epic=%s resolution=%s points=%s", epic, args.resolution, args.points)
        candles = client.historical_prices(epic, args.resolution, args.points)
    if not candles:
        raise RuntimeError("IG returned no candles for this request")

    write_candles_csv(candles, output)
    first = candles[0].timestamp.isoformat()
    last = candles[-1].timestamp.isoformat()
    LOGGER.info("wrote %s candles to %s (%s -> %s)", len(candles), output, first, last)


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")


def _download_range(client: IGClient, epic: str, resolution: str, start: datetime, end: datetime, chunk_hours: int):
    candles_by_time = {}
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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    main()
