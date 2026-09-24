from __future__ import annotations

import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

from sniper_bot.backtest import OpenTrade
from sniper_bot.backtest import chronological_split_bounds
from sniper_bot.backtest import _entry_time_allowed
from sniper_bot.backtest import _parse_optional_clock
from sniper_bot.backtest import _pnl_points
from sniper_bot.backtest import _summarize
from sniper_bot.backtest import _update_open_trade
from sniper_bot.backtest import filter_candles
from sniper_bot.backtest import write_summary_json
from sniper_bot.market import Candle
from sniper_bot.strategy import Direction, Signal


class BacktestTests(unittest.TestCase):
    def test_buy_pnl_points(self) -> None:
        self.assertAlmostEqual(_pnl_points(Direction.BUY, 1.1000, 1.1015), 0.0015)

    def test_sell_pnl_points(self) -> None:
        self.assertAlmostEqual(_pnl_points(Direction.SELL, 1.1000, 1.0980), 0.0020)

    def test_empty_summary(self) -> None:
        result = _summarize([])
        self.assertEqual(result.net_cash, 0)
        self.assertEqual(result.win_rate, 0)

    def test_summary_json_is_written(self) -> None:
        result = _summarize([])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            write_summary_json(result, path)
            payload = json.loads(path.read_text())

        self.assertEqual(payload["net_cash"], 0)
        self.assertEqual(payload["trades"], [])

    def test_filter_candles(self) -> None:
        candles = [
            Candle(datetime(2026, 7, 26, 22, 55, tzinfo=timezone.utc), 1, 1, 1, 1, 1, 1, 1, 1),
            Candle(datetime(2026, 7, 26, 23, 0, tzinfo=timezone.utc), 2, 2, 2, 2, 2, 2, 2, 2),
            Candle(datetime(2026, 7, 27, 23, 0, tzinfo=timezone.utc), 3, 3, 3, 3, 3, 3, 3, 3),
        ]
        filtered = filter_candles(candles, "2026-07-26T23:00:00+00:00", "2026-07-27T23:00:00+00:00")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].open_mid, 2)

    def test_chronological_split_bounds_are_in_order(self) -> None:
        candles = [
            Candle(datetime(2026, 1, 1, index, tzinfo=timezone.utc), 1, 1, 1, 1, 1, 1, 1, 1)
            for index in range(10)
        ]
        train_start, split_time, test_end = chronological_split_bounds(candles, train_fraction=0.8)

        self.assertEqual(train_start, candles[0].timestamp)
        self.assertEqual(split_time, candles[8].timestamp)
        self.assertEqual(test_end, candles[-1].timestamp)

    def test_entry_time_window_uses_local_timezone(self) -> None:
        start = _parse_optional_clock("01:00")
        end = _parse_optional_clock("06:00")
        zone = ZoneInfo("Europe/London")

        self.assertTrue(
            _entry_time_allowed(datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc), start, end, zone)
        )
        self.assertFalse(
            _entry_time_allowed(datetime(2026, 5, 1, 5, 0, tzinfo=timezone.utc), start, end, zone)
        )

    def test_tp1_can_move_remaining_position_to_break_even(self) -> None:
        signal = Signal(
            direction=Direction.BUY,
            reason="test",
            entry_price=100.0,
            stop_price=99.0,
            target_prices=(101.0, 102.0, 103.0),
        )
        trade = OpenTrade(
            signal=signal,
            entry_time="2026-01-01T00:00:00+00:00",
            remaining_fraction=1.0,
            stop_price=99.0,
            tp_hits=[False, False, False],
            realized_points=0.0,
            exit_time="",
            exit_price=100.0,
            exit_reason="",
        )

        _update_open_trade(
            Candle(datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), 100, 100, 101, 101, 100, 100, 101, 101),
            trade,
            [0.5, 0.25, 0.25],
            True,
            "stop_first",
        )
        self.assertEqual(trade.stop_price, 100.0)
        self.assertEqual(trade.remaining_fraction, 0.5)

        _update_open_trade(
            Candle(datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc), 101, 101, 101, 101, 100, 100, 100, 100),
            trade,
            [0.5, 0.25, 0.25],
            True,
            "stop_first",
        )
        self.assertEqual(trade.exit_reason, "SL")
        self.assertEqual(trade.remaining_fraction, 0)
        self.assertEqual(trade.realized_points, 0.5)


if __name__ == "__main__":
    unittest.main()
