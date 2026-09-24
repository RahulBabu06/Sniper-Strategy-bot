import math
import unittest
from datetime import datetime, timedelta, timezone

from sniper_bot.indicators import adx, atr, rsi, session_vwap, sma
from sniper_bot.market import Candle


class IndicatorCausalityTests(unittest.TestCase):
    def candles(self, count=80):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = []
        for i in range(count):
            price = 100 + i * 0.2 + (i % 3) * 0.1
            result.append(Candle(start + timedelta(minutes=5 * i), price, price,
                                 price + 0.5, price + 0.5, price - 0.2, price - 0.2, price, price))
        return result

    def test_future_candles_do_not_change_earlier_indicator_values(self):
        candles = self.candles()
        for function in (atr, adx):
            full = function(candles, 14)
            for end in (1, 10, 14, 20, 28, 40, 60):
                prefix = function(candles[:end], 14)
                self.assertEqual(len(prefix), end)
                for actual, expected in zip(prefix, full[:end]):
                    if math.isnan(expected):
                        self.assertTrue(math.isnan(actual))
                    else:
                        self.assertAlmostEqual(actual, expected)

    def test_atr_includes_first_candle_range(self):
        candles = self.candles(1) * 14
        values = atr(candles, 14)
        self.assertTrue(all(math.isnan(value) for value in values[:13]))
        self.assertAlmostEqual(values[13], 0.7)

    def test_rsi_and_sma_warmup_is_unavailable(self):
        self.assertTrue(all(math.isnan(value) for value in rsi([1, 2, 3], 14)))
        values = sma([1, 2, 3], 3)
        self.assertTrue(math.isnan(values[1]))
        self.assertEqual(values[2], 2)

    def test_vwap_preserves_fractional_and_zero_volume(self):
        from dataclasses import replace
        candles = [replace(candle, open_bid=p, open_ask=p, high_bid=p, high_ask=p,
                           low_bid=p, low_ask=p, close_bid=p, close_ask=p, volume=v)
                   for candle, p, v in zip(self.candles(3), [100, 200, 900], [0.1, 0.2, 0])]
        values = session_vwap(candles)
        self.assertAlmostEqual(values[1], (100 * 0.1 + 200 * 0.2) / 0.3)
        self.assertAlmostEqual(values[2], values[1])
