"""Strategy: RSI(14) + Stochastic(14,3,3) + M30 EMA41 trend filter.

Entry LONG:  RSI < 20 AND Stoch %K < 20 (and M30 EMA41 rising or flat)
Entry SHORT: RSI > 80 AND Stoch %K > 80 (and M30 EMA41 falling or flat)
Exit:        RSI crosses back to 50

No EMA, no ADX, no ATR for entry signals.
M30 EMA41 used ONLY for trend direction filter.
"""

import numpy as np


def calc_rsi(prices: list[float], period: int = 14) -> float:
    """Calculate RSI using Wilder's smoothing."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_stochastic(high: list[float], low: list[float], close: list[float],
                    k_period: int = 14, d_period: int = 3) -> tuple:
    """Calculate Stochastic %K and %D. Returns (k, d)."""
    if len(close) < k_period:
        return 50.0, 50.0

    k_values = []
    for i in range(-min(d_period, len(close) - k_period), 0):
        if len(high) >= k_period + abs(i):
            h = max(high[i - k_period:i])
            l = min(low[i - k_period:i])
            if h != l:
                k_values.append(((close[i] - l) / (h - l)) * 100.0)
            else:
                k_values.append(50.0)

    if not k_values:
        # Just current K
        recent_high = max(high[-k_period:])
        recent_low = min(low[-k_period:])
        if recent_high == recent_low:
            return 50.0, 50.0
        k = ((close[-1] - recent_low) / (recent_high - recent_low)) * 100.0
        return k, k

    k = k_values[-1]
    d = float(np.mean(k_values))
    return k, d


def calc_ema(prices: list[float], period: int) -> float:
    """Calculate EMA."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    multiplier = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def should_enter(close: list[float], high: list[float], low: list[float],
                 today_high: float = 0, today_low: float = 0,
                 m30_ema: float = 0, m30_ema_prev: float = 0,
                 rsi_oversold: float = 20, rsi_overbought: float = 80,
                 stoch_oversold: float = 20, stoch_overbought: float = 80
                 ) -> tuple:
    """Returns (should_enter: bool, reason: str, rsi: float, stoch_k: float)."""
    if len(close) < 20:
        return False, "not enough data", 0.0, 0.0

    rsi = calc_rsi(close, 14)
    stoch_k, stoch_d = calc_stochastic(high, low, close, 14, 3)

    # M30 EMA41 trend filter
    m30_rising = m30_ema > m30_ema_prev if (m30_ema > 0 and m30_ema_prev > 0) else True
    m30_falling = m30_ema < m30_ema_prev if (m30_ema > 0 and m30_ema_prev > 0) else True

    # LONG: RSI < oversold AND Stoch < oversold
    if rsi < rsi_oversold and stoch_k < stoch_oversold:
        if m30_rising or m30_ema == 0:
            return True, f"LONG rsi={rsi:.1f} stoch={stoch_k:.1f}", rsi, stoch_k
        else:
            return False, f"LONG blocked (M30 falling) rsi={rsi:.1f} stoch={stoch_k:.1f}", rsi, stoch_k

    # SHORT: RSI > overbought AND Stoch > overbought
    if rsi > rsi_overbought and stoch_k > stoch_overbought:
        if m30_falling or m30_ema == 0:
            return True, f"SHORT rsi={rsi:.1f} stoch={stoch_k:.1f}", rsi, stoch_k
        else:
            return False, f"SHORT blocked (M30 rising) rsi={rsi:.1f} stoch={stoch_k:.1f}", rsi, stoch_k

    return False, f"No signal rsi={rsi:.1f} stoch={stoch_k:.1f}", rsi, stoch_k


def should_exit_rsi(close: list[float], is_short: bool, rsi_exit: float = 50.0) -> bool:
    """Check if RSI has crossed back to exit level."""
    if len(close) < 15:
        return False
    rsi = calc_rsi(close, 14)
    if is_short:
        return rsi <= rsi_exit
    else:
        return rsi >= rsi_exit
