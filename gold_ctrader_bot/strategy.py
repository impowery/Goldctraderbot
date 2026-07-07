"""Strategy logic: EMA20 + ADX + ATR — ported from gold_bot.py"""

import numpy as np
from settings import EMA_PERIOD, ADX_THRESHOLD, ATR_PERIOD


def calc_ema(prices: list[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_sma(prices: list[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def calc_adx(high: list[float], low: list[float], close: list[float], period: int = 14) -> float:
    n = len(high)
    if n < period * 2:
        return 0.0

    tr_values, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        tr_values.append(tr)
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)
        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    atr = sum(tr_values[:period]) / period
    pdm = sum(plus_dm[:period]) / period
    mdm = sum(minus_dm[:period]) / period
    dx_values = []
    for i in range(period, len(tr_values)):
        atr = (atr * (period - 1) + tr_values[i]) / period
        pdm = (pdm * (period - 1) + plus_dm[i]) / period
        mdm = (mdm * (period - 1) + minus_dm[i]) / period
        pdi = 100 * pdm / atr if atr > 0 else 0
        mdi = 100 * mdm / atr if atr > 0 else 0
        if (pdi + mdi) > 0:
            dx_values.append(abs(pdi - mdi) / (pdi + mdi) * 100)

    if len(dx_values) < period:
        return dx_values[-1] if dx_values else 0.0
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def calc_atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> float:
    if len(high) < period + 1:
        return 0.0
    tr_values = []
    for i in range(1, len(high)):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        tr_values.append(tr)
    return calc_sma(tr_values, period)


def should_enter(close: list[float], high: list[float], low: list[float], today_high: float = 0, today_low: float = 0) -> tuple[bool, str]:
    if len(close) < 50:
        return False, "not enough data"

    ema = calc_ema(close, EMA_PERIOD)
    adx = calc_adx(high, low, close, ATR_PERIOD)
    atr = calc_atr(high, low, close, ATR_PERIOD)
    price = close[-1]

    if adx < ADX_THRESHOLD:
        return False, f"ADX {adx:.1f} < {ADX_THRESHOLD}"

    # Distance filter (was 1.5xATR, REMOVED — blocked all strong trends).
    # Pullback filter in gold_mcp_bot_remote.py (PULLBACK_MAX_MULT=1.0xATR)
    # handles this better — it's checked AFTER should_enter, with M30 trend
    # filter confirming direction. This strategy-level filter was redundant
    # and blocked legitimate trend entries (e.g. ADX=48, price far from EMA
    # = strong trend, but bot skipped it).

    daily_range = today_high - today_low
    if daily_range >= atr:
        range_pos = (price - today_low) / daily_range
        # Relaxed from 0.7/0.3 to 0.95/0.05 — was blocking strong trend entries
        # Bot can now buy up to 95% of daily range (was 70%)
        if price > ema and range_pos > 0.95:
            return False, f"LONG skip at {range_pos*100:.0f}% daily range"
        if price < ema and range_pos < 0.05:
            return False, f"SHORT skip at {range_pos*100:.0f}% daily range"

    # FLIPPED: price > EMA → SHORT (sell overextension), price < EMA → LONG (buy oversold)
    if price > ema:
        return True, f"SHORT ema={ema:.1f} adx={adx:.1f} atr={atr:.2f}"
    else:
        return True, f"LONG ema={ema:.1f} adx={adx:.1f} atr={atr:.2f}"
