import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from sniper_bot.backtest import run_backtest, write_results_csv
from sniper_bot.config import load_config
from sniper_bot.khansaab_indicator import calculate_khansaab_bars
from sniper_bot.market import Candle
from sniper_bot.strategy import SniperStrategy


class CandleResultsTests(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {}, clear=True), patch("sniper_bot.config.load_env_file"):
            self.config = replace(load_config().strategy, spread_bet_point_size=1.0,
                                  min_bear_score_short=60, take_profit_allocations=[0.5, 0.25, 0.25])
        base = self.candle(0, 100, 100, 100)
        self.template = calculate_khansaab_bars([base] * 40, self.config)[0]

    def candle(self, index, high, low, close):
        timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index * 5)
        return Candle(timestamp, close, close, high, high, low, low, close, close)

    def bar(self, index, high=100, low=100, close=100, direction=None):
        short = direction == "SELL"
        return replace(self.template, index=index, candle=self.candle(index, high, low, close),
                       atr=1.0, adx=30.0, rsi=60.0, rsi5m=60.0, volume_average=1.0,
                       status=direction or "WAIT", trigger_buy=direction == "BUY", trigger_sell=short,
                       bull_score=0 if short else 100, bear_score=100 if short else 0,
                       entry_price=100, stop_price=101 if short else 99,
                       targets=(99, 98, 97, 96, 95) if short else (101, 102, 103, 104, 105))

    def run_bars(self, bars, **kwargs):
        with patch("sniper_bot.backtest.calculate_khansaab_bars", return_value=bars):
            return run_backtest([bar.candle for bar in bars], SniperStrategy(self.config), 10, 10000, **kwargs)

    def test_partial_exit_returns_and_order_lifecycle_for_both_sides(self):
        for direction, high, low, close, stop_high, stop_low in [
            ("BUY", 101, 100, 100.5, 100, 99),
            ("SELL", 100, 99, 99.5, 101, 100),
        ]:
            with self.subTest(direction=direction):
                result = self.run_bars([self.bar(0), self.bar(1, direction=direction),
                                        self.bar(2, high, low, close),
                                        self.bar(3, stop_high, stop_low, 100), self.bar(4)])
                frame = result.dataframe
                self.assertEqual(len(frame), 5)
                self.assertEqual(frame.iloc[1].position, 1 if direction == "BUY" else -1)
                partial = frame.iloc[2]
                self.assertEqual(partial.stake_per_point, 5)
                self.assertEqual(partial.position_realized_pnl_cash, 5)
                self.assertEqual(partial.position_unrealized_pnl_cash, 2.5)
                self.assertAlmostEqual(partial.position_return, 0.0075)
                self.assertAlmostEqual(partial.account_return, 0.00075)
                self.assertAlmostEqual(frame.iloc[3].account_return, 10000 / 10007.5 - 1)
                self.assertEqual(frame.iloc[3].cumulative_account_return, 0)
                self.assertEqual(frame.iloc[3].position, 0)
                self.assertAlmostEqual(frame.iloc[3].position_return, 0)
                self.assertEqual(frame.iloc[-1].equity, 10000 + result.net_cash)
                exits = frame.iloc[3].orders
                self.assertEqual({e["kind"] for e in exits if e["status"] == "CANCELLED"}, {"TP2", "TP3"})
                self.assertEqual(frame.iloc[3].active_orders, [])
                self.assertEqual(len(result.trades), 1)

    def test_multiple_targets_and_csv_round_trip(self):
        result = self.run_bars([self.bar(0), self.bar(1, direction="BUY"), self.bar(2, 103, 100, 103)])
        final = result.dataframe.iloc[-1]
        fills = [e for e in final.orders if e["status"] == "FILLED"]
        self.assertEqual([e["kind"] for e in fills], ["TP1", "TP2", "TP3"])
        self.assertEqual(sum(e["fraction"] for e in fills), 1)
        self.assertAlmostEqual(final.position_return, 0.0175)
        self.assertEqual(final.equity, 10017.5)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "backtest_results.csv"
            write_results_csv(result, path)
            frame = pd.read_csv(path)
            self.assertEqual(json.loads(frame.iloc[-1].orders), final.orders)
            self.assertAlmostEqual(frame.iloc[-1].position_return, 0.0175)

    def test_reversal_preserves_both_trade_events(self):
        result = self.run_bars([self.bar(0), self.bar(1, direction="BUY"),
                                self.bar(2, direction="SELL"), self.bar(3)],
                               reverse_on_opposite_signal=True)
        reversal = result.dataframe.iloc[2]
        self.assertEqual(reversal.position, -1)
        self.assertEqual(reversal.closed_positions[0]["exit_reason"], "OPPOSITE_SIGNAL")
        self.assertEqual(len({e["trade_id"] for e in reversal.orders}), 2)
        self.assertEqual(len(result.trades), 2)

    def test_window_closure_recorded_once_in_final_row(self):
        bars = [self.bar(0), self.bar(1, direction="BUY"), self.bar(2), self.bar(3)]
        result = self.run_bars(bars, from_time=bars[1].candle.timestamp.isoformat(),
                               to_time=bars[3].candle.timestamp.isoformat())
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "END_OF_WINDOW")
        self.assertEqual(result.dataframe.iloc[-1].position, 0)
        self.assertEqual(result.dataframe.iloc[-1].equity, 10000 + result.net_cash)

    def test_ambiguous_candle_stop_first(self):
        result = self.run_bars([self.bar(0), self.bar(1, direction="BUY"), self.bar(2, 103, 99, 100)])
        self.assertEqual(result.trades[0].exit_reason, "SL")
        self.assertEqual(result.dataframe.iloc[-1].ambiguous_exit_candles, 1)
        self.assertAlmostEqual(result.dataframe.iloc[-1].position_return, -0.01)

    def test_empty_result_has_csv_headers(self):
        result = self.run_bars([])
        self.assertTrue(result.dataframe.empty)
        self.assertIn("position_return", result.dataframe.columns)

    def test_full_position_exits_at_tp1_for_both_directions(self):
        self.config = replace(self.config, take_profit_allocations=[1, 0, 0])
        for direction, high, low, close in [("BUY", 103, 100, 103), ("SELL", 100, 97, 97)]:
            with self.subTest(direction=direction):
                result = self.run_bars([self.bar(0), self.bar(1, direction=direction),
                                        self.bar(2, high, low, close)])
                self.assertEqual(result.net_cash, 10)
                self.assertTrue(result.trades[0].tp1_hit)
                self.assertFalse(result.trades[0].tp2_hit)
                self.assertFalse(result.trades[0].tp3_hit)
                self.assertEqual({o["kind"] for o in result.rows[1]["active_orders"]}, {"SL", "TP1"})
                fills = [o for o in result.rows[2]["orders"] if o["status"] == "FILLED"]
                self.assertEqual([o["kind"] for o in fills], ["TP1"])
                self.assertEqual(fills[0]["fraction"], 1)
                self.assertEqual(result.rows[2]["position"], 0)

    def test_entry_volume_threshold_is_strict_for_both_directions(self):
        for direction in ("BUY", "SELL"):
            for volume, expected in [(1999, 0), (2000, 0), (2000.1, 1)]:
                with self.subTest(direction=direction, volume=volume):
                    entry = self.bar(1, direction=direction)
                    entry = replace(entry, candle=replace(entry.candle, volume=volume))
                    result = self.run_bars([self.bar(0), entry, self.bar(2)], min_entry_volume=2000)
                    self.assertEqual(len(result.trades), expected)
                    self.assertEqual(result.rows[1]["entry_volume_allowed"], bool(expected))

    def test_low_volume_does_not_block_stops_or_opposite_signal_exits(self):
        entry = self.bar(1, direction="BUY")
        entry = replace(entry, candle=replace(entry.candle, volume=2001))
        for exit_bar, expected_reason in [(self.bar(2, 100, 99, 100), "SL"),
                                          (self.bar(2, direction="SELL"), "OPPOSITE_SIGNAL")]:
            result = self.run_bars(
                [self.bar(0), entry, exit_bar, self.bar(3)],
                min_entry_volume=2000,
                reverse_on_opposite_signal=expected_reason == "OPPOSITE_SIGNAL",
            )
            self.assertEqual(len(result.trades), 1)
            self.assertEqual(result.trades[0].exit_reason, expected_reason)
            self.assertEqual(result.rows[2]["position"], 0)

    def test_low_volume_does_not_block_take_profits(self):
        entry = self.bar(1, direction="BUY")
        entry = replace(entry, candle=replace(entry.candle, volume=2001))
        result = self.run_bars([self.bar(0), entry, self.bar(2, 103, 100, 103)], min_entry_volume=2000)
        self.assertEqual(result.trades[0].exit_reason, "TP_PLAN_COMPLETE")

    def test_cash_at_ten_pence_stake_reconciles_with_fills(self):
        bars = [self.bar(0), self.bar(1, direction="BUY"), self.bar(2, 103, 100, 103)]
        with patch("sniper_bot.backtest.calculate_khansaab_bars", return_value=bars):
            result = run_backtest([b.candle for b in bars], SniperStrategy(self.config), 0.1, 10000)
        fills = [e for r in result.rows for e in r["orders"] if "pnl_cash" in e]
        self.assertAlmostEqual(result.net_cash, 0.175)
        self.assertAlmostEqual(sum(e["pnl_cash"] for e in fills), result.net_cash)
        self.assertAlmostEqual(result.rows[-1]["equity"], 10000.175)
        self.assertEqual(result.rows[1]["stake_per_point"], 0.1)

    def test_close_precision_does_not_depend_on_entry_trailing_zero(self):
        result = self.run_bars([self.bar(0), self.bar(1, direction="BUY"), self.bar(2, 100.4, 100, 100.4)])
        self.assertEqual(result.trades[0].exit_price, 100.4)
        self.assertAlmostEqual(result.net_cash, 4.0)

    def test_fixed_outcome_model_caps_gap_at_stop_for_both_directions(self):
        for direction, price in [("BUY", 98.5), ("SELL", 101.5)]:
            with self.subTest(direction=direction):
                result = self.run_bars([self.bar(0), self.bar(1, direction=direction),
                                        self.bar(2, price, price, price)])
                expected_stop = 99 if direction == "BUY" else 101
                self.assertEqual(result.trades[0].exit_price, expected_stop)
                self.assertEqual(result.net_cash, -10)

    def test_drawdown_includes_unrealized_profit_peak(self):
        result = self.run_bars([self.bar(0), self.bar(1, direction="BUY"),
                                self.bar(2, 100.8, 100, 100.8), self.bar(3)])
        self.assertAlmostEqual(result.max_drawdown_cash, 8)


if __name__ == "__main__":
    unittest.main()
