from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any

import requests

from sniper_bot.market import Candle


class YahooDataError(RuntimeError):
    pass


def download_yahoo_forex(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    timeout: int = 20,
    price_scale: float = 100.0,
    spread_points: float = 1.0,
) -> list[Candle]:
    start_utc = _to_utc(start)
    end_utc = _to_utc(end)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    response = requests.get(
        url,
        params={
            "period1": int(start_utc.timestamp()),
            "period2": int(end_utc.timestamp()),
            "interval": interval,
            "includePrePost": "true",
        },
        timeout=timeout,
    )
    if not response.ok:
        raise YahooDataError(f"Yahoo request failed {response.status_code}: {response.text[:300]}")

    payload = response.json()
    result = _first_result(payload)
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    candles: list[Candle] = []
    for index, timestamp in enumerate(timestamps):
        values = (
            _at(opens, index),
            _at(highs, index),
            _at(lows, index),
            _at(closes, index),
        )
        if any(value is None for value in values):
            continue
        open_mid, high_mid, low_mid, close_mid = (float(value) * price_scale for value in values if value is not None)
        half_spread = spread_points / 2.0
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(timestamp, tz=timezone.utc),
                open_bid=open_mid - half_spread,
                open_ask=open_mid + half_spread,
                high_bid=high_mid - half_spread,
                high_ask=high_mid + half_spread,
                low_bid=low_mid - half_spread,
                low_ask=low_mid + half_spread,
                close_bid=close_mid - half_spread,
                close_ask=close_mid + half_spread,
                volume=float(_at(volumes, index) or 1),
            )
        )
    return sorted(candles, key=lambda candle: candle.timestamp)


def _first_result(payload: dict[str, Any]) -> dict[str, Any]:
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise YahooDataError(str(error))
    results = chart.get("result") or []
    if not results:
        raise YahooDataError("Yahoo returned no chart result")
    return results[0]


def _at(values: list[Any], index: int) -> Any:
    if index >= len(values):
        return None
    return values[index]


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
