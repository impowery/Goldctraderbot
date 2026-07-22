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
                 rsi_oversold: float = 30, rsi_overbought: float = 70,
                 stoch_oversold: float = 20, stoch_overbought: float = 80,
                 m30_ema_history: list = None
                 ) -> tuple:
    """Trend-following with RSI as momentum confirmation (Variant 3, 2026-07-22).

    Entry LONG:  price > EMA41 AND 50 < RSI < 70 AND Stoch > 50
    Entry SHORT: price < EMA41 AND 30 < RSI < 50 AND Stoch < 50

    Protection from extremes:
    - RSI >= 70 -> LONG blocked (overheated, reversal risk)
    - RSI <= 30 -> SHORT blocked (oversold, bounce risk)

    Exit (should_exit_rsi): RSI crosses 50 in reverse direction.

    Args:
        rsi_oversold: lower RSI bound (default 30) - below this = oversold, no SHORT
        rsi_overbought: upper RSI bound (default 70) - above this = overbought, no LONG
        stoch_*: legacy params, not used in entry logic (Stoch > 50 for LONG, < 50 for SHORT)
    """
    if len(close) < 20:
        return False, "not enough data", 0.0, 0.0

    rsi = calc_rsi(close, 14)
    stoch_k, stoch_d = calc_stochastic(high, low, close, 14, 3)
    price = close[-1]

    # Trend filter (Variant 2): price vs EMA41
    if m30_ema > 0:
        if price > m30_ema:
            trend_up = True
            trend_str = f"UP (price ${price:.2f} > EMA ${m30_ema:.2f})"
        else:
            trend_up = False
            trend_str = f"DOWN (price ${price:.2f} < EMA ${m30_ema:.2f})"
    else:
        trend_up = None  # warmup
        trend_str = "inactive (EMA warmup)"

    # LONG: trend UP + RSI momentum 50-70 + Stoch > 50 (buyers in control)
    # RSI >= 70 = overheated, skip (protection from peak entry)
    if trend_up is True or trend_up is None:
        if rsi >= rsi_overbought:
            return False, f"LONG skip (RSI {rsi:.1f} >= {rsi_overbought} overheated) trend={trend_str} stoch={stoch_k:.1f}", rsi, stoch_k
        if 50 < rsi < rsi_overbought and stoch_k > 50:
            return True, f"LONG rsi={rsi:.1f} stoch={stoch_k:.1f} trend={trend_str}", rsi, stoch_k
        if 50 < rsi < rsi_overbought and stoch_k <= 50:
            return False, f"LONG blocked (Stoch {stoch_k:.1f} <= 50, no buyer momentum) rsi={rsi:.1f} trend={trend_str}", rsi, stoch_k
        if rsi <= 50:
            return False, f"No signal (RSI {rsi:.1f} <= 50, no momentum) stoch={stoch_k:.1f} trend={trend_str}", rsi, stoch_k

    # SHORT: trend DOWN + RSI momentum 30-50 + Stoch < 50 (sellers in control)
    # RSI <= 30 = oversold, skip (protection from bottom entry)
    if trend_up is False or trend_up is None:
        if rsi <= rsi_oversold:
            return False, f"SHORT skip (RSI {rsi:.1f} <= {rsi_oversold} oversold) trend={trend_str} stoch={stoch_k:.1f}", rsi, stoch_k
        if rsi_oversold < rsi < 50 and stoch_k < 50:
            return True, f"SHORT rsi={rsi:.1f} stoch={stoch_k:.1f} trend={trend_str}", rsi, stoch_k
        if rsi_oversold < rsi < 50 and stoch_k >= 50:
            return False, f"SHORT blocked (Stoch {stoch_k:.1f} >= 50, no seller momentum) rsi={rsi:.1f} trend={trend_str}", rsi, stoch_k
        if rsi >= 50:
            return False, f"No signal (RSI {rsi:.1f} >= 50, no momentum) stoch={stoch_k:.1f} trend={trend_str}", rsi, stoch_k

    return False, f"No signal rsi={rsi:.1f} stoch={stoch_k:.1f} trend={trend_str}", rsi, stoch_k


def should_exit_rsi(close: list[float], is_short: bool, rsi_exit: float = 50.0) -> bool:
    """Check if RSI has crossed back through 50 (momentum lost).

    LONG exit:  RSI drops below 50 (buyers lost control)
    SHORT exit: RSI rises above 50 (sellers lost control)
    """
    if len(close) < 15:
        return False
    rsi = calc_rsi(close, 14)
    if is_short:
        return rsi >= rsi_exit
    else:
        return rsi <= rsi_exit
