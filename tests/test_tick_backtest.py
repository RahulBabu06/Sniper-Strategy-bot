import math
import csv
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sniper_bot.indicators import ema, atr, rsi, adx, macd, session_vwap, sma
from sniper_bot.market import Candle
from sniper_bot.csv_loader import write_candles_csv
from sniper_bot.tick_indicator import CandleFileIndicator, Tick, TickIndicator
from sniper_bot.tick_backtest import TickReplay, ReplayConfig, read_ticks, run_file


START = datetime(2025, 1, 2, tzinfo=timezone.utc)


def tick(seconds, bid=100, ask=100.2):
    return Tick(START + timedelta(seconds=seconds), bid, ask, 1, 1)


def state(status="WAIT", bull=6, bear=1, bias="STRONG BULL", price=100):
    return {"status": status, "bull_count": bull, "bear_count": bear,
            "bull_score": bull / 7 * 100, "bear_score": bear / 7 * 100,
            "market_bias": bias, "close": price, "atr": 1.0, "volume": 1750.0, "ready": True}


class TickExecutionTests(unittest.TestCase):
    def test_ig_scaled_forex_point_is_default(self):
        self.assertEqual(ReplayConfig().point_size, 1.0)
        self.assertEqual(ReplayConfig().stake, 1.0)

    def test_directional_score_filter_is_strict_and_applies_to_buys_and_sells(self):
        for direction in ("BUY", "SELL"):
            with self.subTest(direction=direction):
                replay = TickReplay(ReplayConfig(min_entry_score=60))
                low = state(direction, bull=4, bear=4)
                low[f"{'bull' if direction == 'BUY' else 'bear'}_score"] = 60
                row = replay.step(tick(0), low, 0)
                self.assertEqual(row["orders"][0]["status"], "FILTERED")
                self.assertIsNone(replay.pending)
                replay.step(tick(1), state(), 1)
                high = state(direction, bull=5 if direction == "BUY" else 1,
                             bear=5 if direction == "SELL" else 1)
                replay.step(tick(2), high, 2)
                self.assertEqual(replay.pending["direction"], direction)

    def test_entry_score_filter_validation(self):
        for value in (-1, 101, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TickReplay(ReplayConfig(min_entry_score=value))

    def test_entry_volume_range_is_inclusive_and_auditable(self):
        for volume, allowed in ((1499.9, False), (1500, True), (2000, True), (2000.1, False)):
            with self.subTest(volume=volume):
                replay = TickReplay(ReplayConfig(min_entry_volume=1500, max_entry_volume=2000))
                snapshot = {**state("BUY"), "volume": volume}
                row = replay.step(tick(0), snapshot, 0)
                self.assertEqual(replay.pending is not None, allowed)
                if not allowed:
                    self.assertEqual(row["orders"][0]["reason"], "VOLUME_RANGE")

    def test_bar_close_signal_ignores_intrabar_cross_and_fills_first_next_bar_tick(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", tp_mode="cash", tp_value=10,
                                         sl_mode="cash", sl_value=10, signal_timing="bar_close",
                                         point_size=0.1))
        initial = {**state(), "_new_bar": True, "_closed_state": None}
        replay.step(tick(0), initial, 0)
        intrabar = {**state("BUY"), "_new_bar": False, "_closed_state": None,
                    "_observation_timestamp": tick(1).timestamp.isoformat()}
        replay.step(tick(1), intrabar, 1)
        self.assertIsNone(replay.position)
        self.assertIsNone(replay.pending)
        current = {**state("WAIT"), "_new_bar": True, "_closed_state": intrabar}
        row = replay.step(tick(300, 101, 101.3), current, 2)
        self.assertEqual(replay.position["direction"], "BUY")
        self.assertEqual(replay.position["entry_seq"], 2)
        self.assertEqual(replay.position["signal_seq"], 1)
        self.assertEqual(replay.position["entry_price"], 101.3)
        self.assertEqual(replay.position["signal_time"], tick(1).timestamp.isoformat())
        self.assertEqual(row["orders"][0]["status"], "FILLED")

    def test_invalid_signal_timing_is_rejected(self):
        with self.assertRaises(ValueError):
            TickReplay(ReplayConfig(signal_timing="future"))

    def test_invalid_entry_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            TickReplay(ReplayConfig(entry_mode="stop_order"))
        for value in (0, -1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TickReplay(ReplayConfig(limit_expiry_minutes=value))

    def test_bar_close_pine_limit_waits_for_quote_and_fills_at_signal_price(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", signal_timing="bar_close",
                                         entry_mode="pine_limit"))
        replay.step(tick(0), {**state(), "_new_bar": True, "_closed_state": None}, 0)
        closed_signal = {**state("BUY", price=100),
                         "_observation_timestamp": tick(299).timestamp.isoformat()}
        submitted = replay.step(tick(300, 101.0, 101.2),
                                {**state(), "_new_bar": True, "_closed_state": closed_signal}, 1)
        self.assertIsNone(replay.position)
        self.assertEqual(replay.pending["signal_price"], 100)
        self.assertEqual(submitted["orders"][0]["status"], "WORKING")
        replay.step(tick(301, 100.0, 100.2), {**state(), "_new_bar": False}, 2)
        self.assertIsNone(replay.position)
        filled = replay.step(tick(302, 99.7, 99.9), {**state(), "_new_bar": False}, 3)
        self.assertEqual(replay.position["direction"], "BUY")
        self.assertEqual(replay.position["entry_price"], 100)
        self.assertEqual(replay.position["stop_price"], 98.5)
        self.assertEqual(replay.position["tp1"], 101.5)
        self.assertEqual(filled["orders"][0]["order_type"], "LIMIT")

    def test_sell_pine_limit_uses_bid_to_trigger(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", entry_mode="pine_limit"))
        replay.step(tick(0), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 0)
        replay.step(tick(1, 99.8, 100.0), state(), 1)
        self.assertIsNone(replay.position)
        replay.step(tick(2, 100.1, 100.3), state(), 2)
        self.assertEqual(replay.position["direction"], "SELL")
        self.assertEqual(replay.position["entry_price"], 100)
        self.assertEqual(replay.position["stop_price"], 101.5)
        self.assertEqual(replay.position["tp1"], 98.5)

    def test_new_signal_does_not_replace_unfilled_limit(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", entry_mode="pine_limit"))
        replay.step(tick(0), state("BUY"), 0)
        replay.step(tick(1, 101.0, 101.2), state(), 1)
        row = replay.step(tick(2, 101.0, 101.2),
                          state("SELL", bull=1, bear=6, bias="STRONG BEAR", price=99), 2)
        self.assertEqual([event["status"] for event in row["orders"]], ["IGNORED"])
        self.assertEqual(row["orders"][0]["reason"], "PENDING_ENTRY_ALREADY_WORKING")
        self.assertEqual(replay.pending["direction"], "BUY")
        self.assertEqual(replay.pending["signal_price"], 100)

    def test_unfilled_limit_is_cancelled_at_end_of_data(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", entry_mode="pine_limit"))
        row = replay.step(tick(0), state("BUY"), 0)
        self.assertEqual(row["orders"][0]["status"], "WORKING")
        row = replay.step(tick(1, 101.0, 101.2), state(), 1, final=True)
        self.assertIsNone(replay.pending)
        self.assertEqual(row["orders"][0]["status"], "CANCELLED")
        self.assertEqual(replay.trades, [])

    def test_pine_limit_can_expire_before_a_late_fill(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", entry_mode="pine_limit",
                                         limit_expiry_minutes=5))
        replay.step(tick(0), state("BUY"), 0)
        row = replay.step(tick(300, 99.7, 99.9), state(), 1)
        self.assertIsNone(replay.position)
        self.assertIsNone(replay.pending)
        self.assertEqual(row["orders"][0]["status"], "CANCELLED")
        self.assertEqual(row["orders"][0]["reason"], "LIMIT_EXPIRED")

    def test_inverted_signals_flip_execution_but_preserve_indicator_signal(self):
        for signal, expected_direction, entry in [("BUY", "SELL", 100),
                                                   ("SELL", "BUY", 100.2)]:
            with self.subTest(signal=signal):
                replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", tp_mode="cash", tp_value=10,
                                                 sl_mode="cash", sl_value=10, invert_signals=True,
                                                 point_size=0.1, stake=0.1))
                pending = replay.step(tick(0), state(signal), 0)
                self.assertEqual(pending["orders"][0]["indicator_signal"], signal)
                self.assertEqual(pending["orders"][0]["side"], expected_direction)
                replay.step(tick(1), state(signal), 1)
                self.assertEqual(replay.position["indicator_signal"], signal)
                self.assertEqual(replay.position["direction"], expected_direction)
                self.assertEqual(replay.position["entry_price"], entry)
                sign = 1 if expected_direction == "BUY" else -1
                self.assertAlmostEqual(replay.position["fixed_tp_price"], entry + sign * 10)
                self.assertAlmostEqual(replay.position["fixed_stop_price"], entry - sign * 10)

    def test_custom_tp_cash_and_points_from_fill_both_directions(self):
        for direction in ("BUY", "SELL"):
            for mode, value, stake, distance in [("cash", 10, 0.1, 10),
                                                  ("cash", 10, 0.2, 5),
                                                  ("points", 100, 0.2, 10)]:
                with self.subTest(direction=direction, mode=mode, stake=stake):
                    replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", tp_mode=mode,
                                                    tp_value=value, stake=stake, point_size=0.1))
                    replay.step(tick(0), state(direction), 0)
                    replay.step(tick(1, 100.3, 100.5), state(direction), 1)
                    entry = 100.5 if direction == "BUY" else 100.3
                    sign = 1 if direction == "BUY" else -1
                    target = entry + sign * distance
                    self.assertAlmostEqual(replay.position["fixed_tp_price"], target)
                    self.assertAlmostEqual(replay.position["stop_price"], 100 - sign * 1.5)
                    quote = entry + sign * 2
                    replay.step(tick(2, quote if sign == 1 else quote - 0.2,
                                     quote + 0.2 if sign == 1 else quote), state(), 2)
                    self.assertEqual(replay.trades, [])  # Crossing Pine TP1 must not close.
                    replay.step(tick(3, target if sign == 1 else target - 0.2,
                                     target + 0.2 if sign == 1 else target), state(), 3)
                    self.assertEqual(replay.trades[0]["exit_reason"], f"TP_{mode.upper()}")
                    self.assertAlmostEqual(replay.trades[0]["pnl_cash"], 10 if mode == "cash" else 100 * stake)

    def test_custom_tp_rejects_invalid_configuration(self):
        for kwargs in ({"tp_mode": "invalid"}, {"tp_value": 10},
                       {"tp_mode": "cash"}, {"tp_mode": "points", "tp_value": 0},
                       {"tp_mode": "cash", "tp_value": -1},
                       {"tp_mode": "cash", "tp_value": math.nan},
                       {"tp_mode": "cash", "tp_value": math.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TickReplay(ReplayConfig(exit_mode="atr_bracket", **kwargs))
        with self.assertRaises(ValueError):
            TickReplay(ReplayConfig(exit_mode="opposite_signal_only", tp_mode="cash", tp_value=10))

    def test_custom_cash_stop_both_directions(self):
        for direction in ("BUY", "SELL"):
            with self.subTest(direction=direction):
                replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", sl_mode="cash", sl_value=10,
                                                 point_size=0.1, stake=0.1))
                replay.step(tick(0), state(direction), 0)
                replay.step(tick(1), state(direction), 1)
                expected = 90.2 if direction == "BUY" else 110.0
                self.assertAlmostEqual(replay.position["fixed_stop_price"], expected)
                self.assertEqual(replay.trades, [])
                if direction == "BUY":
                    replay.step(tick(2, 90.2, 90.4), state(), 2)
                else:
                    replay.step(tick(2, 109.8, 110.0), state(), 2)
                self.assertEqual(replay.trades[0]["exit_reason"], "SL_CASH")
                self.assertAlmostEqual(replay.trades[0]["pnl_cash"], -10)

    def test_custom_sl_rejects_invalid_configuration(self):
        for kwargs in ({"sl_mode": "invalid"}, {"sl_value": 10},
                       {"sl_mode": "cash"}, {"sl_mode": "points", "sl_value": 0},
                       {"sl_mode": "cash", "sl_value": -1},
                       {"sl_mode": "cash", "sl_value": math.nan},
                       {"sl_mode": "cash", "sl_value": math.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TickReplay(ReplayConfig(exit_mode="atr_bracket", **kwargs))
        with self.assertRaises(ValueError):
            TickReplay(ReplayConfig(exit_mode="opposite_signal_only", sl_mode="cash", sl_value=10))

    def test_cash_tp_quote_gap_and_atr_stop(self):
        for bid, reason, pnl in [(111, "TP_CASH", 10.8), (98, "SL", -2.2)]:
            with self.subTest(reason=reason):
                replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", tp_mode="cash", tp_value=10,
                                                 point_size=0.1, stake=0.1))
                replay.step(tick(0), state("BUY"), 0)
                replay.step(tick(1), state("BUY"), 1)
                row = replay.step(tick(2, bid, bid + 0.2), state(), 2)
                self.assertEqual(replay.trades[0]["exit_reason"], reason)
                self.assertAlmostEqual(replay.trades[0]["pnl_cash"], pnl)
                self.assertAlmostEqual(row["equity"], 10000 + pnl)

    def test_atr_bracket_both_directions_and_quote_fills(self):
        for direction, bid, ask, reason in [
            ("BUY", 101.7, 101.9, "TP1"), ("BUY", 98.3, 98.5, "SL"),
            ("SELL", 98.2, 98.4, "TP1"), ("SELL", 101.4, 101.6, "SL"),
        ]:
            with self.subTest(direction=direction, reason=reason):
                replay = TickReplay(ReplayConfig(exit_mode="atr_bracket"))
                replay.step(tick(0), state(direction), 0)
                replay.step(tick(1), state(direction), 1)
                replay.step(tick(90000), state(bull=0, bear=7, bias="STRONG BEAR"), 2)
                self.assertEqual(replay.trades, [])
                self.assertEqual(replay.position["active_tp"], 1)
                replay.step(tick(90001, bid, ask), state(bull=7), 3)
                self.assertEqual(replay.trades[0]["exit_reason"], reason)
                self.assertEqual(replay.trades[0]["exit_price"], bid if direction == "BUY" else ask)

    def test_atr_bracket_opposite_cross_reverses_next_tick(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", reverse_on_opposite_signal=True))
        replay.step(tick(0), state("BUY"), 0)
        replay.step(tick(1), state("BUY"), 1)
        replay.step(tick(2), state("SELL"), 2)
        self.assertEqual(replay.trades, [])
        replay.step(tick(3), state("SELL"), 3)
        self.assertEqual(replay.trades[0]["exit_reason"], "OPPOSITE_SIGNAL")
        self.assertEqual(replay.position["direction"], "SELL")

    def test_opposite_only_ignores_stops_targets_scores_and_timeout(self):
        replay = TickReplay(ReplayConfig(exit_mode="opposite_signal_only", reverse_on_opposite_signal=True,
                                         stake=0.1))
        replay.step(tick(0), state("BUY", bull=4), 0)
        replay.step(tick(1), state("BUY", bull=4), 1)
        replay.step(tick(2, 97, 97.2), state(bull=1, bear=6, bias="STRONG BEAR"), 2)
        replay.step(tick(90000, 110, 110.2), state(bull=7), 3)
        self.assertEqual(replay.trades, [])
        self.assertEqual(replay.position["direction"], "BUY")
        self.assertEqual(replay.position["active_tp"], 0)
        replay.step(tick(90001, 109, 109.2), state("SELL", bull=1, bear=6), 4)
        self.assertEqual(replay.position["direction"], "BUY")
        replay.step(tick(90002, 108, 108.3), state("SELL", bull=1, bear=6), 5)
        self.assertEqual(replay.trades[0]["exit_reason"], "OPPOSITE_SIGNAL")
        self.assertEqual(replay.trades[0]["exit_price"], 108)
        self.assertIsNone(replay.trades[0]["timeout_delay_seconds"])
        self.assertEqual(replay.position["direction"], "SELL")
        self.assertEqual(replay.position["entry_price"], 108)
        replay.step(tick(90003), state("BUY"), 6)
        replay.step(tick(90004, 107, 107.5), state("BUY"), 7)
        self.assertEqual(replay.trades[1]["exit_price"], 107.5)
        self.assertEqual(replay.position["entry_price"], 107.5)

    def test_opposite_only_still_liquidates_at_end(self):
        replay = TickReplay(ReplayConfig(exit_mode="opposite_signal_only", reverse_on_opposite_signal=True,
                                         stake=0.1))
        replay.step(tick(0), state("BUY", bull=4), 0)
        replay.step(tick(1), state("BUY", bull=4), 1, final=True)
        self.assertEqual(replay.trades[0]["exit_reason"], "END_OF_DATA")
        self.assertAlmostEqual(replay.trades[0]["pnl_cash"], -0.02)

    def test_next_observation_entry_uses_ask_even_at_same_timestamp(self):
        replay = TickReplay(ReplayConfig(stake=0.1))
        replay.step(tick(0), state("BUY"), 0)
        self.assertIsNone(replay.position)
        replay.step(tick(0, 100.4, 100.7), state("BUY"), 1)
        self.assertEqual(replay.position["entry_price"], 100.7)
        self.assertEqual(replay.position["signal_price"], 100)
        self.assertEqual(replay.position["entry_seq"], 1)
        self.assertEqual(replay.position["stop_price"], 98.5)

    def test_dynamic_downgrade_exits_at_current_bid(self):
        replay = TickReplay(ReplayConfig(stake=0.1))
        replay.step(tick(0), state("BUY"), 0)
        replay.step(tick(1), state("BUY"), 1)
        replay.step(tick(2, 102, 102.2), state("WAIT", bull=5), 2)
        self.assertEqual(replay.trades[0]["exit_reason"], "DYNAMIC_TP")
        self.assertEqual(replay.trades[0]["exit_price"], 102)
        self.assertAlmostEqual(replay.trades[0]["pnl_cash"], 0.18)

    def test_sell_dynamic_downgrade_uses_ask_and_bear_score(self):
        replay = TickReplay(ReplayConfig(stake=0.1))
        replay.step(tick(0), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 0)
        replay.step(tick(1), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 1)
        replay.step(tick(2, 97.8, 98), state(bull=1, bear=5, bias="STRONG BEAR"), 2)
        self.assertEqual(replay.trades[0]["exit_price"], 98)
        self.assertEqual(replay.trades[0]["exit_reason"], "DYNAMIC_TP")

    def test_low_score_is_an_exit_not_an_entry_filter(self):
        replay = TickReplay(ReplayConfig(stake=0.1))
        replay.step(tick(0), state("BUY", bull=4), 0)
        replay.step(tick(1), state("BUY", bull=4), 1)
        self.assertEqual(len(replay.trades), 1)
        self.assertEqual(replay.trades[0]["exit_reason"], "SCORE_BELOW_5")
        self.assertAlmostEqual(replay.trades[0]["pnl_cash"], -0.02)
        replay.step(tick(2), state("BUY", bull=4), 2)
        self.assertIsNone(replay.pending)

    def test_upgrade_does_not_move_fixed_levels(self):
        replay = TickReplay()
        replay.step(tick(0), state("BUY", bull=5), 0)
        replay.step(tick(1), state("BUY", bull=5), 1)
        replay.step(tick(2, 100.5, 100.7), state(bull=7, price=100.5), 2)
        self.assertEqual(replay.position["active_tp"], 3)
        self.assertEqual(replay.position["tp3"], 104.5)
        self.assertEqual(replay.position["stop_price"], 98.5)

    def test_working_target_fills_before_upgrade_on_same_tick(self):
        replay = TickReplay()
        replay.step(tick(0), state("BUY", bull=5), 0)
        replay.step(tick(1), state("BUY", bull=5), 1)
        replay.step(tick(2, 101.6, 101.8), state(bull=7), 2)
        self.assertEqual(replay.trades[0]["exit_reason"], "TP1")
        self.assertEqual(replay.trades[0]["exit_price"], 101.6)

    def test_stop_gap_and_strong_bias_exit(self):
        for quote, snapshot, reason in [
            (tick(2, 97, 97.2), state(), "SL"),
            (tick(2), state(bull=1, bear=6, bias="STRONG BEAR"), "STRONG_OPPOSITE_BIAS"),
        ]:
            replay = TickReplay()
            replay.step(tick(0), state("BUY"), 0)
            replay.step(tick(1), state("BUY"), 1)
            replay.step(quote, snapshot, 2)
            self.assertEqual(replay.trades[0]["exit_reason"], reason)
            self.assertEqual(replay.trades[0]["exit_price"], quote.bid)

    def test_timeout_and_final_close(self):
        for seconds, final, reason in [(86402, False, "TIMEOUT"), (2, True, "END_OF_DATA")]:
            replay = TickReplay()
            replay.step(tick(0), state("BUY"), 0)
            replay.step(tick(1), state("BUY"), 1)
            row = replay.step(tick(seconds), state(), 2, final=final)
            self.assertEqual(replay.trades[0]["exit_reason"], reason)
            self.assertEqual(row["equity"], replay.balance)

    def test_opposite_status_enters_reverse_on_next_tick_only(self):
        replay = TickReplay(ReplayConfig(reverse_on_opposite_signal=True))
        replay.step(tick(0), state("BUY"), 0)
        replay.step(tick(1), state("BUY"), 1)
        replay.step(tick(2), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 2)
        self.assertIsNone(replay.position)
        replay.step(tick(3), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 3)
        self.assertEqual(replay.position["direction"], "SELL")
        self.assertEqual(replay.position["entry_seq"], 3)

    def test_default_ignores_opposite_cross_while_trade_is_open(self):
        replay = TickReplay(ReplayConfig(exit_mode="atr_bracket", tp_mode="cash", tp_value=10,
                                         sl_mode="cash", sl_value=10, point_size=0.1))
        replay.step(tick(0), state("BUY"), 0)
        replay.step(tick(1), state("BUY"), 1)
        replay.step(tick(2), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 2)
        replay.step(tick(3), state("SELL", bull=1, bear=6, bias="STRONG BEAR"), 3)
        self.assertEqual(replay.trades, [])
        self.assertIsNone(replay.pending)
        self.assertEqual(replay.position["direction"], "BUY")
        replay.step(tick(4, 110.2, 110.4), state(), 4)
        self.assertEqual(replay.trades[0]["exit_reason"], "TP_CASH")
        self.assertIsNone(replay.position)

    def test_same_direction_signal_reanchors_fixed_bracket(self):
        replay = TickReplay(ReplayConfig(
            exit_mode="atr_bracket", tp_mode="points", tp_value=10,
            sl_mode="points", sl_value=10, point_size=1,
            reanchor_on_same_direction_signal=True,
        ))
        replay.step(tick(0), state("BUY", price=100), 0)
        replay.step(tick(1, 99.8, 100.0), state(), 1)
        self.assertEqual(replay.position["fixed_stop_price"], 90)
        self.assertEqual(replay.position["fixed_tp_price"], 110)
        replay.step(tick(2), state("WAIT", price=105), 2)
        row = replay.step(tick(3, 104.8, 105.0), state("BUY", price=105), 3)
        self.assertEqual(replay.position["fixed_stop_price"], 95)
        self.assertEqual(replay.position["fixed_tp_price"], 115)
        self.assertEqual(row["orders"][0]["kind"], "BRACKET_REANCHOR")

    def test_no_same_tick_entry_at_end_of_data(self):
        replay = TickReplay()
        replay.step(tick(0), state("BUY"), 0, final=True)
        self.assertEqual(replay.trades, [])
        self.assertIsNone(replay.pending)


class TickIndicatorTests(unittest.TestCase):
    def test_candle_file_indicator_releases_rows_only_after_close_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candles.csv"
            candles = [
                Candle(START, 100, 100, 102, 102, 99, 99, 101, 101, 10),
                Candle(START + timedelta(minutes=5), 101, 101, 103, 103, 100, 100, 102, 102, 12),
            ]
            write_candles_csv(candles, path)
            indicator = CandleFileIndicator(path)
            before = indicator.update(Tick(START + timedelta(minutes=4, seconds=59), 101, 101.2))
            self.assertFalse(before["_new_bar"])
            self.assertTrue(math.isnan(before["close"]))
            first = indicator.update(Tick(START + timedelta(minutes=5), 101, 101.2))
            self.assertTrue(first["_new_bar"])
            self.assertEqual(first["close"], 101)
            self.assertEqual(first["_closed_state"]["_observation_timestamp"],
                             (START + timedelta(minutes=5)).isoformat())
            unchanged = indicator.update(Tick(START + timedelta(minutes=5, seconds=1), 101, 101.2))
            self.assertFalse(unchanged["_new_bar"])
            second = indicator.update(Tick(START + timedelta(minutes=10), 102, 102.2))
            self.assertTrue(second["_new_bar"])
            self.assertEqual(second["close"], 102)

    def test_closed_values_match_batch_indicators(self):
        indicator = TickIndicator()
        candles, observations = [], []
        for i in range(80):
            timestamp = START + timedelta(minutes=i * 5)
            price = 100 + math.sin(i / 4) * 2 + i * 0.01
            for second, p in [(0, price), (100, price + 0.2), (200, price - 0.1)]:
                result = indicator.update(Tick(timestamp + timedelta(seconds=second), p, p + 0.2, 1, 1))
            candles.append(Candle(timestamp, price, price, price + 0.2, price + 0.2,
                                  price - 0.1, price - 0.1, price - 0.1, price - 0.1, 3))
            observations.append(result)
        closes = [c.close_mid for c in candles]
        m, s, _ = macd(closes, 12, 26, 9)
        expected = {"ema9": ema(closes, 9), "ema21": ema(closes, 21), "atr": atr(candles, 14),
                    "rsi": rsi(closes, 14), "adx": adx(candles, 14), "macd_main": m, "macd_signal": s,
                    "vwap": session_vwap(candles)}
        for key, values in expected.items():
            for i, wanted in enumerate(values):
                if math.isfinite(wanted):
                    self.assertAlmostEqual(observations[i][key], wanted, places=8, msg=f"{key} at {i}")

    def test_extra_intrabar_updates_do_not_compound_ema(self):
        a, b = TickIndicator(), TickIndicator()
        for i in range(40):
            t = Tick(START + timedelta(minutes=5 * i), 100 + i, 100.2 + i, 1, 1)
            a.update(t)
            b.update(t)
        for second in range(1, 10):
            a.update(Tick(t.timestamp + timedelta(seconds=second), 145, 145.2, 0, 0))
        last = Tick(t.timestamp + timedelta(seconds=20), 140, 140.2, 0, 0)
        self.assertAlmostEqual(a.update(last)["ema9"], b.update(last)["ema9"])

    def test_unconfirmed_crossover_is_rolled_back(self):
        indicator = TickIndicator()
        for i in range(40):
            indicator.update(Tick(START + timedelta(minutes=5 * i), 200 - i, 200.2 - i, 1, 1))
        timestamp = START + timedelta(minutes=200)
        first = indicator.update(Tick(timestamp, 400, 400.2, 1, 1))
        self.assertEqual(first["status"], "BUY")
        second = indicator.update(Tick(timestamp + timedelta(seconds=1), 150, 150.2, 1, 1))
        self.assertEqual(second["status"], "WAIT")
        third = indicator.update(Tick(timestamp + timedelta(seconds=2), 400, 400.2, 1, 1))
        self.assertEqual(third["status"], "BUY")

    def test_new_bar_exposes_exact_final_state_of_closed_bar(self):
        indicator = TickIndicator()
        for i in range(40):
            indicator.update(Tick(START + timedelta(minutes=5 * i), 200 - i, 200.2 - i, 1, 1))
        timestamp = START + timedelta(minutes=200)
        final_tick = Tick(timestamp + timedelta(seconds=299), 400, 400.2, 1, 1)
        final_state = indicator.update(final_tick)
        next_state = indicator.update(Tick(timestamp + timedelta(minutes=5), 401, 401.2, 1, 1))
        self.assertTrue(next_state["_new_bar"])
        self.assertEqual(next_state["_closed_state"]["status"], final_state["status"])
        self.assertEqual(next_state["_closed_state"]["close"], final_state["close"])
        self.assertEqual(next_state["_closed_state"]["_observation_timestamp"],
                         final_tick.timestamp.isoformat())

    def test_tick_parser_preserves_equal_time_order_and_rejects_crossed_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ticks.csv"
            path.write_text("timestamp,askPrice,bidPrice,askVolume,bidVolume\n1735776000000,151,150,1,2\n1735776000000,152,151,1,2\n")
            data = list(read_ticks(path))
            self.assertEqual([t.bid for t in data], [15000, 15100])
            path.write_text("timestamp,askPrice,bidPrice,askVolume,bidVolume\n1735776000000,149,150,1,2\n")
            with self.assertRaises(ValueError):
                list(read_ticks(path))

    def test_file_outputs_reconcile_and_include_every_tick(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ticks.csv"
            with path.open("w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["timestamp", "askPrice", "bidPrice", "askVolume", "bidVolume"])
                for i in range(80):
                    price = 150 + math.sin(i / 4) * 0.1
                    for second in (0, 100, 200):
                        stamp = START + timedelta(minutes=i * 5, seconds=second)
                        writer.writerow([int(stamp.timestamp() * 1000), price + 0.002, price, 1, 1])
            out = Path(folder) / "out"
            summary = run_file(path, out, START + timedelta(minutes=150), START + timedelta(days=1))
            self.assertEqual(summary["ticks"], 150)
            self.assertEqual(summary["warmup_ticks"], 90)
            with gzip.open(out / "tick_results.csv.gz", "rt") as file:
                rows = list(csv.DictReader(file))
            with (out / "backtest_results.csv").open() as file:
                bars = list(csv.DictReader(file))
            self.assertEqual(len(rows), 150)
            self.assertEqual(len(bars), 50)
            self.assertAlmostEqual(float(rows[-1]["equity"]), 10000 + summary["net_cash"])
            self.assertEqual(float(rows[-1]["position"]), 0)
            previous = 10000
            for row in rows:
                self.assertAlmostEqual(float(row["account_return"]), float(row["equity"]) / previous - 1)
                previous = float(row["equity"])
