from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sniper_bot.market import Candle


def resample_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    if minutes <= 0:
        raise ValueError("minutes must be greater than zero")
    if 60 % minutes != 0:
        raise ValueError("minutes must divide evenly into an hour")

    sorted_candles = sorted(candles, key=lambda candle: candle.timestamp)
    buckets: dict[datetime, list[Candle]] = {}
    for candle in sorted_candles:
        bucket_time = _floor_time(candle.timestamp, minutes)
        buckets.setdefault(bucket_time, []).append(candle)

    return [_combine(bucket_time, buckets[bucket_time]) for bucket_time in sorted(buckets)]


def _floor_time(value: datetime, minutes: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    floored_minute = (value.minute // minutes) * minutes
    return value.replace(minute=floored_minute, second=0, microsecond=0)


def _combine(timestamp: datetime, candles: list[Candle]) -> Candle:
    if not candles:
        raise ValueError("cannot combine an empty candle bucket")
    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    first = ordered[0]
    last = ordered[-1]
    return Candle(
        timestamp=timestamp,
        open_bid=first.open_bid,
        open_ask=first.open_ask,
        high_bid=max(candle.high_bid for candle in ordered),
        high_ask=max(candle.high_ask for candle in ordered),
        low_bid=min(candle.low_bid for candle in ordered),
        low_ask=min(candle.low_ask for candle in ordered),
        close_bid=last.close_bid,
        close_ask=last.close_ask,
        volume=sum(candle.volume for candle in ordered),
    )
