"""Technical indicators implemented with pandas/numpy."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def calc_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, pd.Series]:
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    macd = ema_fast - ema_slow
    signal_line = calc_ema(macd, signal)
    histogram = macd - signal_line
    return {"macd": macd, "signal": signal_line, "histogram": histogram}


def calc_bollinger(
    series: pd.Series, period: int = 20, std: float = 2.0
) -> dict[str, pd.Series]:
    middle = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std()
    upper = middle + std * rolling_std
    lower = middle - std * rolling_std
    bandwidth = (upper - lower) / middle.replace(0, np.nan)
    percent_b = (series - lower) / (upper - lower).replace(0, np.nan)
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "bandwidth": bandwidth.fillna(0.0),
        "percent_b": percent_b.fillna(0.5),
    }


def calc_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> dict[str, pd.Series]:
    """Average Directional Index (ADX) with +DI and -DI."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move.values, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move.values, 0.0),
        index=high.index,
    )

    alpha = 1.0 / period
    tr_smooth = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = (
        100 * plus_dm.ewm(alpha=alpha, adjust=False).mean()
        / tr_smooth.replace(0, np.nan)
    ).fillna(20.0)
    minus_di = (
        100 * minus_dm.ewm(alpha=alpha, adjust=False).mean()
        / tr_smooth.replace(0, np.nan)
    ).fillna(20.0)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(20.0)
    adx = dx.ewm(alpha=alpha, adjust=False).mean().fillna(20.0)

    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def calc_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def calc_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict[str, Any]:
    if df.empty or "close" not in df.columns:
        return {"poc": 0.0, "value_area_high": 0.0, "value_area_low": 0.0, "levels": []}

    prices = df["close"].to_numpy()
    volumes = (
        df["volume"].to_numpy()
        if "volume" in df.columns and df["volume"].sum() > 0
        else np.ones(len(df))
    )

    hist, edges = np.histogram(prices, bins=bins, weights=volumes)
    centers = (edges[:-1] + edges[1:]) / 2

    poc_idx = int(np.argmax(hist))
    poc = float(centers[poc_idx])

    total_vol = float(hist.sum())
    target_vol = total_vol * 0.70
    order = np.argsort(hist)[::-1]
    accumulated = 0.0
    selected_idx: list[int] = []
    for i in order:
        accumulated += float(hist[i])
        selected_idx.append(int(i))
        if accumulated >= target_vol:
            break

    selected_prices = centers[selected_idx]
    value_area_high = float(selected_prices.max()) if len(selected_prices) else poc
    value_area_low = float(selected_prices.min()) if len(selected_prices) else poc

    levels = [
        {"price": float(centers[i]), "volume": float(hist[i])}
        for i in range(len(centers))
    ]

    return {
        "poc": poc,
        "value_area_high": value_area_high,
        "value_area_low": value_area_low,
        "levels": levels,
    }


def detect_divergence(
    price: pd.Series, rsi: pd.Series, lookback: int = 10
) -> dict[str, bool]:
    if len(price) < lookback * 2 or len(rsi) < lookback * 2:
        return {"bullish": False, "bearish": False}

    recent_price = price.iloc[-lookback:]
    prev_price = price.iloc[-lookback * 2 : -lookback]
    recent_rsi = rsi.iloc[-lookback:]
    prev_rsi = rsi.iloc[-lookback * 2 : -lookback]

    p_low_recent = float(recent_price.min())
    p_low_prev = float(prev_price.min())
    r_low_recent = float(recent_rsi.min())
    r_low_prev = float(prev_rsi.min())

    p_high_recent = float(recent_price.max())
    p_high_prev = float(prev_price.max())
    r_high_recent = float(recent_rsi.max())
    r_high_prev = float(prev_rsi.max())

    bullish = p_low_recent < p_low_prev and r_low_recent > r_low_prev
    bearish = p_high_recent > p_high_prev and r_high_recent < r_high_prev
    return {"bullish": bool(bullish), "bearish": bool(bearish)}


def get_all_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """Compute every indicator and return a structured dict with last values."""
    if df is None or df.empty or len(df) < 30:
        return {"valid": False}

    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi = calc_rsi(close)
    macd = calc_macd(close)
    bollinger = calc_bollinger(close)
    atr = calc_atr(high, low, close)
    adx_data = calc_adx(high, low, close)
    ema20 = calc_ema(close, 20)
    ema50 = calc_ema(close, 50)
    ema200 = calc_ema(close, 200) if len(close) >= 200 else calc_ema(close, len(close) - 1)
    vp = calc_volume_profile(df)
    divergence = detect_divergence(close, rsi)

    def last(s: pd.Series) -> float:
        if s is None or len(s) == 0:
            return 0.0
        val = s.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    return {
        "valid": True,
        "close": last(close),
        "rsi": last(rsi),
        "rsi_series": rsi,
        "macd": last(macd["macd"]),
        "macd_signal": last(macd["signal"]),
        "macd_histogram": last(macd["histogram"]),
        "macd_prev_hist": float(macd["histogram"].iloc[-2]) if len(macd["histogram"]) > 1 else 0.0,
        "bb_upper": last(bollinger["upper"]),
        "bb_middle": last(bollinger["middle"]),
        "bb_lower": last(bollinger["lower"]),
        "bb_bandwidth": last(bollinger["bandwidth"]),
        "bb_percent_b": last(bollinger["percent_b"]),
        "atr": last(atr),
        "atr_series": atr,
        "ema20": last(ema20),
        "ema50": last(ema50),
        "ema200": last(ema200),
        "vp_poc": vp["poc"],
        "vp_vah": vp["value_area_high"],
        "vp_val": vp["value_area_low"],
        "adx": last(adx_data["adx"]),
        "plus_di": last(adx_data["plus_di"]),
        "minus_di": last(adx_data["minus_di"]),
        "divergence_bullish": divergence["bullish"],
        "divergence_bearish": divergence["bearish"],
    }
