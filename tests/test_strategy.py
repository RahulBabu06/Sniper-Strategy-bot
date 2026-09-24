from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sniper_bot.config import StrategyConfig
from sniper_bot.khansaab_indicator import calculate_khansaab_bars
from sniper_bot.market import Candle
from sniper_bot.strategy import Direction, SniperStrategy


def config() -> StrategyConfig:
    return StrategyConfig(
        market_epics=["TEST"],
        resolution="MINUTE_5",
        price_points=80,
        use_spread_filter=True,
        max_spread_points=0.0004,
        fast_ema=9,
        slow_ema=21,
        rsi_period=14,
        atr_period=14,
        adx_period=14,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        min_bull_score_long=60,
        min_bear_score_short=0,
        require_short_bear_dominance=True,
        min_risk_reward=1.0,
        stop_atr_multiple=1.5,
        target_multiples=[1, 2, 3, 4, 5],
        take_profit_allocations=[0.5, 0.25, 0.25],
        break_even_after_tp1=True,
        broker_target_index=2,
        default_size=10,
        spread_bet_price_decimals=1,
        spread_bet_point_size=0.1,
        starting_balance=10000,
    )


def candle(index: int, close: float, spread: float = 0.0002) -> Candle:
    bid = close - spread / 2
    ask = close + spread / 2
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return Candle(ts, bid, ask, bid + 0.0004, ask + 0.0004, bid - 0.0004, ask - 0.0004, bid, ask)


class StrategyTests(unittest.TestCase):
    def test_holds_when_not_enough_candles(self) -> None:
        signal = SniperStrategy(config()).evaluate([candle(0, 1.1)])
        self.assertEqual(signal.direction, Direction.HOLD)

    def test_holds_when_spread_is_too_wide(self) -> None:
        candles = [candle(i, 1.10 + i * 0.0001) for i in range(60)]
        candles[-1] = candle(60, 1.105, spread=0.001)
        signal = SniperStrategy(config()).evaluate(candles)
        self.assertEqual(signal.direction, Direction.HOLD)
        self.assertIn("spread", signal.reason)

    def test_indicator_status_uses_trade_direction(self) -> None:
        values = [1.20 - i * 0.0005 for i in range(55)] + [1.18 + i * 0.001 for i in range(10)]
        bars = calculate_khansaab_bars([candle(i, value) for i, value in enumerate(values)], config())
        statuses = [bar.status for bar in bars]
        self.assertIn("BUY", statuses)

    def test_public_pine_parity_does_not_gate_cross_with_dashboard_score(self) -> None:
        cfg = replace(config(), min_bull_score_long=100)
        values = [100 - index for index in range(25)] + [80, 90, 110, 140, 180]
        bars = calculate_khansaab_bars([candle(i, value) for i, value in enumerate(values)], cfg)
        buy_bar = next(bar for bar in bars if bar.trigger_buy)
        self.assertLessEqual(buy_bar.bull_score, cfg.min_bull_score_long)

        signal = SniperStrategy(cfg).evaluate(
            [candle(i, value) for i, value in enumerate(values[: buy_bar.index + 1])]
        )
        self.assertEqual(signal.direction, Direction.BUY)


if __name__ == "__main__":
    unittest.main()
