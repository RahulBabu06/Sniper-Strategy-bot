from __future__ import annotations

from sniper_bot.market import Candle


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def rsi(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("RSI period must be positive")
    if len(values) < period + 1:
        return [float("nan")] * len(values)

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    output = [float("nan")] * period

    def value_from(avg_gain: float, avg_loss: float) -> float:
        if avg_gain == 0 and avg_loss == 0:
            return float("nan")
        if avg_loss == 0:
            return 100.0
        relative_strength = avg_gain / avg_loss
        return 100 - (100 / (1 + relative_strength))

    output.append(value_from(avg_gain, avg_loss))
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        output.append(value_from(avg_gain, avg_loss))
    return output


def atr(candles: list[Candle], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    if len(candles) < period:
        return [float("nan")] * len(candles)

    true_ranges: list[float] = [candles[0].high_mid - candles[0].low_mid]
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(
            max(
                current.high_mid - current.low_mid,
                abs(current.high_mid - previous.close_mid),
                abs(current.low_mid - previous.close_mid),
            )
        )

    first = sum(true_ranges[:period]) / period
    output = [first]
    for true_range in true_ranges[period:]:
        output.append(((output[-1] * (period - 1)) + true_range) / period)
    return [float("nan")] * (period - 1) + output


def macd(values: list[float], fast_period: int, slow_period: int, signal_period: int) -> tuple[list[float], list[float], list[float]]:
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal = ema(line, signal_period)
    histogram = [line_value - signal_value for line_value, signal_value in zip(line, signal)]
    return line, signal, histogram


def adx(candles: list[Candle], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("ADX period must be positive")
    if len(candles) < period + 2:
        return [float("nan")] * len(candles)

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        up_move = current.high_mid - previous.high_mid
        down_move = previous.low_mid - current.low_mid
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        true_ranges.append(
            max(
                current.high_mid - current.low_mid,
                abs(current.high_mid - previous.close_mid),
                abs(current.low_mid - previous.close_mid),
            )
        )

    if len(true_ranges) < period:
        return [float("nan")] * len(candles)

    tr_smooth = sum(true_ranges[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])
    dx_values: list[float] = []

    def dx_from(plus_value: float, minus_value: float, tr_value: float) -> float:
        if tr_value == 0:
            return 0.0
        plus_di = 100 * plus_value / tr_value
        minus_di = 100 * minus_value / tr_value
        denominator = plus_di + minus_di
        if denominator == 0:
            return 0.0
        return 100 * abs(plus_di - minus_di) / denominator

    dx_values.append(dx_from(plus_smooth, minus_smooth, tr_smooth))
    for tr, plus, minus in zip(true_ranges[period:], plus_dm[period:], minus_dm[period:]):
        tr_smooth = tr_smooth - (tr_smooth / period) + tr
        plus_smooth = plus_smooth - (plus_smooth / period) + plus
        minus_smooth = minus_smooth - (minus_smooth / period) + minus
        dx_values.append(dx_from(plus_smooth, minus_smooth, tr_smooth))

    if len(dx_values) < period:
        return [float("nan")] * len(candles)

    first_adx = sum(dx_values[:period]) / period
    output = [first_adx]
    for dx in dx_values[period:]:
        output.append(((output[-1] * (period - 1)) + dx) / period)
    return [float("nan")] * (len(candles) - len(output)) + output


def rolling_vwap(candles: list[Candle], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("VWAP period must be positive")
    output: list[float] = []
    for index in range(len(candles)):
        window = candles[max(0, index - period + 1) : index + 1]
        weighted_total = sum(((c.high_mid + c.low_mid + c.close_mid) / 3) * max(c.volume, 1) for c in window)
        volume_total = sum(max(c.volume, 1) for c in window)
        output.append(weighted_total / volume_total)
    return output


def session_vwap(candles: list[Candle]) -> list[float]:
    output: list[float] = []
    weighted_total = 0.0
    volume_total = 0.0
    current_session = None
    for candle in candles:
        session = candle.timestamp.date()
        if session != current_session:
            current_session = session
            weighted_total = 0.0
            volume_total = 0.0
        volume = max(candle.volume, 0)
        typical = (candle.high_mid + candle.low_mid + candle.close_mid) / 3
        weighted_total += typical * volume
        volume_total += volume
        output.append(weighted_total / volume_total if volume_total else float("nan"))
    return output


def sma(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("SMA period must be positive")
    output: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        output.append(running / period if index >= period - 1 else float("nan"))
    return output
