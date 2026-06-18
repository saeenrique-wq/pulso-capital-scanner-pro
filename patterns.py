"""Harmonic and price action pattern detection."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

TOL = 0.05  # 5% tolerance


def _in_range(value: float, low: float, high: float, tol: float = TOL) -> bool:
    span = high - low
    return (low - span * tol) <= value <= (high + span * tol)


def _near(value: float, target: float, tol: float = TOL) -> bool:
    return abs(value - target) <= target * tol


def find_swing_points(df: pd.DataFrame, lookback: int = 5) -> list[dict[str, Any]]:
    """Return list of swing points {index, price, type ('H'|'L')}."""
    if df is None or len(df) < lookback * 2 + 1:
        return []

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    swings: list[dict[str, Any]] = []

    for i in range(lookback, len(df) - lookback):
        window_high = highs[i - lookback : i + lookback + 1]
        window_low = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_high.max():
            swings.append({"index": i, "price": float(highs[i]), "type": "H"})
        elif lows[i] == window_low.min():
            swings.append({"index": i, "price": float(lows[i]), "type": "L"})

    # Remove consecutive same-type swings keeping the more extreme one
    cleaned: list[dict[str, Any]] = []
    for sw in swings:
        if cleaned and cleaned[-1]["type"] == sw["type"]:
            if sw["type"] == "H" and sw["price"] > cleaned[-1]["price"]:
                cleaned[-1] = sw
            elif sw["type"] == "L" and sw["price"] < cleaned[-1]["price"]:
                cleaned[-1] = sw
        else:
            cleaned.append(sw)
    return cleaned


def _last_five_swings(swings: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    if len(swings) < 5:
        return None
    return swings[-5:]


def _ratios(points: list[dict[str, Any]]) -> dict[str, float] | None:
    X, A, B, C, D = points
    xa = abs(A["price"] - X["price"])
    ab = abs(B["price"] - A["price"])
    bc = abs(C["price"] - B["price"])
    cd = abs(D["price"] - C["price"])
    ad = abs(D["price"] - A["price"])
    if xa == 0 or ab == 0 or bc == 0:
        return None
    return {
        "xab": ab / xa,
        "abc": bc / ab,
        "bcd": cd / bc,
        "xad": ad / xa,
    }


def _harmonic_template(
    swings: list[dict[str, Any]],
    xab_range: tuple[float, float],
    abc_range: tuple[float, float],
    bcd_range: tuple[float, float],
    xad_range: tuple[float, float],
    name: str,
) -> dict[str, Any]:
    base = {"found": False, "name": name}
    pts = _last_five_swings(swings)
    if pts is None:
        return base

    # Alternating pattern: XABCD must alternate H/L
    types = [p["type"] for p in pts]
    if not (types == ["L", "H", "L", "H", "L"] or types == ["H", "L", "H", "L", "H"]):
        return base

    r = _ratios(pts)
    if r is None:
        return base

    if not (
        _in_range(r["xab"], *xab_range)
        and _in_range(r["abc"], *abc_range)
        and _in_range(r["bcd"], *bcd_range)
        and _in_range(r["xad"], *xad_range)
    ):
        return base

    X, A, B, C, D = pts
    direction = "BULLISH" if types[-1] == "L" else "BEARISH"
    d_price = D["price"]
    span = abs(A["price"] - X["price"]) * 0.02  # PRZ band
    confidence = 60
    confidence += 10 if _near(r["xab"], (xab_range[0] + xab_range[1]) / 2) else 0
    confidence += 10 if _near(r["xad"], (xad_range[0] + xad_range[1]) / 2) else 0
    confidence = min(95, confidence)

    return {
        "found": True,
        "name": name,
        "direction": direction,
        "X": X,
        "A": A,
        "B": B,
        "C": C,
        "D": D,
        "prz_low": d_price - span,
        "prz_high": d_price + span,
        "confidence": confidence,
        "ratios": r,
    }


def detect_gartley(swings: list[dict[str, Any]]) -> dict[str, Any]:
    return _harmonic_template(
        swings,
        xab_range=(0.618 * 0.95, 0.618 * 1.05),
        abc_range=(0.382, 0.886),
        bcd_range=(1.13, 1.618),
        xad_range=(0.786 * 0.95, 0.786 * 1.05),
        name="Gartley",
    )


def detect_butterfly(swings: list[dict[str, Any]]) -> dict[str, Any]:
    return _harmonic_template(
        swings,
        xab_range=(0.786 * 0.95, 0.786 * 1.05),
        abc_range=(0.382, 0.886),
        bcd_range=(1.618, 2.618),
        xad_range=(1.27, 1.618),
        name="Butterfly",
    )


def detect_crab(swings: list[dict[str, Any]]) -> dict[str, Any]:
    return _harmonic_template(
        swings,
        xab_range=(0.382, 0.618),
        abc_range=(0.382, 0.886),
        bcd_range=(2.618, 3.618),
        xad_range=(1.618 * 0.95, 1.618 * 1.05),
        name="Crab",
    )


def detect_shark(swings: list[dict[str, Any]]) -> dict[str, Any]:
    base = {"found": False, "name": "Shark"}
    pts = _last_five_swings(swings)
    if pts is None:
        return base
    r = _ratios(pts)
    if r is None:
        return base
    types = [p["type"] for p in pts]
    if not (types == ["L", "H", "L", "H", "L"] or types == ["H", "L", "H", "L", "H"]):
        return base
    if not (
        _in_range(r["xab"], 0.446, 0.618)
        and _in_range(r["abc"], 1.13, 1.618)
        and _in_range(r["xad"], 0.886, 1.13)
    ):
        return base
    X, A, B, C, D = pts
    direction = "BULLISH" if types[-1] == "L" else "BEARISH"
    span = abs(A["price"] - X["price"]) * 0.02
    return {
        "found": True,
        "name": "Shark",
        "direction": direction,
        "X": X, "A": A, "B": B, "C": C, "D": D,
        "prz_low": D["price"] - span,
        "prz_high": D["price"] + span,
        "confidence": 70,
        "ratios": r,
    }


def detect_5_0(swings: list[dict[str, Any]]) -> dict[str, Any]:
    base = {"found": False, "name": "5-0"}
    pts = _last_five_swings(swings)
    if pts is None:
        return base
    r = _ratios(pts)
    if r is None:
        return base
    types = [p["type"] for p in pts]
    if not (types == ["L", "H", "L", "H", "L"] or types == ["H", "L", "H", "L", "H"]):
        return base
    if not (
        _in_range(r["abc"], 1.13, 1.618)
        and _in_range(r["bcd"], 0.5 * 0.95, 0.5 * 1.05)
    ):
        return base
    X, A, B, C, D = pts
    direction = "BULLISH" if types[-1] == "L" else "BEARISH"
    span = abs(A["price"] - X["price"]) * 0.02
    return {
        "found": True,
        "name": "5-0",
        "direction": direction,
        "X": X, "A": A, "B": B, "C": C, "D": D,
        "prz_low": D["price"] - span,
        "prz_high": D["price"] + span,
        "confidence": 65,
        "ratios": r,
    }


# ---------------------- Price Action ----------------------


def detect_pin_bar(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or len(df) < 3:
        return {"found": False, "direction": None, "strength": 0}
    last = df.iloc[-1]
    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    body = abs(c - o)
    if body == 0:
        body = (h - l) * 0.01 + 1e-9
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    rng = h - l
    if rng == 0:
        return {"found": False, "direction": None, "strength": 0}

    if lower_wick >= 2 * body and lower_wick > upper_wick * 2:
        strength = int(min(100, (lower_wick / body) * 20))
        return {"found": True, "direction": "BULLISH", "strength": strength}
    if upper_wick >= 2 * body and upper_wick > lower_wick * 2:
        strength = int(min(100, (upper_wick / body) * 20))
        return {"found": True, "direction": "BEARISH", "strength": strength}
    return {"found": False, "direction": None, "strength": 0}


def detect_engulfing(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or len(df) < 2:
        return {"found": False, "direction": None, "strength": 0}
    prev = df.iloc[-2]
    cur = df.iloc[-1]
    po, pc = float(prev["open"]), float(prev["close"])
    co, cc = float(cur["open"]), float(cur["close"])
    prev_body = abs(pc - po)
    cur_body = abs(cc - co)
    if cur_body <= prev_body or prev_body == 0:
        return {"found": False, "direction": None, "strength": 0}

    if pc < po and cc > co and co <= pc and cc >= po:
        strength = int(min(100, (cur_body / prev_body) * 30))
        return {"found": True, "direction": "BULLISH", "strength": strength}
    if pc > po and cc < co and co >= pc and cc <= po:
        strength = int(min(100, (cur_body / prev_body) * 30))
        return {"found": True, "direction": "BEARISH", "strength": strength}
    return {"found": False, "direction": None, "strength": 0}


def detect_inside_bar(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or len(df) < 2:
        return {"found": False}
    prev = df.iloc[-2]
    cur = df.iloc[-1]
    if float(cur["high"]) < float(prev["high"]) and float(cur["low"]) > float(prev["low"]):
        return {"found": True}
    return {"found": False}


def detect_breakout(
    df: pd.DataFrame, levels: list[dict[str, Any]], atr: float
) -> dict[str, Any]:
    if df is None or len(df) < 2 or not levels:
        return {"found": False, "direction": None, "level": None, "strength": 0}
    last_close = float(df["close"].iloc[-1])
    last_open = float(df["open"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    threshold = max(atr * 0.5, 1e-9)

    for lvl in levels:
        lp = lvl["price"]
        if prev_close < lp <= last_close and (last_close - lp) >= threshold * 0.25:
            strength = int(min(100, 40 + lvl.get("strength", 1) * 10))
            return {"found": True, "direction": "BULLISH", "level": lp, "strength": strength}
        if prev_close > lp >= last_close and (lp - last_close) >= threshold * 0.25:
            strength = int(min(100, 40 + lvl.get("strength", 1) * 10))
            return {"found": True, "direction": "BEARISH", "level": lp, "strength": strength}
        _ = last_open  # unused but kept for clarity
    return {"found": False, "direction": None, "level": None, "strength": 0}


def find_support_resistance(
    df: pd.DataFrame, lookback: int = 50
) -> list[dict[str, Any]]:
    if df is None or len(df) < 10:
        return []
    sample = df.tail(lookback)
    highs = sample["high"].to_numpy()
    lows = sample["low"].to_numpy()
    candidates = np.concatenate([highs, lows])

    if len(candidates) == 0:
        return []

    rng = float(candidates.max() - candidates.min())
    if rng == 0:
        return []

    bin_width = rng / 20.0
    levels: dict[int, list[float]] = {}
    for p in candidates:
        key = int(p // bin_width) if bin_width > 0 else 0
        levels.setdefault(key, []).append(float(p))

    out: list[dict[str, Any]] = []
    for prices in levels.values():
        if len(prices) >= 2:
            out.append(
                {
                    "price": float(np.mean(prices)),
                    "strength": len(prices),
                }
            )
    out.sort(key=lambda x: x["strength"], reverse=True)
    return out[:10]


def analyze_patterns(df: pd.DataFrame) -> dict[str, Any]:
    swings = find_swing_points(df)
    levels = find_support_resistance(df)

    from indicators import calc_atr

    atr_val = 0.0
    if df is not None and len(df) >= 14:
        atr_series = calc_atr(df["high"], df["low"], df["close"])
        atr_val = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0

    harmonic = {
        "gartley": detect_gartley(swings),
        "butterfly": detect_butterfly(swings),
        "crab": detect_crab(swings),
        "shark": detect_shark(swings),
        "five_zero": detect_5_0(swings),
    }
    price_action = {
        "pin_bar": detect_pin_bar(df),
        "engulfing": detect_engulfing(df),
        "inside_bar": detect_inside_bar(df),
        "breakout": detect_breakout(df, levels, atr_val),
    }

    best_harmonic = None
    for h in harmonic.values():
        if h.get("found"):
            if best_harmonic is None or h.get("confidence", 0) > best_harmonic.get("confidence", 0):
                best_harmonic = h

    return {
        "swings": swings,
        "levels": levels,
        "harmonic": harmonic,
        "best_harmonic": best_harmonic,
        "price_action": price_action,
        "atr": atr_val,
    }
