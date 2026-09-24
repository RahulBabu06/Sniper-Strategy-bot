from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from sniper_bot.csv_loader import write_candles_csv
from sniper_bot.dukascopy_client import download_dukascopy_5m


LOGGER = logging.getLogger("sniper_bot.download_dukascopy_prices")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Dukascopy tick data and aggregate it to 5m CSV candles.")
    parser.add_argument("--instrument", default="USDJPY", help="Dukascopy instrument. Default: USDJPY.")
    parser.add_argument("--from-time", required=True, help="Inclusive UTC ISO timestamp.")
    parser.add_argument("--to-time", required=True, help="Exclusive UTC ISO timestamp.")
    parser.add_argument("--out", required=True, help="Output CSV path.")
    parser.add_argument("--price-scale", type=float, default=100.0, help="Scale FX price into spread-bet points.")
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel hourly downloads. Default: 8.")
    parser.add_argument("--timeout", type=int, default=20, help="Per-hour HTTP timeout in seconds. Default: 20.")
    parser.add_argument("--max-attempts", type=int, default=5, help="Per-hour retry attempts. Default: 5.")
    parser.add_argument("--chunk-days", type=int, default=7, help="Download/checkpoint chunk size. Default: 7.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    start = _parse_time(args.from_time)
    end = _parse_time(args.to_time)
    candles_by_time = {}
    output = Path(args.out)
    if output.exists():
        from sniper_bot.csv_loader import load_candles_csv

        for candle in load_candles_csv(output):
            candles_by_time[candle.timestamp] = candle
        if candles_by_time:
            LOGGER.info("loaded %s existing candles from %s", len(candles_by_time), output)

    all_candles = [candles_by_time[key] for key in sorted(candles_by_time)]
    if all_candles:
        LOGGER.info("starting with cached range %s -> %s", all_candles[0].timestamp.isoformat(), all_candles[-1].timestamp.isoformat())

    for chunk_start, chunk_end in _time_chunks(start, end, args.chunk_days):
        if _chunk_already_cached(candles_by_time, chunk_start, chunk_end):
            LOGGER.info("skipping cached %s -> %s", chunk_start.isoformat(), chunk_end.isoformat())
            continue
        LOGGER.info("downloading %s %s -> %s", args.instrument, chunk_start.isoformat(), chunk_end.isoformat())
        candles = _download_valid_chunk(
            args.instrument,
            chunk_start,
            chunk_end,
            args.price_scale,
            args.max_workers,
            args.timeout,
            args.max_attempts,
        )
        LOGGER.info("received %s candles", len(candles))
        if candles:
            for timestamp in [timestamp for timestamp in candles_by_time if chunk_start <= timestamp < chunk_end]:
                del candles_by_time[timestamp]
            for candle in candles:
                candles_by_time[candle.timestamp] = candle
        _write_checkpoint(candles_by_time, output)

    if not candles_by_time:
        raise RuntimeError("Dukascopy returned no candles")
    _write_checkpoint(candles_by_time, output)


def _time_chunks(start: datetime, end: datetime, chunk_days: int):
    cursor = start
    delta = timedelta(days=chunk_days)
    while cursor < end:
        chunk_end = min(cursor + delta, end)
        yield cursor, chunk_end
        cursor = chunk_end


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _download_valid_chunk(
    instrument: str,
    start: datetime,
    end: datetime,
    price_scale: float,
    max_workers: int,
    timeout: int,
    max_attempts: int,
):
    last_candles = []
    for attempt in range(3):
        candles = download_dukascopy_5m(
            instrument,
            start,
            end,
            price_scale=price_scale,
            max_workers=max_workers,
            timeout=timeout,
            max_attempts=max_attempts,
        )
        last_candles = candles
        if _downloaded_chunk_complete(candles, start, end):
            return candles
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(
        f"Incomplete Dukascopy chunk {start.isoformat()} -> {end.isoformat()}: "
        f"received {len(last_candles)} candles"
    )


def _chunk_already_cached(candles_by_time: dict[datetime, object], start: datetime, end: datetime) -> bool:
    if not candles_by_time:
        return False
    keys = [timestamp for timestamp in candles_by_time if start <= timestamp < end]
    if end - start <= _one_day():
        return _daily_chunk_cached(keys, start, end)
    if not keys:
        return False
    first = min(keys)
    last = max(keys)
    return first <= start + _three_days() and last >= end - _three_days()


def _write_checkpoint(candles_by_time: dict[datetime, object], output: Path) -> None:
    all_candles = [candles_by_time[key] for key in sorted(candles_by_time)]
    write_candles_csv(all_candles, output)
    LOGGER.info(
        "checkpoint wrote %s candles to %s (%s -> %s)",
        len(all_candles),
        output,
        all_candles[0].timestamp.isoformat(),
        all_candles[-1].timestamp.isoformat(),
    )


def _five_minutes():
    from datetime import timedelta

    return timedelta(minutes=5)


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def _three_days():
    from datetime import timedelta

    return timedelta(days=3)


def _daily_chunk_cached(keys: list[datetime], start: datetime, end: datetime) -> bool:
    weekday = start.weekday()
    if weekday == 5:
        return True
    if not keys:
        return False
    if _known_low_liquidity_holiday(start):
        return max(keys) >= end - timedelta(minutes=10)
    if len(keys) >= 200:
        return True
    last = max(keys)
    if weekday == 4:
        return False
    if weekday == 6:
        return last >= end - timedelta(minutes=10)
    return False


def _known_low_liquidity_holiday(start: datetime) -> bool:
    return (start.month, start.day) in {(1, 1), (12, 25)}


def _downloaded_chunk_complete(candles: list[object], start: datetime, end: datetime) -> bool:
    keys = [candle.timestamp for candle in candles]
    if end - start <= _one_day():
        return _daily_chunk_cached(keys, start, end)
    if not keys:
        return False
    return min(keys) <= start + _three_days() and max(keys) >= end - _three_days()


if __name__ == "__main__":
    main()
