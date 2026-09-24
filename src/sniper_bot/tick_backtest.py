"""Historical bid/ask tick replay for the KhanSaab EMA-cross signals."""

import argparse
import csv
import gzip
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sniper_bot.tick_indicator import CandleFileIndicator, Tick, TickIndicator


@dataclass(frozen=True)
class ReplayConfig:
    stake: float = 1.0
    point_size: float = 1.0
    starting_balance: float = 10000
    atr_multiple: float = 1.5
    price_decimals: int = 1
    max_hours: float = 24
    exit_mode: str = "dynamic"
    low_score_tp: str = "tp1"
    tp_mode: str = "pine"
    tp_value: float | None = None
    sl_mode: str = "pine"
    sl_value: float | None = None
    reverse_on_opposite_signal: bool = False
    invert_signals: bool = False
    signal_timing: str = "intrabar"
    min_entry_score: float = 0.0
    entry_mode: str = "market"
    limit_expiry_minutes: float | None = None
    min_entry_volume: float | None = None
    max_entry_volume: float | None = None
    reanchor_on_same_direction_signal: bool = False


class TickReplay:
    def __init__(self, config=ReplayConfig()):
        if config.exit_mode not in {"dynamic", "opposite_signal_only", "opposite_signal_or_tp", "atr_bracket"}:
            raise ValueError("invalid exit mode")
        if config.signal_timing not in {"intrabar", "bar_close", "candle_file"}:
            raise ValueError("signal timing must be intrabar, bar_close or candle_file")
        if config.entry_mode not in {"market", "pine_limit"}:
            raise ValueError("entry mode must be market or pine_limit")
        if (config.limit_expiry_minutes is not None and
                (not math.isfinite(config.limit_expiry_minutes) or config.limit_expiry_minutes <= 0)):
            raise ValueError("limit expiry must be a positive number of minutes")
        if not math.isfinite(config.min_entry_score) or not 0 <= config.min_entry_score <= 100:
            raise ValueError("minimum entry score must be between 0 and 100")
        for name, value in (("minimum", config.min_entry_volume), ("maximum", config.max_entry_volume)):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} entry volume must be finite and nonnegative")
        if (config.min_entry_volume is not None and config.max_entry_volume is not None
                and config.min_entry_volume > config.max_entry_volume):
            raise ValueError("minimum entry volume cannot exceed maximum entry volume")
        if config.low_score_tp not in {"tp1", "keep"}:
            raise ValueError("invalid low-score TP rule")
        if config.tp_mode not in {"pine", "cash", "points"}:
            raise ValueError("TP mode must be pine, cash or points")
        if config.tp_mode == "pine":
            if config.tp_value is not None:
                raise ValueError("TP value must be omitted for Pine targets")
        else:
            if config.exit_mode != "atr_bracket":
                raise ValueError("cash/points TP requires atr_bracket exit mode")
            if config.tp_value is None or not math.isfinite(config.tp_value) or config.tp_value <= 0:
                raise ValueError("cash/points TP requires a positive finite TP value")
        if config.sl_mode not in {"pine", "cash", "points"}:
            raise ValueError("SL mode must be pine, cash or points")
        if config.sl_mode == "pine":
            if config.sl_value is not None:
                raise ValueError("SL value must be omitted for Pine stops")
        else:
            if config.exit_mode != "atr_bracket":
                raise ValueError("cash/points SL requires atr_bracket exit mode")
            if config.sl_value is None or not math.isfinite(config.sl_value) or config.sl_value <= 0:
                raise ValueError("cash/points SL requires a positive finite SL value")
        if any(not math.isfinite(x) or x <= 0 for x in (config.stake, config.point_size,
               config.starting_balance, config.atr_multiple, config.max_hours)):
            raise ValueError("stake, point size, balance, ATR multiple and time limit must be positive")
        self.config = config
        self.position = None
        self.pending = None
        self.previous_status = "WAIT"
        self.trades = []
        self.balance = self.peak = config.starting_balance
        self.previous_equity = config.starting_balance
        self.max_drawdown = 0.0
        self.next_id = 1
        self.trading_started = False
        self.entry_orders_submitted = 0
        self.entry_orders_filled = 0
        self.entry_orders_cancelled = 0

    def quote_exit(self, tick, direction):
        return tick.bid if direction == "BUY" else tick.ask

    def target_for_score(self, count, current=0):
        if self.config.exit_mode == "atr_bracket":
            return 1
        if self.config.exit_mode == "opposite_signal_only":
            return 0
        if count >= 5:
            return count - 4
        if self.config.exit_mode == "dynamic":
            return 0
        return (current or 1) if self.config.low_score_tp == "keep" else 1

    def pnl(self, position, price):
        sign = 1 if position["direction"] == "BUY" else -1
        return round(sign * (price - position["entry_price"]) / self.config.point_size * self.config.stake, 10)

    def _close(self, tick, seq, reason, events, state):
        position = self.position
        price = self.quote_exit(tick, position["direction"])
        pnl = self.pnl(position, price)
        record = {**position, "exit_time": tick.timestamp.isoformat(), "exit_seq": seq,
                  "exit_price": price, "exit_reason": reason, "pnl_cash": pnl,
                  "pnl_points": pnl / self.config.stake, "exit_bull_score": state["bull_score"],
                  "exit_bear_score": state["bear_score"], "holding_seconds":
                  (tick.timestamp - datetime.fromisoformat(position["entry_time"])).total_seconds(),
                  "timeout_delay_seconds": (max(0.0, (tick.timestamp - datetime.fromisoformat(position["entry_time"])).total_seconds()
                                               - self.config.max_hours * 3600)
                                            if self.config.exit_mode == "dynamic" else None)}
        self.trades.append(record)
        self.balance += pnl
        events.append({"trade_id": position["trade_id"], "kind": reason, "status": "FILLED",
                       "side": "SELL" if position["direction"] == "BUY" else "BUY",
                       "price": price, "fraction": 1.0, "stake_per_point": self.config.stake,
                       "pnl_cash": pnl})
        self.position = None
        return record

    def _open(self, pending, tick, seq, events, state, fill_price=None):
        direction = pending["direction"]
        count = pending["signal_bull_count"] if direction == "BUY" else pending["signal_bear_count"]
        target = self.target_for_score(count)
        entry_price = (fill_price if fill_price is not None else
                       tick.ask if direction == "BUY" else tick.bid)
        self.position = {**pending, "trade_id": str(self.next_id), "entry_time": tick.timestamp.isoformat(),
                         "entry_seq": seq, "entry_price": entry_price,
                         "entry_bull_score": state["bull_score"], "entry_bear_score": state["bear_score"],
                         "active_tp": target, "initial_stake_per_point": self.config.stake}
        p = self.position
        p["tp_mode"] = self.config.tp_mode
        p["fixed_tp_price"] = None
        if self.config.tp_mode != "pine":
            points = (self.config.tp_value / self.config.stake
                      if self.config.tp_mode == "cash" else self.config.tp_value)
            sign = 1 if direction == "BUY" else -1
            p["fixed_tp_price"] = p["entry_price"] + sign * points * self.config.point_size
        p["sl_mode"] = self.config.sl_mode
        p["fixed_stop_price"] = None
        if self.config.sl_mode != "pine":
            points = (self.config.sl_value / self.config.stake
                      if self.config.sl_mode == "cash" else self.config.sl_value)
            sign = 1 if direction == "BUY" else -1
            p["fixed_stop_price"] = p["entry_price"] - sign * points * self.config.point_size
        self.next_id += 1
        self.entry_orders_filled += 1
        events.append({"trade_id": p["trade_id"], "kind": "ENTRY", "status": "FILLED",
                       "side": direction, "price": p["entry_price"], "fraction": 1.0,
                       "stake_per_point": self.config.stake, "signal_seq": pending["signal_seq"],
                       "indicator_signal": pending["indicator_signal"],
                       "order_type": "LIMIT" if fill_price is not None else "MARKET"})

    @staticmethod
    def _limit_reached(pending, tick):
        if pending["direction"] == "BUY":
            return tick.ask <= pending["signal_price"]
        return tick.bid >= pending["signal_price"]

    def _cancel_pending(self, events, reason):
        pending = self.pending
        if pending is None:
            return
        events.append({"kind": "LIMIT_ENTRY", "status": "CANCELLED",
                       "side": pending["direction"], "price": pending["signal_price"],
                       "indicator_signal": pending["indicator_signal"],
                       "signal_seq": pending["signal_seq"], "reason": reason})
        self.entry_orders_cancelled += 1
        self.pending = None

    def step(self, tick, state, seq, *, trading=True, final=False):
        events, closed = [], []
        first_trading_tick = trading and not self.trading_started
        if not trading:
            return None
        self.trading_started = True
        if self.config.signal_timing in {"bar_close", "candle_file"}:
            signal_state = state.get("_closed_state") if state.get("_new_bar") else None
            status = signal_state["status"] if signal_state is not None and not first_trading_tick else "WAIT"
            fresh_signal = status != "WAIT"
        else:
            signal_state = state
            status = state["status"]
            fresh_signal = status != "WAIT" and status != self.previous_status
            self.previous_status = status

        # A decision from observation n can only fill at observation n+1 or later.
        if self.pending is not None:
            expiry = self.config.limit_expiry_minutes
            expired = (self.config.entry_mode == "pine_limit" and expiry is not None and
                       tick.timestamp >= datetime.fromisoformat(self.pending["submitted_time"]) +
                       timedelta(minutes=expiry))
            if expired:
                self._cancel_pending(events, "LIMIT_EXPIRED")
            should_fill = (self.pending is not None and
                           (self.config.entry_mode == "market" or
                            (seq > self.pending["submitted_seq"] and
                             self._limit_reached(self.pending, tick))))
            if should_fill:
                pending = self.pending
                self.pending = None
                if self.position is not None:
                    if not self.config.reverse_on_opposite_signal:
                        raise RuntimeError("unexpected opposite signal pending while reversal is disabled")
                    closed.append(self._close(tick, seq, "OPPOSITE_SIGNAL", events, state))
                fill_price = pending["signal_price"] if self.config.entry_mode == "pine_limit" else None
                self._open(pending, tick, seq, events, state, fill_price=fill_price)

        if self.position is not None and self.config.exit_mode != "opposite_signal_only":
            p = self.position
            direction = p["direction"]
            quote = self.quote_exit(tick, direction)
            sign = 1 if direction == "BUY" else -1
            score = state["bull_count"] if direction == "BUY" else state["bear_count"]
            opposite = "STRONG BEAR" if direction == "BUY" else "STRONG BULL"
            reason = None
            stop_price = p["fixed_stop_price"] if p["fixed_stop_price"] is not None else p["stop_price"]
            if self.config.exit_mode in {"dynamic", "atr_bracket"} and sign * (quote - stop_price) <= 1e-10:
                reason = f"SL_{self.config.sl_mode.upper()}" if p["fixed_stop_price"] is not None else "SL"
            elif self.config.exit_mode == "dynamic" and tick.timestamp >= datetime.fromisoformat(p["entry_time"]) + timedelta(hours=self.config.max_hours):
                reason = "TIMEOUT"
            elif p["active_tp"] and sign * (quote - (p["fixed_tp_price"] if p["fixed_tp_price"] is not None else p[f"tp{p['active_tp']}"])) >= -1e-10:
                reason = f"TP_{self.config.tp_mode.upper()}" if p["fixed_tp_price"] is not None else f"TP{p['active_tp']}"
            elif self.config.exit_mode == "dynamic" and state["market_bias"] == opposite:
                reason = "STRONG_OPPOSITE_BIAS"
            elif self.config.exit_mode == "dynamic" and score < 5:
                reason = "SCORE_BELOW_5"
            elif p["fixed_tp_price"] is None:
                target = self.target_for_score(score, p["active_tp"])
                if target != p["active_tp"]:
                    p["active_tp"] = target
                    events.append({"trade_id": p["trade_id"], "kind": f"TP{target}", "status": "AMENDED",
                                   "price": p[f"tp{target}"], "fraction": 1.0})
                if sign * (quote - p[f"tp{target}"]) >= 0:
                    reason = "DYNAMIC_TP"
            if reason:
                closed.append(self._close(tick, seq, reason, events, state))

        # A newly confirmed same-direction signal does not add exposure. It
        # moves the existing fixed bracket so both levels are measured from
        # that signal candle's close. Existing orders are evaluated first.
        if (fresh_signal and signal_state is not None and signal_state["ready"]
                and self.position is not None and self.config.reanchor_on_same_direction_signal):
            executed_direction = ({"BUY": "SELL", "SELL": "BUY"}[status]
                                  if self.config.invert_signals else status)
            if executed_direction == self.position["direction"]:
                if self.config.tp_mode != "points" or self.config.sl_mode != "points":
                    raise ValueError("same-direction bracket re-anchoring requires points TP and SL modes")
                reference = signal_state["close"]
                sign = 1 if executed_direction == "BUY" else -1
                old_stop = self.position["fixed_stop_price"]
                old_target = self.position["fixed_tp_price"]
                self.position["fixed_stop_price"] = reference - sign * self.config.sl_value * self.config.point_size
                self.position["fixed_tp_price"] = reference + sign * self.config.tp_value * self.config.point_size
                events.append({"trade_id": self.position["trade_id"], "kind": "BRACKET_REANCHOR",
                               "status": "AMENDED", "side": executed_direction,
                               "signal_seq": seq - 1 if self.config.signal_timing == "bar_close" else seq,
                               "reference_price": reference, "old_stop_price": old_stop,
                               "new_stop_price": self.position["fixed_stop_price"],
                               "old_target_price": old_target,
                               "new_target_price": self.position["fixed_tp_price"]})

        signal_ready = (fresh_signal and signal_state["ready"] and math.isfinite(signal_state["atr"])
                        and signal_state["atr"] > 0 and not final)
        if signal_ready:
            directional_score = (signal_state["bull_score"] if status == "BUY"
                                 else signal_state["bear_score"])
            if self.config.min_entry_score > 0 and directional_score <= self.config.min_entry_score:
                events.append({"kind": "ENTRY", "status": "FILTERED", "side": status,
                               "indicator_signal": status,
                               "signal_seq": seq - 1 if self.config.signal_timing == "bar_close" else seq,
                               "directional_score": directional_score})
                signal_ready = False
        if signal_ready:
            signal_volume = signal_state.get("volume", math.nan)
            volume_allowed = (
                math.isfinite(signal_volume)
                and (self.config.min_entry_volume is None or signal_volume >= self.config.min_entry_volume)
                and (self.config.max_entry_volume is None or signal_volume <= self.config.max_entry_volume)
            )
            if not volume_allowed:
                events.append({"kind": "ENTRY", "status": "FILTERED", "side": status,
                               "indicator_signal": status,
                               "signal_seq": seq - 1 if self.config.signal_timing == "bar_close" else seq,
                               "signal_volume": signal_volume,
                               "reason": "VOLUME_RANGE"})
                signal_ready = False
        if signal_ready:
            direction = ({"BUY": "SELL", "SELL": "BUY"}[status]
                         if self.config.invert_signals else status)
            can_enter = self.pending is None and (
                self.position is None or (
                    self.config.reverse_on_opposite_signal and self.position["direction"] != direction
                )
            )
            if can_enter:
                reference = signal_state["close"]
                risk = signal_state["atr"] * self.config.atr_multiple
                sign = 1 if direction == "BUY" else -1
                self.pending = {"direction": direction, "indicator_signal": status,
                                "signal_time": signal_state.get("_observation_timestamp", tick.timestamp.isoformat()),
                                "signal_seq": seq - 1 if self.config.signal_timing == "bar_close" else seq,
                                "submitted_seq": seq,
                                "submitted_time": tick.timestamp.isoformat(),
                                "signal_price": reference,
                                "signal_bull_count": signal_state["bull_count"],
                                "signal_bear_count": signal_state["bear_count"],
                                "stop_price": round(reference - sign * risk, self.config.price_decimals),
                                **{f"tp{i}": round(reference + sign * risk * i, self.config.price_decimals) for i in (1, 2, 3)}}
                self.entry_orders_submitted += 1
                if self.config.entry_mode == "market" and self.config.signal_timing == "bar_close":
                    pending = self.pending
                    self.pending = None
                    if self.position is not None:
                        closed.append(self._close(tick, seq, "OPPOSITE_SIGNAL", events, state))
                    self._open(pending, tick, seq, events, state)
                else:
                    events.append({"kind": ("LIMIT_ENTRY" if self.config.entry_mode == "pine_limit" else "ENTRY"),
                                   "status": "WORKING" if self.config.entry_mode == "pine_limit" else "PENDING",
                                   "side": direction, "price": reference,
                                   "indicator_signal": status,
                                   "signal_seq": self.pending["signal_seq"]})
            elif self.pending is not None:
                events.append({"kind": "ENTRY", "status": "IGNORED", "side": direction,
                               "indicator_signal": status,
                               "signal_seq": seq - 1 if self.config.signal_timing == "bar_close" else seq,
                               "reason": "PENDING_ENTRY_ALREADY_WORKING",
                               "working_signal_seq": self.pending["signal_seq"],
                               "working_price": self.pending["signal_price"]})

        if final:
            self._cancel_pending(events, "END_OF_DATA")
            if self.position:
                closed.append(self._close(tick, seq, "END_OF_DATA", events, state))

        p = self.position
        unrealized = self.pnl(p, self.quote_exit(tick, p["direction"])) if p else 0.0
        equity = self.balance + unrealized
        self.peak = max(self.peak, equity)
        drawdown = self.peak - equity
        self.max_drawdown = max(self.max_drawdown, drawdown)
        account_return = equity / self.previous_equity - 1 if self.previous_equity else None
        self.previous_equity = equity
        return {"timestamp": tick.timestamp.isoformat(), "tick_seq": seq, "bid": tick.bid, "ask": tick.ask,
                "spread": tick.ask - tick.bid, **{key: value for key, value in state.items() if not key.startswith("_")},
                "position": (1 if p["direction"] == "BUY" else -1) if p else 0,
                "pending_direction": self.pending["direction"] if self.pending else "",
                "pending_entry_price": self.pending["signal_price"] if self.pending else None,
                "pending_signal_time": self.pending["signal_time"] if self.pending else "",
                "trade_id": p["trade_id"] if p else "", "entry_price": p["entry_price"] if p else None,
                "stop_price": p["stop_price"] if p else None, "active_tp": p["active_tp"] if p else None,
                "tp_mode": self.config.tp_mode, "fixed_tp_price": p["fixed_tp_price"] if p else None,
                "sl_mode": self.config.sl_mode, "fixed_stop_price": p["fixed_stop_price"] if p else None,
                **{f"tp{i}": p[f"tp{i}"] if p else None for i in (1, 2, 3)},
                "balance": self.balance, "unrealized_pnl_cash": unrealized, "equity": equity,
                "account_return": account_return, "cumulative_account_return": equity / self.config.starting_balance - 1,
                "drawdown_cash": drawdown, "orders": events, "closed_positions": closed}


