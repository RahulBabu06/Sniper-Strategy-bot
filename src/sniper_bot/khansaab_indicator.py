from __future__ import annotations

from dataclasses import dataclass

from sniper_bot.config import StrategyConfig
from sniper_bot.indicators import adx, atr, ema, macd, rsi, session_vwap, sma
from sniper_bot.market import Candle


@dataclass(frozen=True)
class KhanSaabBar:
    index: int
    candle: Candle
    ema9: float
    ema21: float
    ema50: float
    vwap: float
    atr: float
    rsi: float
    rsi5m: float
    macd_main: float
    macd_signal: float
    macd_histogram: float
    adx: float
    volume_average: float
    bull_score: float
    bear_score: float
    bias: str
    status: str
    trend_strength: str
    trigger_buy: bool
    trigger_sell: bool
    last_signal_state: int
    entry_price: float
    stop_price: float
    targets: tuple[float, float, float, float, float]
    target_hits: tuple[bool, bool, bool, bool, bool]
    is_retest: bool


def calculate_khansaab_bars(
    candles: list[Candle],
    config: StrategyConfig,
    rsi5m_values: list[float] | None = None,
) -> list[KhanSaabBar]:
    if not candles:
        return []

    closes = [candle.close_mid for candle in candles]
    volumes = [candle.volume for candle in candles]
    ema9_values = ema(closes, 9)
    ema21_values = ema(closes, 21)
    ema50_values = ema(closes, 50)
    vwap_values = session_vwap(candles)
    atr_values = atr(candles, 14)
    rsi_values = rsi(closes, 14)
    macd_main_values, macd_signal_values, macd_hist_values = macd(closes, 12, 26, 9)
    adx_values = adx(candles, 14)
    vol_avg_values = sma(volumes, 20)

    if not atr_values or not rsi_values or not adx_values:
        return []

    bars: list[KhanSaabBar] = []
    last_signal_state = 0
    entry_price = 0.0
    stop_price = 0.0
    targets = (0.0, 0.0, 0.0, 0.0, 0.0)
    target_hits = (False, False, False, False, False)

    for index, candle in enumerate(candles):
        ema9_now = ema9_values[index]
        ema21_now = ema21_values[index]
        ema50_now = ema50_values[index]
        atr_now = atr_values[index]
        rsi_now = rsi_values[index]
        adx_now = adx_values[index]
        macd_main = macd_main_values[index]
        macd_signal = macd_signal_values[index]
        macd_hist = macd_hist_values[index]
        vwap_now = vwap_values[index]
        vol_avg = vol_avg_values[index]
        rsi5m_now = _rsi5m_at(index, rsi_values, rsi5m_values)

        bull_score, bear_score = _scores(
            candle=candle,
            vwap=vwap_now,
            rsi_value=rsi_now,
            rsi5m=rsi5m_now,
            macd_main=macd_main,
            macd_signal=macd_signal,
            ema9=ema9_now,
            ema21=ema21_now,
            adx_value=adx_now,
            volume_average=vol_avg,
        )
        bias = _bias_text(bull_score, bear_score)

        buy_cond = index > 0 and ema9_values[index - 1] <= ema21_values[index - 1] and ema9_now > ema21_now
        sell_cond = index > 0 and ema9_values[index - 1] >= ema21_values[index - 1] and ema9_now < ema21_now
        trigger_buy = buy_cond and last_signal_state <= 0
        trigger_sell = sell_cond and last_signal_state >= 0

        if trigger_buy or trigger_sell:
            last_signal_state = 1 if trigger_buy else -1
            entry_price = candle.close_mid
            # Execution convention requested for the bot: a symmetric fixed
            # point bracket around the KhanSaab signal/entry price.
            fixed_points = getattr(config, "fixed_bracket_points", 10.0)
            point_size = getattr(config, "spread_bet_point_size", 1.0)
            risk = fixed_points * point_size
            if trigger_buy:
                stop_price = entry_price - risk
                targets = tuple(entry_price + (risk * multiple) for multiple in (1.0, 2.0, 3.0, 4.0, 5.0))
            else:
                stop_price = entry_price + risk
                targets = tuple(entry_price - (risk * multiple) for multiple in (1.0, 2.0, 3.0, 4.0, 5.0))
            target_hits = (False, False, False, False, False)

        # Pine's ``lastSignalState`` changes only on an EMA crossover.  Stops
        # and targets are execution assumptions in this project and must not
        # feed back into the indicator state.
        if last_signal_state == 1:
            target_hits = tuple(hit or candle.high_mid >= target for hit, target in zip(target_hits, targets))
        elif last_signal_state == -1:
            target_hits = tuple(hit or candle.low_mid <= target for hit, target in zip(target_hits, targets))

        is_retest = (
            (last_signal_state == 1 and candle.low_mid <= ema9_now and candle.low_mid > ema21_now)
            or (last_signal_state == -1 and candle.high_mid >= ema9_now and candle.high_mid < ema21_now)
        )

        bars.append(
            KhanSaabBar(
                index=index,
                candle=candle,
                ema9=ema9_now,
                ema21=ema21_now,
                ema50=ema50_now,
                vwap=vwap_now,
                atr=atr_now,
                rsi=rsi_now,
                rsi5m=rsi5m_now,
                macd_main=macd_main,
                macd_signal=macd_signal,
                macd_histogram=macd_hist,
                adx=adx_now,
                volume_average=vol_avg,
                bull_score=bull_score,
                bear_score=bear_score,
                bias=bias,
                status="BUY" if trigger_buy else ("SELL" if trigger_sell else "WAIT"),
                trend_strength="STRONG" if adx_now > 25 else "WEAK",
                trigger_buy=trigger_buy,
                trigger_sell=trigger_sell,
                last_signal_state=last_signal_state,
                entry_price=entry_price,
                stop_price=stop_price,
                targets=targets,
                target_hits=target_hits,
                is_retest=is_retest,
            )
        )

    return bars


def _rsi5m_at(index: int, rsi_values: list[float], rsi5m_values: list[float] | None) -> float:
    if rsi5m_values and index < len(rsi5m_values):
        return rsi5m_values[index]
    if index == 0:
        return rsi_values[0]
    return rsi_values[index - 1]


def _scores(
    *,
    candle: Candle,
    vwap: float,
    rsi_value: float,
    rsi5m: float,
    macd_main: float,
    macd_signal: float,
    ema9: float,
    ema21: float,
    adx_value: float,
    volume_average: float,
) -> tuple[float, float]:
    bull_checks = [
        candle.close_mid > vwap,
        rsi_value > 50,
        macd_main > macd_signal,
        ema9 > ema21,
        adx_value > 25 and candle.close_mid > ema9,
        candle.volume > volume_average and candle.close_mid > candle.open_mid,
        rsi5m > 50,
    ]
    bear_checks = [
        candle.close_mid < vwap,
        rsi_value < 50,
        macd_main < macd_signal,
        ema9 < ema21,
        adx_value > 25 and candle.close_mid < ema9,
        candle.volume > volume_average and candle.close_mid < candle.open_mid,
        rsi5m < 50,
    ]
    return _score(bull_checks), _score(bear_checks)


def _score(checks: list[bool]) -> float:
    return (sum(1 for check in checks if check) / 7.0) * 100.0


def _bias_text(bull_score: float, bear_score: float) -> str:
    if bull_score - bear_score >= 40.0:
        return "STRONG BULL"
    if bear_score - bull_score >= 40.0:
        return "STRONG BEAR"
    if bull_score > bear_score:
        return "MILD BULL"
    return "MILD BEAR"
