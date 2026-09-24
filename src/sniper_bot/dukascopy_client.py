from __future__ import annotations

import lzma
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import requests

from sniper_bot.market import Candle
from sniper_bot.resample import resample_candles


class DukascopyDataError(RuntimeError):
    pass


def download_dukascopy_5m(
    instrument: str,
    start: datetime,
    end: datetime,
    *,
    timeout: int = 20,
    price_scale: float = 100.0,
    max_workers: int = 8,
    max_attempts: int = 5,
) -> list[Candle]:
    ticks = _download_ticks(
        instrument,
        start,
        end,
        timeout=timeout,
        max_workers=max_workers,
        max_attempts=max_attempts,
    )
    one_minute = _ticks_to_one_minute_candles(ticks, price_scale)
    return resample_candles(one_minute, 5)


def _download_ticks(
    instrument: str,
    start: datetime,
    end: datetime,
    *,
    timeout: int,
    max_workers: int,
    max_attempts: int,
) -> list[tuple[datetime, float, float, float]]:
    start = _to_utc(start).replace(minute=0, second=0, microsecond=0)
    end = _to_utc(end)
    hours: list[datetime] = []
    cursor = start
    while cursor < end:
        hours.append(cursor)
        cursor += timedelta(hours=1)

    def download(hour: datetime) -> list[tuple[datetime, float, float, float]]:
        session = requests.Session()
        return _download_hour(session, instrument, hour, timeout=timeout, max_attempts=max_attempts)

    ticks: list[tuple[datetime, float, float, float]] = []
    workers = max(1, max_workers)
    if workers == 1:
        session = requests.Session()
        for hour in hours:
            try:
                ticks.extend(_download_hour(session, instrument, hour, timeout=timeout, max_attempts=max_attempts))
            except DukascopyDataError:
                continue
            except requests.RequestException:
                continue
        return [tick for tick in ticks if start <= tick[0] < end]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download, hour) for hour in hours]
        for future in as_completed(futures):
            try:
                ticks.extend(future.result())
            except DukascopyDataError:
                continue
            except requests.RequestException:
                continue
    return [tick for tick in ticks if start <= tick[0] < end]


def _download_hour(
    session: requests.Session,
    instrument: str,
    hour: datetime,
    *,
    timeout: int,
    max_attempts: int = 5,
) -> list[tuple[datetime, float, float, float]]:
    url = _hour_url(instrument, hour)
    response = None
    for attempt in range(max(1, max_attempts)):
        response = session.get(url, timeout=timeout)
        if response.status_code != 429:
            break
        time.sleep(min(2.0 * (attempt + 1), 5.0))
    if response is None:
        return []
    if response.status_code == 404:
        return []
    if not response.ok:
        raise DukascopyDataError(f"Dukascopy request failed {response.status_code}: {url}")
    if not response.content:
        return []

    try:
        payload = lzma.decompress(response.content)
    except lzma.LZMAError as error:
        raise DukascopyDataError(f"Could not decompress Dukascopy BI5 payload: {url}") from error

    divisor = _price_divisor(instrument)
    ticks: list[tuple[datetime, float, float, float]] = []
    record_size = 20
    for offset in range(0, len(payload) - record_size + 1, record_size):
        millis, ask_raw, bid_raw, ask_volume, bid_volume = struct.unpack(">IIIff", payload[offset : offset + record_size])
        timestamp = hour + timedelta(milliseconds=millis)
        ask = ask_raw / divisor
        bid = bid_raw / divisor
        ticks.append((timestamp, bid, ask, float(ask_volume + bid_volume)))
    return ticks


def _ticks_to_one_minute_candles(
    ticks: list[tuple[datetime, float, float, float]],
    price_scale: float,
) -> list[Candle]:
    buckets: dict[datetime, list[tuple[datetime, float, float, float]]] = {}
    for timestamp, bid, ask, volume in ticks:
        bucket = timestamp.replace(second=0, microsecond=0)
        buckets.setdefault(bucket, []).append((timestamp, bid * price_scale, ask * price_scale, volume))

    candles: list[Candle] = []
    for bucket in sorted(buckets):
        bucket_ticks = sorted(buckets[bucket], key=lambda item: item[0])
        first = bucket_ticks[0]
        last = bucket_ticks[-1]
        bid_values = [item[1] for item in bucket_ticks]
        ask_values = [item[2] for item in bucket_ticks]
        candles.append(
            Candle(
                timestamp=bucket,
                open_bid=first[1],
                open_ask=first[2],
                high_bid=max(bid_values),
                high_ask=max(ask_values),
                low_bid=min(bid_values),
                low_ask=min(ask_values),
                close_bid=last[1],
                close_ask=last[2],
                volume=sum(item[3] for item in bucket_ticks),
            )
        )
    return candles


def _hour_url(instrument: str, hour: datetime) -> str:
    normalized = instrument.replace("/", "").upper()
    month_zero_based = hour.month - 1
    return (
        "https://datafeed.dukascopy.com/datafeed/"
        f"{normalized}/{hour.year}/{month_zero_based:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def _price_divisor(instrument: str) -> float:
    normalized = instrument.replace("/", "").upper()
    if normalized.endswith("JPY"):
        return 1000.0
    return 100000.0


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
