from datetime import datetime, timedelta, timezone
import unittest

from sniper_bot.market import Candle
from sniper_bot.runner import _completed_candles, _live_limit_level, _resolution_duration
from sniper_bot.strategy import Direction, Signal


class RunnerTests(unittest.TestCase):
    def test_only_completed_candles_are_evaluated(self):
        start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        candles = [Candle(start + timedelta(minutes=5 * i), 1, 1, 1, 1, 1, 1, 1, 1)
                   for i in range(3)]

        actual = _completed_candles(candles, "MINUTE_5", start + timedelta(minutes=12))

        self.assertEqual([item.timestamp for item in actual], [start, start + timedelta(minutes=5)])

    def test_known_resolution_duration(self):
        self.assertEqual(_resolution_duration("MINUTE_15"), timedelta(minutes=15))

    def test_live_target_matches_backtest_allocation(self):
        signal = Signal(Direction.BUY, "test", target_prices=(101, 102, 103))
        self.assertEqual(_live_limit_level(signal, [1, 0, 0]), 101)

    def test_live_rejects_unimplementable_partial_target_plan(self):
        signal = Signal(Direction.BUY, "test", target_prices=(101, 102, 103))
        with self.assertRaises(ValueError):
            _live_limit_level(signal, [0.5, 0.25, 0.25])


if __name__ == "__main__":
    unittest.main()
