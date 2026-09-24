from __future__ import annotations

import unittest
from datetime import datetime
from datetime import timezone

from sniper_bot.market import Candle
from sniper_bot.resample import resample_candles


class ResampleTests(unittest.TestCase):
    def test_resample_two_5m_candles_to_10m(self) -> None:
        candles = [
            Candle(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc), 10, 12, 14, 16, 8, 9, 11, 13, 100),
            Candle(datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc), 20, 22, 24, 26, 6, 7, 21, 23, 150),
        ]

        resampled = resample_candles(candles, 10)

        self.assertEqual(len(resampled), 1)
        self.assertEqual(resampled[0].timestamp, datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(resampled[0].open_bid, 10)
        self.assertEqual(resampled[0].open_ask, 12)
        self.assertEqual(resampled[0].high_bid, 24)
        self.assertEqual(resampled[0].high_ask, 26)
        self.assertEqual(resampled[0].low_bid, 6)
        self.assertEqual(resampled[0].low_ask, 7)
        self.assertEqual(resampled[0].close_bid, 21)
        self.assertEqual(resampled[0].close_ask, 23)
        self.assertEqual(resampled[0].volume, 250)


if __name__ == "__main__":
    unittest.main()