def read_ticks(path, price_scale=100.0):
    """Read dukascopy-node tick CSV without sorting away original equal-time order."""
    previous = None
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", newline="") as file:
        for row in csv.DictReader(file):
            timestamp = row["timestamp"]
            stamp = datetime.fromtimestamp(float(timestamp) / 1000, timezone.utc) if timestamp.isdigit() else datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("tick timestamps must be timezone-aware")
            tick = Tick(stamp, float(row["bidPrice"]) * price_scale, float(row["askPrice"]) * price_scale,
                        float(row["bidVolume"]), float(row["askVolume"]))
            if previous is not None and stamp < previous:
                raise ValueError("tick timestamps are out of order")
            if not all(math.isfinite(x) for x in (tick.bid, tick.ask, tick.bid_volume, tick.ask_volume)) or tick.bid <= 0 or tick.ask < tick.bid or min(tick.bid_volume, tick.ask_volume) < 0:
                raise ValueError(f"invalid tick at {stamp}")
            previous = stamp
            yield tick


def _csv_row(row):
    return {key: json.dumps(value, allow_nan=False) if isinstance(value, list) else
            None if isinstance(value, float) and not math.isfinite(value) else value for key, value in row.items()}


def run_file(path, out_dir, start, end, config=ReplayConfig(), price_scale=100.0,
             session_timezone="UTC", price_basis="mid", volume_basis="bid", candles_path=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    indicator = (CandleFileIndicator(candles_path, price_basis, config.atr_multiple)
                 if candles_path else TickIndicator(session_timezone, price_basis, volume_basis))
    engine = TickReplay(config)
    count = warmup = 0
    first = last = None
    hourly = Counter()
    gaps = []
    previous_tick = None
    tick_writer = candle_writer = None
    candle_row = None
    candle_events, candle_closed = [], []
    candle_previous_equity = config.starting_balance

    def flush_candle(file):
        nonlocal candle_writer, candle_events, candle_closed, candle_previous_equity
        if candle_row is None:
            return
        row = {**candle_row, "orders": candle_events, "closed_positions": candle_closed,
               "account_return": candle_row["equity"] / candle_previous_equity - 1 if candle_previous_equity else None}
        candle_previous_equity = row["equity"]
        if candle_writer is None:
            candle_writer = csv.DictWriter(file, fieldnames=list(row))
            candle_writer.writeheader()
        candle_writer.writerow(_csv_row(row))
        candle_events, candle_closed = [], []

    with gzip.open(out / "tick_results.csv.gz", "wt", newline="", compresslevel=1) as tick_file, \
            (out / "backtest_results.csv").open("w", newline="") as candle_file:
        # One-record lookahead marks the final observation; it is never used by the strategy.
        ticks = (tick for tick in read_ticks(path, price_scale) if tick.timestamp < end)
        current = next(ticks, None)
        seq = 0
        while current is not None:
            following = next(ticks, None)
            state = indicator.update(current)
            trading = current.timestamp >= start
            row = engine.step(current, state, seq, trading=trading, final=following is None)
            if not trading:
                warmup += 1
            else:
                if first is None:
                    first = current.timestamp
                last = current.timestamp
                hourly[current.timestamp.strftime("%Y-%m-%dT%H:00:00Z")] += 1
                if previous_tick is not None and (current.timestamp - previous_tick).total_seconds() > 300:
                    gaps.append({"from": previous_tick.isoformat(), "to": current.timestamp.isoformat(),
                                 "seconds": (current.timestamp - previous_tick).total_seconds()})
                previous_tick = current.timestamp
                if tick_writer is None:
                    tick_writer = csv.DictWriter(tick_file, fieldnames=list(row))
                    tick_writer.writeheader()
                tick_writer.writerow(_csv_row(row))
                if candle_row is not None and candle_row["candle_timestamp"] != row["candle_timestamp"]:
                    flush_candle(candle_file)
                candle_row = row
                candle_events.extend(row["orders"])
                candle_closed.extend(row["closed_positions"])
                count += 1
                if count % 250000 == 0:
                    print(f"Replayed {count:,} ticks through {last.isoformat()}", flush=True)
            current = following
            seq += 1
        flush_candle(candle_file)
    if count == 0:
        raise ValueError("no ticks in requested window")
    with (out / "trades.csv").open("w", newline="") as file:
        if engine.trades:
            writer = csv.DictWriter(file, fieldnames=list(engine.trades[0]))
            writer.writeheader()
            writer.writerows(engine.trades)
    with open(path, "rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    candle_digest = None
    if candles_path:
        with open(candles_path, "rb") as source:
            candle_digest = hashlib.file_digest(source, "sha256").hexdigest()
    summary = {"config": asdict(config), "source_file": str(Path(path).resolve()), "source_sha256": digest,
               "indicator_source": "TradingView HBDcHSB7 public Community V.02, inspected 2026-09-17",
               "indicator_candles_file": str(Path(candles_path).resolve()) if candles_path else None,
               "indicator_candles_sha256": candle_digest,
               "signal_rule": ("Existing five-minute CSV candle matched by close timestamp; submit on first later tick"
                               if config.signal_timing == "candle_file" else
                               "Confirmed five-minute candle close; submit entry on first tick of next candle"
                               if config.signal_timing == "bar_close" else
                               "Intrabar recalculation; submit entry after the signal tick"),
               "entry_rule": ("Working limit at Pine signal close; fill only when executable quote reaches it"
                              if config.entry_mode == "pine_limit" else
                              "Market entry on the configured next observation"),
               "risk_levels": "Earlier Pine ATR(14)*1.5 SL and reference targets, fixed at signal tick",
               "take_profit_rule": ("Pine ATR targets" if config.tp_mode == "pine" else
                                    f"{config.tp_value} {config.tp_mode}, fixed from actual entry fill"),
               "stop_loss_rule": ("Pine ATR stop" if config.sl_mode == "pine" else
                                  f"{config.sl_value} {config.sl_mode}, fixed from actual entry fill"),
               "price_basis": price_basis,
               "volume_basis": "candle_file_volume" if candles_path else volume_basis,
               "session_timezone": session_timezone,
               "price_scale": price_scale, "from": start.isoformat(), "to_exclusive": end.isoformat(),
               "first_tick": first.isoformat(), "last_tick": last.isoformat(), "ticks": count, "warmup_ticks": warmup,
               "trades": len(engine.trades), "net_cash": engine.balance - config.starting_balance,
               "entry_orders_submitted": engine.entry_orders_submitted,
               "entry_orders_filled": engine.entry_orders_filled,
               "entry_orders_cancelled": engine.entry_orders_cancelled,
               "profit_percent": (engine.balance / config.starting_balance - 1) * 100,
               "win_rate_percent": sum(t["pnl_cash"] > 0 for t in engine.trades) / len(engine.trades) * 100 if engine.trades else 0,
               "max_drawdown_cash": engine.max_drawdown,
               "exit_reasons": dict(Counter(t["exit_reason"] for t in engine.trades)),
               "limitations": ["Dukascopy quotes and bid quote-volume proxy, not the user's TradingView feed or IG fills",
                               "Limit queue priority, submission latency, positive slippage, financing and margin liquidation are not modeled",
                               ("Timeout uses first available quote at or after 24 hours; no invented weekend quotes"
                                if config.exit_mode == "dynamic" else
                                ("Configured full-position SL/TP plus opposite-crossover reversal; no timeout or score exits; final position liquidated at end of data"
                                 if config.reverse_on_opposite_signal else
                                 "Configured full-position SL/TP; opposite signals are ignored while a trade is open; no timeout or score exits; final position liquidated at end of data")
                                if config.exit_mode == "atr_bracket" else
                                "Timeout/SL/score exits disabled; final open position liquidated at end of data"),
                               "Price-point conversion is explicit config, not inferred from decimal precision"]}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "data_quality.json").write_text(json.dumps({"hourly_tick_counts": dict(hourly), "gaps_over_5_minutes": gaps}, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticks_csv")
    parser.add_argument("--from-time", default="2025-01-01T00:00:00+00:00")
    parser.add_argument("--to-time", default="2025-02-01T00:00:00+00:00")
    parser.add_argument("--out-dir", default="backtest-results/usdjpy-2025-01-ticks")
    parser.add_argument("--size", type=float, default=10.0)
    parser.add_argument("--exit-mode", choices=["dynamic", "opposite_signal_only", "opposite_signal_or_tp", "atr_bracket"], default="atr_bracket")
    parser.add_argument("--low-score-tp", choices=["tp1", "keep"], default="tp1")
    parser.add_argument("--tp-mode", choices=["pine", "cash", "points"], default="points",
                        help="Configured point distance (default), Pine TP1, or GBP profit")
    parser.add_argument("--tp-value", type=float, default=10.0,
                        help="Positive GBP amount or point distance (default: 10 points)")
    parser.add_argument("--sl-mode", choices=["pine", "cash", "points"], default="points",
                        help="Configured point distance (default), Pine ATR stop, or GBP loss")
    parser.add_argument("--sl-value", type=float, default=10.0,
                        help="Positive GBP amount or point distance (default: 10 points)")
    parser.add_argument("--reverse-on-opposite-signal", action="store_true",
                        help="Close and reverse on an opposite crossover; disabled by default")
    parser.add_argument("--invert-signals", action="store_true",
                        help="Proof of concept: BUY signals execute short and SELL signals execute long")
    parser.add_argument("--signal-timing", choices=["intrabar", "bar_close", "candle_file"], default="intrabar",
                        help="Recalculate the live five-minute candle on every tick by default")
    parser.add_argument("--candles-csv",
                        help="Use this existing five-minute CSV as the authoritative indicator stream")
    parser.add_argument("--min-entry-score", type=float, default=0,
                        help="Require directional Bull/Bear Score to be strictly above this percentage")
    parser.add_argument("--min-entry-volume", type=float,
                        help="Minimum completed signal-candle volume, inclusive")
    parser.add_argument("--max-entry-volume", type=float,
                        help="Maximum completed signal-candle volume, inclusive")
    parser.add_argument("--reanchor-on-same-direction-signal", action="store_true",
                        help="Reset an open trade's fixed SL/TP around each new same-direction signal price")
    parser.add_argument("--entry-mode", choices=["market", "pine_limit"], default="pine_limit",
                        help="Place a working limit at the Pine entry price by default")
    parser.add_argument("--limit-expiry-minutes", type=float,
                        help="Cancel an unfilled Pine limit after this many minutes")
    parser.add_argument("--point-size", type=float, default=1.0)
    parser.add_argument("--price-scale", type=float, default=100)
    parser.add_argument("--session-timezone", default="UTC")
    parser.add_argument("--price-basis", choices=["bid", "ask", "mid"], default="mid")
    parser.add_argument("--volume-basis", choices=["bid", "ask", "tick_count"], default="bid")
    args = parser.parse_args()
    signal_timing = "candle_file" if args.candles_csv else args.signal_timing
    result = run_file(args.ticks_csv, args.out_dir, datetime.fromisoformat(args.from_time),
                      datetime.fromisoformat(args.to_time), ReplayConfig(stake=args.size, point_size=args.point_size,
                      exit_mode=args.exit_mode, low_score_tp=args.low_score_tp,
                      tp_mode=args.tp_mode, tp_value=None if args.tp_mode == "pine" else args.tp_value,
                      sl_mode=args.sl_mode, sl_value=None if args.sl_mode == "pine" else args.sl_value,
                      reverse_on_opposite_signal=args.reverse_on_opposite_signal,
                      invert_signals=args.invert_signals, signal_timing=signal_timing,
                      min_entry_score=args.min_entry_score, entry_mode=args.entry_mode,
                      limit_expiry_minutes=args.limit_expiry_minutes,
                      min_entry_volume=args.min_entry_volume,
                      max_entry_volume=args.max_entry_volume,
                      reanchor_on_same_direction_signal=args.reanchor_on_same_direction_signal),
                      args.price_scale, args.session_timezone, args.price_basis, args.volume_basis,
                      args.candles_csv)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
