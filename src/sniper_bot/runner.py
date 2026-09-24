from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sniper_bot.config import load_config
from sniper_bot.config import AppConfig
from sniper_bot.indicators import rsi
from sniper_bot.ig_client import IGClient
from sniper_bot.strategy import Direction, SniperStrategy


LOGGER = logging.getLogger("sniper_bot")


@dataclass
class LiveState:
    """In-memory idempotency state for one bot process."""

    handled_signals: dict[str, tuple[str, str]] = field(default_factory=dict)
    dry_run_pending_epics: set[str] = field(default_factory=set)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the IG sniper strategy bot.")
    parser.add_argument("--once", action="store_true", help="Evaluate every configured market once and exit.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    client = IGClient(config.ig)
    strategy = SniperStrategy(config.strategy)
    state = LiveState()

    LOGGER.info("starting sniper bot env=%s dry_run=%s", config.ig.environment, config.runtime.dry_run)
    while True:
        run_cycle(client, strategy, config, state)
        if args.once:
            return
        time.sleep(config.runtime.poll_seconds)


def run_cycle(
    client: IGClient,
    strategy: SniperStrategy,
    config: AppConfig,
    state: LiveState | None = None,
    now: datetime | None = None,
) -> None:
    state = state or LiveState()
    now = now or datetime.now(timezone.utc)
    positions = client.open_positions()
    working_orders = [] if config.runtime.dry_run else client.working_orders()
    for epic in config.strategy.market_epics:
        try:
            candles = client.historical_prices(epic, config.strategy.resolution, config.strategy.price_points)
            candles = _completed_candles(candles, config.strategy.resolution, now)
            signal = strategy.evaluate(candles, _five_minute_rsi(client, epic, config))
            LOGGER.info("%s signal=%s reason=%s", epic, signal.direction, signal.reason)

            if signal.direction == Direction.HOLD:
                continue
            signal_key = (candles[-1].timestamp.isoformat(), signal.direction.value)
            if state.handled_signals.get(epic) == signal_key:
                LOGGER.info("%s skipped duplicate signal candle %s", epic, signal_key[0])
                continue
            if _has_open_position(positions, epic):
                LOGGER.info("%s skipped because a bracket position is already open", epic)
                state.handled_signals[epic] = signal_key
                continue
            if epic in state.dry_run_pending_epics or _has_working_order(working_orders, epic):
                LOGGER.info("%s skipped because its original entry limit is still working", epic)
                state.handled_signals[epic] = signal_key
                continue
            if config.runtime.dry_run:
                LOGGER.info(
                    "%s dry-run spread-bet direction=%s stake_per_point=%s entry_ref=%s stop_level=%s tp_plan=%s targets=%s",
                    epic,
                    signal.direction,
                    signal.size,
                    signal.entry_price,
                    signal.stop_price,
                    config.strategy.take_profit_allocations,
                    signal.target_prices,
                )
                state.handled_signals[epic] = signal_key
                state.dry_run_pending_epics.add(epic)
                continue

            response = client.create_working_order(
                epic=epic,
                direction=signal.direction.value,
                size=signal.size,
                entry_level=signal.entry_price,
                stop_level=signal.stop_price,
                limit_level=_live_limit_level(signal, config.strategy.take_profit_allocations),
            )
            LOGGER.info("%s order submitted response=%s", epic, response)
            state.handled_signals[epic] = signal_key
        except Exception:
            LOGGER.exception("%s cycle failed", epic)


def _completed_candles(candles, resolution: str, now: datetime):
    duration = _resolution_duration(resolution)
    if duration is None:
        return candles
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return [candle for candle in candles if candle.timestamp + duration <= now]


def _resolution_duration(resolution: str) -> timedelta | None:
    units = {
        "MINUTE": timedelta(minutes=1),
        "MINUTE_2": timedelta(minutes=2),
        "MINUTE_3": timedelta(minutes=3),
        "MINUTE_5": timedelta(minutes=5),
        "MINUTE_10": timedelta(minutes=10),
        "MINUTE_15": timedelta(minutes=15),
        "MINUTE_30": timedelta(minutes=30),
        "HOUR": timedelta(hours=1),
        "HOUR_2": timedelta(hours=2),
        "HOUR_3": timedelta(hours=3),
        "HOUR_4": timedelta(hours=4),
        "DAY": timedelta(days=1),
    }
    return units.get(resolution.upper())


def _has_open_position(
    positions: list[dict], epic: str, direction: Direction | None = None
) -> bool:
    for item in positions:
        market = item.get("market", {})
        position = item.get("position", {})
        if (market.get("epic") == epic
                and (direction is None or position.get("direction") == direction.value)):
            return True
    return False


def _has_working_order(orders: list[dict], epic: str) -> bool:
    for item in orders:
        market = item.get("market", {})
        order = item.get("workingOrder", {})
        if market.get("epic") == epic or order.get("epic") == epic:
            return True
    return False


def _live_limit_level(signal, allocations: list[float]) -> float:
    """Return the single full-exit target that IG can attach to one position."""
    active = [index for index, value in enumerate(allocations[:3]) if value > 0]
    if len(active) != 1 or abs(allocations[active[0]] - 1.0) > 1e-9:
        raise ValueError(
            "live IG orders require one TAKE_PROFIT_ALLOCATIONS value equal to 1 "
            "(partial multi-target exits are backtest-only)"
        )
    index = active[0]
    if index >= len(signal.target_prices):
        raise ValueError("configured live take-profit target is unavailable")
    return signal.target_prices[index]


def _five_minute_rsi(client: IGClient, epic: str, config: AppConfig) -> float | None:
    if config.strategy.resolution == "MINUTE_5":
        return None
    candles = client.historical_prices(epic, "MINUTE_5", max(config.strategy.rsi_period + 5, 30))
    values = rsi([candle.close_mid for candle in candles], config.strategy.rsi_period)
    # Pine requests ``ta.rsi(close[1], 14)``: the previous 5-minute source
    # candle, not the most recent value returned by the broker.
    if len(values) < 2:
        return None
    return values[-2]
