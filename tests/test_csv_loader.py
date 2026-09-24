from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sniper_bot.csv_loader import load_candles_csv, write_candles_csv
from sniper_bot.market import Candle


class CsvLoaderTests(unittest.TestCase):
    def test_write_and_load_candles(self) -> None:
        candle = Candle(
            timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            open_bid=1.0999,
            open_ask=1.1001,
            high_bid=1.1019,
            high_ask=1.1021,
            low_bid=1.0989,
            low_ask=1.0991,
            close_bid=1.1009,
            close_ask=1.1011,
            volume=42,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            write_candles_csv([candle], path)
            loaded = load_candles_csv(path)

        self.assertEqual(len(loaded), 1)
        self.assertAlmostEqual(loaded[0].close_mid, candle.close_mid)
        self.assertAlmostEqual(loaded[0].spread, candle.spread)
        self.assertEqual(loaded[0].volume, 42)


if __name__ == "__main__":
    unittest.main()
