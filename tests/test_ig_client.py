import unittest
from datetime import datetime, timedelta, timezone

from sniper_bot.ig_client import IGClient
from sniper_bot.market import Candle
from sniper_bot.runner import LiveState, _fetch_candles


def price(**times):
    level = {"bid": 1.0, "ask": 1.1}
    return {**times, "openPrice": level, "highPrice": level, "lowPrice": level, "closePrice": level}


class TimestampTests(unittest.TestCase):
    def test_snapshot_time_is_uk_local(self):
        client = IGClient.__new__(IGClient)
        summer = client._parse_price(price(snapshotTime="2026/10/09 21:55:00"))
        winter = client._parse_price(price(snapshotTime="2026/01/09 21:55:00"))
        self.assertEqual(summer.timestamp, datetime(2026, 10, 9, 20, 55, tzinfo=timezone.utc))
        self.assertEqual(winter.timestamp, datetime(2026, 1, 9, 21, 55, tzinfo=timezone.utc))

    def test_snapshot_time_utc_preferred(self):
        client = IGClient.__new__(IGClient)
        candle = client._parse_price(price(snapshotTime="2026/10/09 21:55:00",
                                           snapshotTimeUTC="2026-10-09T20:55:00"))
        self.assertEqual(candle.timestamp, datetime(2026, 10, 9, 20, 55, tzinfo=timezone.utc))


class FakeClient:
    def __init__(self, start):
        self.start, self.now, self.calls = start, 0, []

    def historical_prices(self, epic, resolution, points):
        self.calls.append(points)
        last = self.start + timedelta(minutes=5 * self.now)
        return [Candle(last - timedelta(minutes=5 * i), 1, 1, 1, 1, 1, 1, 1, 1, 1) for i in reversed(range(points))]


class Config:
    class strategy:
        price_points = 120
        resolution = "MINUTE_5"


class IncrementalFetchTests(unittest.TestCase):
    def test_full_fetch_once_then_small_updates(self):
        client, state = FakeClient(datetime(2026, 10, 9, tzinfo=timezone.utc)), LiveState()
        self.assertEqual(len(_fetch_candles(client, "E", Config, state)), 120)
        client.now = 1
        candles = _fetch_candles(client, "E", Config, state)
        self.assertEqual(client.calls, [120, 5])
        self.assertEqual(len(candles), 120)
        self.assertEqual(candles[-1].timestamp, client.start + timedelta(minutes=5))

    def test_gap_triggers_full_refetch(self):
        client, state = FakeClient(datetime(2026, 10, 9, tzinfo=timezone.utc)), LiveState()
        _fetch_candles(client, "E", Config, state)
        client.now = 50
        _fetch_candles(client, "E", Config, state)
        self.assertEqual(client.calls, [120, 5, 120])
