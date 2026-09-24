"""Incremental five-minute evaluation of the public KhanSaab V.02 signals.

The current public version has no risk levels. Execution adds the separately
agreed ATR levels. Preview updates roll back to the last committed bar like Pine.
"""

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sniper_bot.csv_loader import load_candles_csv
from sniper_bot.khansaab_indicator import calculate_khansaab_bars
from sniper_bot.market import Candle


@dataclass(frozen=True, slots=True)
class Tick:
    timestamp: datetime
    bid: float
    ask: float
    bid_volume: float = 0.0
    ask_volume: float = 0.0


class RMA:
    def __init__(self, period=14):
        self.period = period
        self.count = 0
        self.total = 0.0
        self.value = math.nan

    def preview(self, value):
        if not math.isfinite(value):
            return self.value
        if self.count < self.period:
            return (self.total + value) / self.period if self.count + 1 == self.period else math.nan
        return (self.value * (self.period - 1) + value) / self.period

    def commit(self, value):
        result = self.preview(value)
        if math.isfinite(value):
            self.count += 1
            self.total += value
            self.value = result


class TickIndicator:
    def __init__(self, session_timezone="UTC", price_basis="bid", volume_basis="bid"):
        self.zone = ZoneInfo(session_timezone)
        if price_basis not in {"bid", "mid", "ask"} or volume_basis not in {"bid", "ask", "tick_count"}:
            raise ValueError("invalid indicator price or volume basis")
        self.price_basis, self.volume_basis = price_basis, volume_basis
        self.bucket = None
        self.bar = None
        self.previous = None
        self.last_signal_state = 0
        self.ema = {}
        self.rma = {name: RMA() for name in ("atr", "gain", "loss", "tr", "plus", "minus", "adx")}
        self.volumes = deque(maxlen=19)
        self.session = None
        self.vwap_weight = self.vwap_volume = 0.0
        self.last = None
        self.closed_bars = 0

    def update(self, tick):
        bucket = tick.timestamp.replace(minute=(tick.timestamp.minute // 5) * 5, second=0, microsecond=0)
        price = tick.bid if self.price_basis == "bid" else tick.ask if self.price_basis == "ask" else (tick.bid + tick.ask) / 2
        volume = tick.bid_volume if self.volume_basis == "bid" else tick.ask_volume if self.volume_basis == "ask" else 1.0
        closed_state = None
        new_bar = bucket != self.bucket
        if bucket != self.bucket:
            if self.bar is not None:
                closed_state = {key: value for key, value in self.last.items()
                                if key != "_closed_state"}
                self._commit()
            self.bucket = bucket
            self.bar = {"open": price, "high": price, "low": price, "close": price, "volume": volume}
        else:
            self.bar.update(high=max(self.bar["high"], price), low=min(self.bar["low"], price),
                            close=price, volume=self.bar["volume"] + volume)
        self.last = self._preview()
        self.last["_new_bar"] = new_bar
        self.last["_closed_state"] = closed_state
        self.last["_observation_timestamp"] = tick.timestamp.isoformat()
        return self.last

    def _preview(self):
        bar, prev = self.bar, self.previous
        close = bar["close"]
        ema = {n: close if n not in self.ema else self.ema[n] + 2 / (n + 1) * (close - self.ema[n])
               for n in (9, 21, 50, 12, 26)}
        macd = ema[12] - ema[26]
        macd_signal = macd if "macd" not in self.ema else self.ema["macd"] + 0.2 * (macd - self.ema["macd"])
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - prev["close"]),
                 abs(bar["low"] - prev["close"])) if prev else bar["high"] - bar["low"]
        change = close - prev["close"] if prev else math.nan
        up = bar["high"] - prev["high"] if prev else math.nan
        down = prev["low"] - bar["low"] if prev else math.nan
        inputs = {"atr": tr, "gain": max(change, 0), "loss": max(-change, 0),
                  "tr": tr if prev else math.nan,
                  "plus": (up if up > down and up > 0 else 0.0) if prev else math.nan,
                  "minus": (down if down > up and down > 0 else 0.0) if prev else math.nan}
        values = {key: self.rma[key].preview(value) for key, value in inputs.items()}
        gain, loss = values["gain"], values["loss"]
        rsi = (100 * gain / (gain + loss)) if math.isfinite(gain + loss) and gain + loss > 0 else math.nan
        di_sum = values["plus"] + values["minus"]
        dx = (100 * abs(values["plus"] - values["minus"]) / di_sum if di_sum else 0.0)
        inputs["adx"] = dx
        adx = self.rma["adx"].preview(dx)
        session = self.bucket.astimezone(self.zone).date()
        base_weight, base_volume = (self.vwap_weight, self.vwap_volume) if session == self.session else (0.0, 0.0)
        weight = base_weight + (bar["high"] + bar["low"] + close) / 3 * bar["volume"]
        total_volume = base_volume + bar["volume"]
        vwap = weight / total_volume if total_volume else math.nan
        vol_avg = (sum(self.volumes) + bar["volume"]) / 20 if len(self.volumes) == 19 else math.nan
        previous_rsi = prev["rsi"] if prev else math.nan
        bull = sum((close > vwap, rsi > 50, macd > macd_signal, ema[9] > ema[21],
                    adx > 25 and close > ema[9], bar["volume"] > vol_avg and close > bar["open"], previous_rsi > 50))
        bear = sum((close < vwap, rsi < 50, macd < macd_signal, ema[9] < ema[21],
                    adx > 25 and close < ema[9], bar["volume"] > vol_avg and close < bar["open"], previous_rsi < 50))
        buy = bool(prev and self.ema[9] <= self.ema[21] and ema[9] > ema[21] and self.last_signal_state <= 0)
        sell = bool(prev and self.ema[9] >= self.ema[21] and ema[9] < ema[21] and self.last_signal_state >= 0)
        bias = "STRONG BULL" if bull - bear >= 3 else "STRONG BEAR" if bear - bull >= 3 else "MILD BULL" if bull > bear else "MILD BEAR"
        return {**bar, "candle_timestamp": self.bucket.isoformat(), "ema9": ema[9], "ema21": ema[21],
                "ema50": ema[50], "macd_main": macd, "macd_signal": macd_signal, "atr": values["atr"],
                "rsi": rsi, "rsi5m": previous_rsi, "adx": adx, "vwap": vwap, "volume_average": vol_avg,
                "bull_count": bull, "bear_count": bear, "bull_score": bull / 7 * 100, "bear_score": bear / 7 * 100,
                "market_bias": bias, "status": "BUY" if buy else "SELL" if sell else "WAIT",
                "ready": all(math.isfinite(v) for v in (values["atr"], rsi, previous_rsi, adx, vol_avg)),
                "_ema": {**ema, "macd": macd_signal}, "_rma_inputs": inputs,
                "_session": session, "_weight": weight, "_volume": total_volume}

    def _commit(self):
        result = self.last
        self.ema = result["_ema"]
        for key, value in result["_rma_inputs"].items():
            self.rma[key].commit(value)
        self.previous = {key: value for key, value in result.items()
                         if key not in {"_closed_state", "_new_bar"}}
        self.volumes.append(result["volume"])
        self.session, self.vwap_weight, self.vwap_volume = result["_session"], result["_weight"], result["_volume"]
        if result["status"] != "WAIT":
            self.last_signal_state = 1 if result["status"] == "BUY" else -1
        self.closed_bars += 1


