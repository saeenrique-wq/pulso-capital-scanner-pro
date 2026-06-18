"""Broker connectivity: MT5 → MT4 file bridge → yfinance fallback."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── MT5 ──────────────────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
    MT5_AVAILABLE = True
except Exception:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

# ── yfinance ─────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
    YF_AVAILABLE = True
except Exception:
    yf = None  # type: ignore[assignment]
    YF_AVAILABLE = False

# ── data_feeds (agregador de APIs públicas: stooq, twelvedata, alphavantage…) ─
try:
    from data_feeds import DataFeedAggregator
    _feed_agg: DataFeedAggregator | None = DataFeedAggregator()
    FEEDS_AVAILABLE = True
except Exception:
    _feed_agg = None  # type: ignore[assignment]
    FEEDS_AVAILABLE = False

_MT5_CONNECTED = False

# ── MT4 bridge paths ──────────────────────────────────────────────────────────
MT4_COMMON = Path(r"C:\Users\saems\AppData\Roaming\MetaQuotes\Terminal\Common\Files\scanner_bridge")

# MT4 puede tener el símbolo como "XAUUSD.m" o "XAUUSD" según el broker
_MT4_SYMBOL_VARIANTS: dict[str, list[str]] = {
    "XAUUSD": ["XAUUSD", "XAUUSD.m", "XAUUSD."],
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD."],
    "BTCUSD": ["BTCUSD", "BTCUSD.m"],
}

_YF_SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "GOLD":   "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "XRPUSD": "XRP-USD",
}

# Swissquote public quotes API — sin API key, precio spot en tiempo real
_SQ_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    "XAUUSD": ("XAU", "USD"),
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
    "NZDUSD": ("NZD", "USD"),
}

_CG_ID_MAP = {
    "BTCUSD": "bitcoin",
    "ETHUSD": "ethereum",
    "XRPUSD": "ripple",
}

_YF_TF_MAP = {
    "M1":  ("1m",  "5d"),
    "M5":  ("5m",  "30d"),
    "M15": ("15m", "60d"),
    "H1":  ("60m", "730d"),
    "H4":  ("1h",  "730d"),
    "D1":  ("1d",  "5y"),
}


# ─────────────────────────────────────────────────────────────────────────────
# MT5
# ─────────────────────────────────────────────────────────────────────────────

def _mt5_tf_map() -> dict[str, Any]:
    if not MT5_AVAILABLE:
        return {}
    return {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }


def connect_mt5(config: dict[str, Any]) -> bool:
    global _MT5_CONNECTED
    if not MT5_AVAILABLE:
        logger.info("MetaTrader5 no instalado; usando MT4 bridge / yfinance")
        return False
    if not config.get("mt5", {}).get("enabled", True):
        return False
    try:
        mt5_cfg = config.get("mt5", {})
        if mt5_cfg.get("login") and mt5_cfg.get("password") and mt5_cfg.get("server"):
            ok = mt5.initialize(
                login=int(mt5_cfg["login"]),
                password=str(mt5_cfg["password"]),
                server=str(mt5_cfg["server"]),
            )
        else:
            ok = mt5.initialize()
        if not ok:
            logger.warning("MT5 init falló: %s", mt5.last_error())
            return False
        _MT5_CONNECTED = True
        logger.info("MT5 conectado")
        return True
    except Exception as exc:
        logger.warning("MT5 excepción: %s", exc)
        return False


def disconnect_mt5() -> None:
    global _MT5_CONNECTED
    if MT5_AVAILABLE and _MT5_CONNECTED:
        try:
            mt5.shutdown()
        except Exception:
            pass
    _MT5_CONNECTED = False


def _from_mt5(symbol: str, timeframe: str, bars: int) -> pd.DataFrame | None:
    if not (MT5_AVAILABLE and _MT5_CONNECTED):
        return None
    try:
        tf = _mt5_tf_map().get(timeframe)
        if tf is None:
            return None
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["time", "open", "high", "low", "close", "volume"]]
    except Exception as exc:
        logger.warning("MT5 copy_rates error %s/%s: %s", symbol, timeframe, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MT4 FILE BRIDGE
# ─────────────────────────────────────────────────────────────────────────────

def mt4_bridge_active() -> bool:
    """True si el EA SCANNER_BRIDGE está corriendo y escribió datos recientes (< 30s)."""
    status_file = MT4_COMMON / "bridge_status.json"
    if not status_file.exists():
        return False
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        if data.get("status") != "ONLINE":
            return False
        ts = int(data.get("time", 0))
        return (time.time() - ts) < 30
    except Exception:
        return False


def _mt4_find_file(symbol: str, timeframe: str) -> Path | None:
    """Busca el CSV correcto probando las variantes de símbolo MT4."""
    variants = _MT4_SYMBOL_VARIANTS.get(symbol.upper(), [symbol])
    for variant in variants:
        p = MT4_COMMON / f"ohlcv_{variant}_{timeframe}.csv"
        if p.exists():
            return p
    return None


def _from_mt4(symbol: str, timeframe: str, bars: int) -> pd.DataFrame | None:
    if not mt4_bridge_active():
        return None
    csv_path = _mt4_find_file(symbol, timeframe)
    if csv_path is None:
        return None
    try:
        df = pd.read_csv(csv_path, dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]
        df["time"] = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.sort_values("time").tail(bars).reset_index(drop=True)
        logger.debug("MT4 bridge: %s/%s — %d velas", symbol, timeframe, len(df))
        return df[["time", "open", "high", "low", "close", "volume"]]
    except Exception as exc:
        logger.warning("MT4 bridge read error %s/%s: %s", symbol, timeframe, exc)
        return None


def _mt4_tick(symbol: str) -> dict[str, float] | None:
    variants = _MT4_SYMBOL_VARIANTS.get(symbol.upper(), [symbol])
    for variant in variants:
        tick_file = MT4_COMMON / f"tick_{variant}.json"
        if not tick_file.exists():
            continue
        try:
            data = json.loads(tick_file.read_text(encoding="utf-8"))
            if (time.time() - int(data.get("time", 0))) > 30:
                continue  # dato viejo
            bid = float(data["bid"])
            ask = float(data["ask"])
            return {"bid": bid, "ask": ask, "spread": ask - bid, "source": "MT4"}
        except Exception:
            continue
    return None


def mt4_bridge_info() -> dict[str, Any]:
    """Estado detallado del bridge MT4 para el dashboard."""
    status_file = MT4_COMMON / "bridge_status.json"
    if not status_file.exists():
        return {"active": False, "reason": "EA no instalado o no iniciado"}
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        ts = int(data.get("time", 0))
        age = int(time.time() - ts)
        return {
            "active": data.get("status") == "ONLINE" and age < 30,
            "symbol": data.get("symbol", "?"),
            "account": data.get("account", 0),
            "broker": data.get("broker", "?"),
            "age_seconds": age,
        }
    except Exception as exc:
        return {"active": False, "reason": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# yfinance
# ─────────────────────────────────────────────────────────────────────────────

def _resample_h4(df: pd.DataFrame) -> pd.DataFrame:
    """Resamplea velas de 1h a 4h para uso como H4."""
    if df is None or df.empty:
        return df
    tmp = df.copy()
    tmp["time"] = pd.to_datetime(tmp["time"], utc=True)
    tmp = tmp.set_index("time")
    r = tmp.resample("4h", label="left", closed="left").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["open", "close"])
    return r.reset_index()


def _from_yfinance(symbol: str, timeframe: str, bars: int) -> pd.DataFrame | None:
    if not YF_AVAILABLE:
        return None
    yf_symbol = _YF_SYMBOL_MAP.get(symbol.upper(), symbol)
    interval, period = _YF_TF_MAP.get(timeframe, ("5m", "30d"))
    try:
        data = yf.download(
            yf_symbol, period=period, interval=interval,
            progress=False, auto_adjust=False, threads=False,
        )
        if data is None or data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [c[0] for c in data.columns]
        data = data.reset_index()
        time_col = "Datetime" if "Datetime" in data.columns else "Date"
        df = pd.DataFrame({
            "time":   pd.to_datetime(data[time_col], utc=True),
            "open":   data["Open"].astype(float),
            "high":   data["High"].astype(float),
            "low":    data["Low"].astype(float),
            "close":  data["Close"].astype(float),
            "volume": data.get("Volume", pd.Series([0] * len(data))).fillna(0).astype(float),
        })
        df = df.dropna(subset=["open", "high", "low", "close"])
        if timeframe == "H4":
            df = _resample_h4(df)
        return df.tail(bars).reset_index(drop=True)
    except Exception as exc:
        logger.warning("yfinance error %s: %s", yf_symbol, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# API pública — prioridad: MT5 → MT4 bridge → yfinance → data_feeds (respaldo)
# ─────────────────────────────────────────────────────────────────────────────

def get_ohlcv(symbol: str, timeframe: str, bars: int = 200) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    # 1. MT5
    df = _from_mt5(symbol, timeframe, bars)
    if df is not None and not df.empty:
        return df.reset_index(drop=True)

    # 2. MT4 bridge
    df = _from_mt4(symbol, timeframe, bars)
    if df is not None and not df.empty:
        return df.reset_index(drop=True)

    # 3. yfinance
    df = _from_yfinance(symbol, timeframe, bars)
    if df is not None and not df.empty:
        return df.reset_index(drop=True)

    # 4. data_feeds aggregator — respaldo solo si yfinance falla (Yahoo HTTP
    #    directo / TwelveData / Alpha Vantage). No reemplaza nada de lo anterior,
    #    solo evita que el scanner se quede sin velas si yfinance se cae en el VPS.
    if FEEDS_AVAILABLE and _feed_agg is not None:
        try:
            df = _feed_agg.get_ohlcv_multi(symbol, timeframe, bars)
            if df is not None and not df.empty:
                logger.warning("get_ohlcv: usando fallback data_feeds para %s/%s "
                               "(yfinance no respondió)", symbol, timeframe)
                return df.reset_index(drop=True)
        except Exception as exc:
            logger.debug("data_feeds get_ohlcv_multi error %s/%s: %s",
                        symbol, timeframe, exc)

    return empty


def _swissquote_tick(symbol: str) -> dict[str, float] | None:
    """Precio spot en tiempo real desde Swissquote Bank (sin API key)."""
    pair = _SQ_SYMBOL_MAP.get(symbol.upper())
    if pair is None:
        return None
    try:
        import requests as _req
        url = (f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes"
               f"/instrument/{pair[0]}/{pair[1]}")
        resp = _req.get(url, headers={"User-Agent": "ScannerPRO/1.0"}, timeout=4)
        data = resp.json()
        if not data:
            return None
        profiles = data[0].get("spreadProfilePrices", [])
        standard = next((p for p in profiles if p.get("spreadProfile") == "standard"), profiles[0] if profiles else None)
        if standard is None:
            return None
        bid = float(standard["bid"])
        ask = float(standard["ask"])
        if bid <= 0 or ask <= 0:
            return None
        return {"bid": bid, "ask": ask, "spread": ask - bid, "source": "swissquote"}
    except Exception as exc:
        logger.debug("swissquote tick error %s: %s", symbol, exc)
        return None


def get_current_price(symbol: str) -> dict[str, float]:
    # 1. MT5 — tick en tiempo real
    if MT5_AVAILABLE and _MT5_CONNECTED:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                return {"bid": float(tick.bid), "ask": float(tick.ask),
                        "spread": float(tick.ask - tick.bid), "source": "MT5"}
        except Exception:
            pass

    # 2. MT4 bridge tick
    tick4 = _mt4_tick(symbol)
    if tick4 is not None:
        return tick4

    # 3. Swissquote — precio SPOT en tiempo real, sin API key
    sq = _swissquote_tick(symbol)
    if sq is not None:
        return sq

    # 3b. CoinGecko — solo cripto. API oficial sin key, más estable que el
    #     scraping de yfinance para BTC/ETH/XRP (probado en vivo y funcionando).
    cg_id = _CG_ID_MAP.get(symbol.upper())
    if cg_id and FEEDS_AVAILABLE:
        try:
            from data_feeds import fetch_coingecko
            prices = fetch_coingecko([cg_id])
            p = prices.get(cg_id)
            if p and float(p) > 0:
                p = float(p)
                spread = p * 0.0001
                return {"bid": p - spread / 2, "ask": p + spread / 2,
                        "spread": spread, "source": "coingecko"}
        except Exception as exc:
            logger.debug("coingecko price error %s: %s", symbol, exc)

    # 4. yfinance fast_info — cotización near-real-time (endpoint de quotes)
    if YF_AVAILABLE:
        try:
            yf_symbol = _YF_SYMBOL_MAP.get(symbol.upper(), symbol)
            ticker = yf.Ticker(yf_symbol)
            price = ticker.fast_info.last_price
            if price is not None and float(price) > 0:
                p = float(price)
                spread = p * 0.0001
                return {"bid": p - spread / 2, "ask": p + spread / 2,
                        "spread": spread, "source": "yf_fast_info"}
        except Exception as exc:
            logger.debug("yf fast_info error %s: %s", symbol, exc)

    # 5. data_feeds aggregator (Gold-API, Yahoo direct HTTP, ExchangeRate-API…)
    if FEEDS_AVAILABLE and _feed_agg is not None:
        try:
            data = _feed_agg.get_price(symbol)
            if data and data.get("price"):
                price = float(data["price"])
                spread = price * 0.0001
                return {
                    "bid": price - spread / 2,
                    "ask": price + spread / 2,
                    "spread": spread,
                    "source": str(data.get("source", "data_feeds")),
                }
        except Exception as exc:
            logger.debug("data_feeds price error %s: %s", symbol, exc)

    # 6. yfinance barras M1 (último recurso — puede tener delay)
    df = _from_yfinance(symbol, "M1", 2)
    if df is not None and not df.empty:
        last = float(df["close"].iloc[-1])
        spread = last * 0.0001
        logger.warning("Precio yfinance BARRAS (posible delay) %s: %.2f", symbol, last)
        return {"bid": last - spread / 2, "ask": last + spread / 2,
                "spread": spread, "source": "yfinance_bars"}

    return {"bid": 0.0, "ask": 0.0, "spread": 0.0, "source": "none"}


def get_spread(symbol: str) -> float:
    price = get_current_price(symbol)
    spread = price.get("spread", 0.0)
    sym_up = symbol.upper()
    if "JPY" in sym_up:
        return spread * 100.0
    if sym_up in {"XAUUSD", "GOLD"}:
        return spread * 10.0
    if sym_up in {"BTCUSD", "ETHUSD", "XRPUSD"}:
        # Cripto: sin concepto real de "pip" — se reporta el spread crudo en USD.
        return spread
    return spread * 10000.0


def is_mt5_connected() -> bool:
    return _MT5_CONNECTED


def yfinance_available() -> bool:
    return YF_AVAILABLE


def feeds_available() -> bool:
    return FEEDS_AVAILABLE


def get_feed_aggregator() -> Any | None:
    """Devuelve la instancia del DataFeedAggregator (o None si no está disponible)."""
    return _feed_agg


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("MT4 bridge activo:", mt4_bridge_active())
    print("MT4 info:", mt4_bridge_info())
    print("data_feeds disponible:", FEEDS_AVAILABLE)
    df = get_ohlcv("XAUUSD", "M5", 10)
    print(df)