class CandleFileIndicator:
    """Expose completed five-minute CSV bars on the historical tick timeline."""

    def __init__(self, path, price_basis="bid", atr_multiple=1.5):
        if price_basis not in {"bid", "mid", "ask"}:
            raise ValueError("invalid indicator price basis")
        candles = [_basis_candle(candle, price_basis) for candle in load_candles_csv(path)]
        bars = calculate_khansaab_bars(
            candles,
            SimpleNamespace(stop_atr_multiple=atr_multiple),
        )
        self.states = [_bar_state(bar) for bar in bars]
        self.available_index = -1
        self.last = None

    def update(self, tick):
        previous_index = self.available_index
        while (self.available_index + 1 < len(self.states)
               and datetime.fromisoformat(self.states[self.available_index + 1]["candle_timestamp"])
               + timedelta(minutes=5) <= tick.timestamp):
            self.available_index += 1
        if self.available_index < 0:
            return _empty_file_state(tick)
        self.last = dict(self.states[self.available_index])
        new_bar = self.available_index != previous_index
        self.last["_new_bar"] = new_bar
        self.last["_closed_state"] = dict(self.last) if new_bar else None
        self.last["_observation_timestamp"] = tick.timestamp.isoformat()
        if new_bar:
            completed = datetime.fromisoformat(self.last["candle_timestamp"]) + timedelta(minutes=5)
            self.last["_closed_state"]["_observation_timestamp"] = completed.isoformat()
        return self.last


def _basis_candle(candle, price_basis):
    if price_basis == "bid":
        values = (candle.open_bid, candle.high_bid, candle.low_bid, candle.close_bid)
    elif price_basis == "ask":
        values = (candle.open_ask, candle.high_ask, candle.low_ask, candle.close_ask)
    else:
        values = (candle.open_mid, candle.high_mid, candle.low_mid, candle.close_mid)
    return Candle(candle.timestamp, values[0], values[0], values[1], values[1],
                  values[2], values[2], values[3], values[3], candle.volume)


def _bar_state(bar):
    candle = bar.candle
    return {
        "open": candle.open_mid,
        "high": candle.high_mid,
        "low": candle.low_mid,
        "close": candle.close_mid,
        "volume": candle.volume,
        "candle_timestamp": candle.timestamp.isoformat(),
        "ema9": bar.ema9,
        "ema21": bar.ema21,
        "ema50": bar.ema50,
        "macd_main": bar.macd_main,
        "macd_signal": bar.macd_signal,
        "atr": bar.atr,
        "rsi": bar.rsi,
        "rsi5m": bar.rsi5m,
        "adx": bar.adx,
        "vwap": bar.vwap,
        "volume_average": bar.volume_average,
        "bull_count": round(bar.bull_score * 7 / 100),
        "bear_count": round(bar.bear_score * 7 / 100),
        "bull_score": bar.bull_score,
        "bear_score": bar.bear_score,
        "market_bias": bar.bias,
        "status": bar.status,
        "ready": all(math.isfinite(value) for value in
                     (bar.atr, bar.rsi, bar.rsi5m, bar.adx, bar.volume_average)),
    }


def _empty_file_state(tick):
    nan = math.nan
    return {
        "open": nan, "high": nan, "low": nan, "close": nan, "volume": nan,
        "candle_timestamp": "", "ema9": nan, "ema21": nan, "ema50": nan,
        "macd_main": nan, "macd_signal": nan, "atr": nan, "rsi": nan,
        "rsi5m": nan, "adx": nan, "vwap": nan, "volume_average": nan,
        "bull_count": 0, "bear_count": 0, "bull_score": 0.0, "bear_score": 0.0,
        "market_bias": "MILD BEAR", "status": "WAIT", "ready": False,
        "_new_bar": False, "_closed_state": None,
        "_observation_timestamp": tick.timestamp.isoformat(),
    }
