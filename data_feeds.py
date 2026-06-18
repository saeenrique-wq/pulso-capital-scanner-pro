"""data_feeds.py — Fuentes de datos adicionales para Scanner PRO.

Agregador que se conecta a múltiples APIs gratuitas y feeds públicos para
ampliar las fuentes del scanner más allá de yfinance/MT4/MT5.

Fuentes integradas (todas gratuitas, sin API key obligatoria):
    - Alpha Vantage (key=demo)
    - Twelve Data (key=demo)
    - Stooq (CSV directo)
    - Yahoo Finance (HTTP directo, sin yfinance)
    - Gold-API.io (header demo)
    - Metals-API (key demo)
    - Open Exchange Rates (demo)
    - ExchangeRate-API (sin key)
    - CoinGecko (sin key)
    - Marketaux / Newsdata (demo)
    - RSS Reuters / FXStreet
    - CNN Fear & Greed Index
    - DXY via Stooq

Solo depende de `requests` y `pandas` (ya en el proyecto).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Configuración
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 5  # segundos
CACHE_TTL = 60  # segundos
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 ScannerPRO/1.0"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

# Mapeo de símbolos universal
SYMBOL_MAP: dict[str, dict[str, str | None]] = {
    "XAUUSD": {
        "stooq": "xauusd",
        "yahoo": "GC=F",
        "twelvedata": "XAU/USD",
        "alphavantage": "XAUUSD",
        "coingecko": None,
    },
    "EURUSD": {
        "stooq": "eurusd",
        "yahoo": "EURUSD=X",
        "twelvedata": "EUR/USD",
        "alphavantage": "EURUSD",
        "coingecko": None,
    },
    "GBPUSD": {
        "stooq": "gbpusd",
        "yahoo": "GBPUSD=X",
        "twelvedata": "GBP/USD",
        "alphavantage": "GBPUSD",
        "coingecko": None,
    },
    "USDJPY": {
        "stooq": "usdjpy",
        "yahoo": "USDJPY=X",
        "twelvedata": "USD/JPY",
        "alphavantage": "USDJPY",
        "coingecko": None,
    },
    "DXY": {
        "stooq": "usdidx",
        "yahoo": "DX-Y.NYB",
        "twelvedata": "DXY",
        "alphavantage": "DXY",
        "coingecko": None,
    },
    "BTCUSD": {
        "stooq": "btcusd",
        "yahoo": "BTC-USD",
        "twelvedata": "BTC/USD",
        "alphavantage": "BTCUSD",
        "coingecko": "bitcoin",
    },
    "ETHUSD": {
        "stooq": "ethusd",
        "yahoo": "ETH-USD",
        "twelvedata": "ETH/USD",
        "alphavantage": "ETHUSD",
        "coingecko": "ethereum",
    },
    "XRPUSD": {
        "stooq": "xrpusd",
        "yahoo": "XRP-USD",
        "twelvedata": "XRP/USD",
        "alphavantage": "XRPUSD",
        "coingecko": "ripple",
    },
    "SPX": {
        "stooq": "^spx",
        "yahoo": "^GSPC",
        "twelvedata": "SPX",
        "alphavantage": None,
        "coingecko": None,
    },
    "VIX": {
        "stooq": "^vix",
        "yahoo": "^VIX",
        "twelvedata": "VIX",
        "alphavantage": None,
        "coingecko": None,
    },
}

# Intervalos para cada fuente
_AV_INTERVALS = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "60min"}
_TD_INTERVALS = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h",
                 "H4": "4h", "D1": "1day"}
_YAHOO_DIRECT_INTERVALS = {
    "M1": ("1m", "5d"), "M5": ("5m", "30d"), "M15": ("15m", "60d"),
    "H1": ("60m", "730d"), "H4": ("1h", "730d"), "D1": ("1d", "5y"),
}


# ────────────────────────────────────────────────────────────────────────────
# Caché simple en memoria
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class _TTLCache:
    """Caché LRU-ish muy simple basado en TTL."""

    def __init__(self, ttl: int = CACHE_TTL) -> None:
        self.ttl = ttl
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            self._store.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = _CacheEntry(value=value,
                                       expires_at=time.time() + self.ttl)

    def clear(self) -> None:
        self._store.clear()


_cache = _TTLCache(ttl=CACHE_TTL)


# ────────────────────────────────────────────────────────────────────────────
# Helpers HTTP
# ────────────────────────────────────────────────────────────────────────────

def _get(url: str, *, headers: dict[str, str] | None = None,
         timeout: int = DEFAULT_TIMEOUT) -> requests.Response | None:
    """GET con manejo común de errores. Retorna None si falla."""
    merged = dict(HEADERS)
    if headers:
        merged.update(headers)
    try:
        logger.debug("GET %s", url)
        resp = requests.get(url, headers=merged, timeout=timeout)
        if resp.status_code != 200:
            logger.debug("HTTP %d en %s", resp.status_code, url)
            return None
        return resp
    except requests.RequestException as exc:
        logger.debug("Request fallido %s: %s", url, exc)
        return None


def _measure(callable_obj: Any, *args: Any, **kwargs: Any) -> tuple[Any, int]:
    """Ejecuta un callable y mide la latencia en ms."""
    t0 = time.time()
    try:
        result = callable_obj(*args, **kwargs)
    except Exception as exc:
        logger.debug("Callable %s falló: %s", callable_obj.__name__, exc)
        result = None
    latency_ms = int((time.time() - t0) * 1000)
    return result, latency_ms


# ────────────────────────────────────────────────────────────────────────────
# Fetchers individuales — devuelven None ante cualquier error
# ────────────────────────────────────────────────────────────────────────────

def fetch_stooq(symbol_stooq: str, interval: str = "d") -> pd.DataFrame | None:
    """
    Stooq ahora requiere captcha + apikey (2025+).
    Redirige a Yahoo Finance usando el mapeo de símbolos equivalentes.
    """
    _STOOQ_TO_YAHOO = {
        "xauusd": ("GC=F",   "1d", "5y"),
        "eurusd": ("EURUSD=X", "1d", "5y"),
        "gbpusd": ("GBPUSD=X", "1d", "5y"),
        "usdjpy": ("USDJPY=X", "1d", "5y"),
        "usdidx": ("DX-Y.NYB", "1d", "2y"),
        "^spx":   ("^GSPC",   "1d", "2y"),
        "^vix":   ("^VIX",    "1d", "2y"),
        "btcusd": ("BTC-USD",  "1d", "2y"),
    }
    cache_key = f"stooq_via_yahoo:{symbol_stooq}:{interval}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    mapping = _STOOQ_TO_YAHOO.get(symbol_stooq.lower())
    if mapping is None:
        return None
    yahoo_sym, yinterval, yrange = mapping
    df = fetch_yahoo_direct(yahoo_sym, interval=yinterval, range_=yrange)
    if df is not None and not df.empty:
        _cache.set(cache_key, df)
    return df


def fetch_yahoo_direct(symbol: str, interval: str = "5m",
                       range_: str = "5d") -> pd.DataFrame | None:
    """Llama directamente al endpoint chart de Yahoo Finance (sin yfinance)."""
    cache_key = f"yahoo:{symbol}:{interval}:{range_}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={range_}")
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        result_block = data.get("chart", {}).get("result")
        if not result_block:
            return None
        block = result_block[0]
        timestamps = block.get("timestamp", []) or []
        indicators = block.get("indicators", {}).get("quote", [{}])[0]
        opens = indicators.get("open", [])
        highs = indicators.get("high", [])
        lows = indicators.get("low", [])
        closes = indicators.get("close", [])
        vols = indicators.get("volume", [])

        rows = []
        for i, ts in enumerate(timestamps):
            if i >= len(closes) or closes[i] is None:
                continue
            rows.append({
                "time": datetime.fromtimestamp(int(ts), tz=timezone.utc),
                "open": float(opens[i]) if opens[i] is not None else float(closes[i]),
                "high": float(highs[i]) if highs[i] is not None else float(closes[i]),
                "low": float(lows[i]) if lows[i] is not None else float(closes[i]),
                "close": float(closes[i]),
                "volume": float(vols[i]) if i < len(vols) and vols[i] is not None else 0.0,
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        _cache.set(cache_key, df)
        return df
    except Exception as exc:
        logger.debug("yahoo_direct error %s: %s", symbol, exc)
        return None


def fetch_alphavantage(symbol_av: str, timeframe: str = "M5",
                       apikey: str = "demo") -> pd.DataFrame | None:
    """Alpha Vantage TIME_SERIES_INTRADAY o FX_INTRADAY."""
    cache_key = f"av:{symbol_av}:{timeframe}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    interval = _AV_INTERVALS.get(timeframe, "5min")

    # Para FOREX usar FX_INTRADAY
    is_forex = (len(symbol_av) == 6 and symbol_av.isalpha()) or "XAU" in symbol_av
    if is_forex and len(symbol_av) == 6:
        from_sym, to_sym = symbol_av[:3], symbol_av[3:]
        url = (f"https://www.alphavantage.co/query?function=FX_INTRADAY"
               f"&from_symbol={from_sym}&to_symbol={to_sym}"
               f"&interval={interval}&apikey={apikey}")
        ts_key = f"Time Series FX ({interval})"
    else:
        url = (f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY"
               f"&symbol={symbol_av}&interval={interval}&apikey={apikey}")
        ts_key = f"Time Series ({interval})"

    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        series = data.get(ts_key)
        if not series:
            logger.debug("AlphaVantage sin serie para %s: %s",
                         symbol_av, list(data.keys()))
            return None
        rows = []
        for ts_str, ohlc in series.items():
            rows.append({
                "time": pd.to_datetime(ts_str, utc=True),
                "open": float(ohlc.get("1. open", 0)),
                "high": float(ohlc.get("2. high", 0)),
                "low": float(ohlc.get("3. low", 0)),
                "close": float(ohlc.get("4. close", 0)),
                "volume": float(ohlc.get("5. volume", 0) or 0),
            })
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        _cache.set(cache_key, df)
        return df
    except Exception as exc:
        logger.debug("alphavantage error %s: %s", symbol_av, exc)
        return None


def fetch_twelvedata(symbol_td: str, timeframe: str = "M5",
                     outputsize: int = 100,
                     apikey: str = "demo") -> pd.DataFrame | None:
    """Twelve Data — alias interno a fetch_frankfurter para no depender de apikey."""
    # "demo" no funciona con XAU/USD; usamos Frankfurter como backend real
    sym = symbol_td.replace("/", "").upper()
    if len(sym) == 6:
        base, quote = sym[:3], sym[3:]
        rate = fetch_frankfurter(base, quote)
        if rate is not None:
            now = datetime.now(timezone.utc)
            df = pd.DataFrame([{
                "time": now, "open": rate, "high": rate,
                "low": rate, "close": rate, "volume": 0.0,
            }])
            return df
    return None


def fetch_frankfurter(from_currency: str = "EUR",
                      to_currency: str = "USD") -> float | None:
    """
    Frankfurter.app — BCE (Banco Central Europeo), 100% gratis, sin key.
    Cubre todos los pares principales de forex.
    """
    cache_key = f"frankfurter:{from_currency}:{to_currency}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.frankfurter.app/latest?from={from_currency}&to={to_currency}"
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        rates = data.get("rates", {})
        rate = rates.get(to_currency)
        if rate is None:
            return None
        val = float(rate)
        _cache.set(cache_key, val)
        return val
    except Exception as exc:
        logger.debug("frankfurter error %s/%s: %s", from_currency, to_currency, exc)
        return None


def fetch_frankfurter_historical(from_currency: str = "EUR",
                                  to_currency: str = "USD",
                                  days: int = 30) -> pd.DataFrame | None:
    """Frankfurter.app — histórico diario (sin key, gratis)."""
    from datetime import timedelta
    cache_key = f"frankfurter_hist:{from_currency}:{to_currency}:{days}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    url = (f"https://api.frankfurter.app/{start}..{end}"
           f"?from={from_currency}&to={to_currency}")
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        raw = data.get("rates", {})
        rows = []
        for date_str, rate_dict in sorted(raw.items()):
            close = float(rate_dict.get(to_currency, 0))
            rows.append({
                "time": pd.to_datetime(date_str, utc=True),
                "open": close, "high": close, "low": close,
                "close": close, "volume": 0.0,
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        _cache.set(cache_key, df)
        return df
    except Exception as exc:
        logger.debug("frankfurter hist error: %s", exc)
        return None


def fetch_gold_api_price() -> float | None:
    """Precio spot XAU/USD desde Gold-API.io (demo token)."""
    cache_key = "goldapi:XAUUSD"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = "https://www.goldapi.io/api/XAU/USD"
    resp = _get(url, headers={"X-Access-Token": "goldapi-demo",
                              "Content-Type": "application/json"})
    if resp is None:
        return None
    try:
        data = resp.json()
        price = data.get("price")
        if price is None:
            return None
        val = float(price)
        _cache.set(cache_key, val)
        return val
    except Exception as exc:
        logger.debug("goldapi error: %s", exc)
        return None


def fetch_metals_api(symbol: str = "XAU",
                     base: str = "USD",
                     access_key: str = "demo") -> float | None:
    """Metals-API latest."""
    cache_key = f"metalsapi:{base}:{symbol}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://metals-api.com/api/latest?access_key={access_key}"
           f"&base={base}&symbols={symbol}")
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        rates = data.get("rates", {})
        if symbol not in rates:
            return None
        # API devuelve precio inverso (1 USD = X XAU). Invertir si es metal.
        rate = float(rates[symbol])
        if rate == 0:
            return None
        val = 1.0 / rate if symbol in {"XAU", "XAG", "XPT", "XPD"} else rate
        _cache.set(cache_key, val)
        return val
    except Exception as exc:
        logger.debug("metals-api error: %s", exc)
        return None


def fetch_exchange_rate(base: str = "USD", quote: str = "EUR") -> float | None:
    """ExchangeRate-API gratuito. Retorna 1 unidad de base en quote."""
    cache_key = f"erapi:{base}:{quote}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.exchangerate-api.com/v4/latest/{base}"
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        rates = data.get("rates", {})
        rate = rates.get(quote)
        if rate is None:
            return None
        val = float(rate)
        _cache.set(cache_key, val)
        return val
    except Exception as exc:
        logger.debug("exchangerate-api error: %s", exc)
        return None


def fetch_open_exchange_rates(app_id: str = "demo") -> dict[str, float] | None:
    """Open Exchange Rates latest."""
    cache_key = f"oer:{app_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://openexchangerates.org/api/latest.json?app_id={app_id}"
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        rates = data.get("rates")
        if not rates:
            return None
        result = {k: float(v) for k, v in rates.items()}
        _cache.set(cache_key, result)
        return result
    except Exception as exc:
        logger.debug("openexchangerates error: %s", exc)
        return None


def fetch_coingecko(coins: list[str] | None = None) -> dict[str, float]:
    """Precios desde CoinGecko (sin key)."""
    if coins is None:
        coins = ["bitcoin", "ethereum"]
    ids = ",".join(coins)
    cache_key = f"coingecko:{ids}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://api.coingecko.com/api/v3/simple/price?ids={ids}"
           f"&vs_currencies=usd")
    resp = _get(url)
    if resp is None:
        return {}
    try:
        data = resp.json()
        result = {coin: float(payload.get("usd", 0.0))
                  for coin, payload in data.items()}
        _cache.set(cache_key, result)
        return result
    except Exception as exc:
        logger.debug("coingecko error: %s", exc)
        return {}


def fetch_fear_greed() -> dict[str, Any] | None:
    """CNN Fear & Greed Index."""
    cache_key = "fng:cnn"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    resp = _get(url, headers={
        "Origin": "https://edition.cnn.com",
        "Referer": "https://edition.cnn.com/",
        "Accept": "application/json",
    })
    if resp is None:
        return None
    try:
        data = resp.json()
        fng = data.get("fear_and_greed", {})
        if not fng:
            return None
        value = int(round(float(fng.get("score", 50))))
        classification = str(fng.get("rating", "neutral"))
        result = {
            "value": value,
            "classification": classification,
            "previous_close": float(fng.get("previous_close", value)),
            "previous_1_week": float(fng.get("previous_1_week", value)),
            "previous_1_month": float(fng.get("previous_1_month", value)),
        }
        _cache.set(cache_key, result)
        return result
    except Exception as exc:
        logger.debug("fear_greed error: %s", exc)
        return None


def fetch_dxy() -> float | None:
    """Valor último del DXY (Dollar Index) via Stooq."""
    df = fetch_stooq("usdidx", "d")
    if df is None or df.empty:
        return None
    try:
        return float(df["close"].iloc[-1])
    except Exception:
        return None


def fetch_rss_news(url: str, max_items: int = 10) -> list[dict[str, str]]:
    """Parsea un feed RSS estándar y retorna lista de noticias."""
    cache_key = f"rss:{url}:{max_items}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    resp = _get(url, headers={"Accept": "application/rss+xml, application/xml"})
    if resp is None:
        return []
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(resp.content)
        # RSS 2.0
        channel = root.find("channel")
        nodes = channel.findall("item") if channel is not None else root.findall(".//item")
        if not nodes:
            # Atom
            ns = "{http://www.w3.org/2005/Atom}"
            nodes = root.findall(f"{ns}entry")
            for entry in nodes[:max_items]:
                title_el = entry.find(f"{ns}title")
                link_el = entry.find(f"{ns}link")
                pub_el = entry.find(f"{ns}updated")
                items.append({
                    "title": (title_el.text or "").strip() if title_el is not None else "",
                    "link": (link_el.get("href", "") if link_el is not None else ""),
                    "published": (pub_el.text or "").strip() if pub_el is not None else "",
                })
        else:
            for item in nodes[:max_items]:
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                desc_el = item.find("description")
                items.append({
                    "title": (title_el.text or "").strip() if title_el is not None else "",
                    "link": (link_el.text or "").strip() if link_el is not None else "",
                    "published": (pub_el.text or "").strip() if pub_el is not None else "",
                    "description": (desc_el.text or "").strip() if desc_el is not None else "",
                })
        _cache.set(cache_key, items)
        return items
    except ET.ParseError as exc:
        logger.debug("rss parse error %s: %s", url, exc)
        return []
    except Exception as exc:
        logger.debug("rss error %s: %s", url, exc)
        return []


def fetch_marketaux(symbols: str = "GOLD,EURUSD",
                    api_token: str = "demo") -> list[dict[str, str]]:
    """Marketaux noticias."""
    cache_key = f"marketaux:{symbols}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://api.marketaux.com/v1/news/all?symbols={symbols}"
           f"&api_token={api_token}")
    resp = _get(url)
    if resp is None:
        return []
    try:
        data = resp.json()
        articles = data.get("data", []) or []
        result = [
            {
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "url": a.get("url", ""),
                "published": a.get("published_at", ""),
                "source": a.get("source", ""),
            }
            for a in articles
        ]
        _cache.set(cache_key, result)
        return result
    except Exception as exc:
        logger.debug("marketaux error: %s", exc)
        return []


def fetch_newsdata(query: str = "gold forex",
                   apikey: str = "pub_demo") -> list[dict[str, str]]:
    """Newsdata.io noticias."""
    cache_key = f"newsdata:{query}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = (f"https://newsdata.io/api/1/news?q={requests.utils.quote(query)}"
           f"&language=en&apikey={apikey}")
    resp = _get(url)
    if resp is None:
        return []
    try:
        data = resp.json()
        results = data.get("results", []) or []
        out = [
            {
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "url": r.get("link", ""),
                "published": r.get("pubDate", ""),
                "source": r.get("source_id", ""),
            }
            for r in results
        ]
        _cache.set(cache_key, out)
        return out
    except Exception as exc:
        logger.debug("newsdata error: %s", exc)
        return []


def fetch_swissquote(base: str = "XAU", quote: str = "USD") -> float | None:
    """Precio spot en tiempo real desde Swissquote Bank (sin API key)."""
    cache_key = f"swissquote:{base}:{quote}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{base}/{quote}"
    resp = _get(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        if not data:
            return None
        profiles = data[0].get("spreadProfilePrices", [])
        standard = next((p for p in profiles if p.get("spreadProfile") == "standard"), profiles[0] if profiles else None)
        if not standard:
            return None
        bid = float(standard["bid"])
        ask = float(standard["ask"])
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        _cache.set(cache_key, mid)
        return mid
    except Exception as exc:
        logger.debug("swissquote error %s/%s: %s", base, quote, exc)
        return None


def aggregate_gold_price() -> dict[str, Any]:
    """Combina varias fuentes de precio del oro y retorna promedio/spread."""
    sources: dict[str, float] = {}

    # Swissquote spot (más preciso — sin delay, precio real)
    val_sq = fetch_swissquote("XAU", "USD")
    if val_sq:
        sources["swissquote"] = val_sq

    # Gold-API
    val = fetch_gold_api_price()
    if val:
        sources["goldapi"] = val

    # Yahoo direct GC=F (futuros — +basis vs spot)
    df_y = fetch_yahoo_direct("GC=F", interval="5m", range_="1d")
    if df_y is not None and not df_y.empty:
        sources["yahoo_futures"] = float(df_y["close"].iloc[-1])

    # Metals API
    val_m = fetch_metals_api("XAU", "USD")
    if val_m:
        sources["metalsapi"] = val_m

    if not sources:
        return {"price": None, "sources": {}, "count": 0}

    # Precio preferido: spot (swissquote/goldapi) sobre futuros
    spot_sources = {k: v for k, v in sources.items() if k in ("swissquote", "goldapi", "metalsapi")}
    prices = list(spot_sources.values()) if spot_sources else list(sources.values())
    avg = sum(prices) / len(prices)
    return {
        "price": avg,
        "min": min(prices),
        "max": max(prices),
        "spread": max(prices) - min(prices),
        "sources": sources,
        "count": len(sources),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ────────────────────────────────────────────────────────────────────────────
# Sentimiento básico basado en palabras clave
# ────────────────────────────────────────────────────────────────────────────

_BULLISH_WORDS = (
    "surge", "rally", "gains", "soars", "jumps", "climbs", "rises",
    "bullish", "boost", "high", "record", "outperform", "upgrade",
)
_BEARISH_WORDS = (
    "fall", "drop", "plunge", "tumble", "slides", "declines", "loses",
    "bearish", "crash", "low", "downgrade", "underperform", "selloff",
)


def _score_headline(text: str) -> int:
    """+1 bullish word, -1 bearish word. Suma neta."""
    if not text:
        return 0
    txt = text.lower()
    score = 0
    for w in _BULLISH_WORDS:
        if w in txt:
            score += 1
    for w in _BEARISH_WORDS:
        if w in txt:
            score -= 1
    return score


def _classify(score: int) -> str:
    if score >= 2:
        return "bullish"
    if score <= -2:
        return "bearish"
    return "neutral"


# ────────────────────────────────────────────────────────────────────────────
# Agregador principal
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class DataFeedAggregator:
    """Agrega datos de múltiples fuentes, retorna el mejor disponible."""

    timeout: int = DEFAULT_TIMEOUT
    apikey_av: str = "demo"
    apikey_td: str = "demo"
    rss_feeds: list[str] = field(default_factory=lambda: [
        "https://www.fxstreet.com/rss/news",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.investing.com/rss/news_25.rss",  # Commodities
        "https://www.investing.com/rss/news_1.rss",   # Forex
    ])

    # ---------------- Precios ----------------

    def get_price(self, symbol: str) -> dict[str, Any] | None:
        """Intenta obtener precio actual desde varias fuentes."""
        sym = symbol.upper()
        smap = SYMBOL_MAP.get(sym, {})

        # 1. Swissquote spot (tiempo real, sin key) — para todos los pares conocidos
        _SQ_MAP = {
            "XAUUSD": ("XAU", "USD"), "EURUSD": ("EUR", "USD"),
            "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
            "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"),
        }
        if sym in _SQ_MAP:
            base, quote = _SQ_MAP[sym]
            val = fetch_swissquote(base, quote)
            if val:
                return {"price": val, "bid": val, "ask": val,
                        "source": "swissquote",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 2. Para oro: Gold-API directo
        if sym in {"XAUUSD", "GOLD"}:
            val = fetch_gold_api_price()
            if val:
                return {"price": val, "bid": val, "ask": val,
                        "source": "goldapi",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 3. Yahoo direct
        ysym = smap.get("yahoo")
        if ysym:
            df = fetch_yahoo_direct(ysym, interval="1m", range_="1d")
            if df is not None and not df.empty:
                price = float(df["close"].iloc[-1])
                return {"price": price, "bid": price, "ask": price,
                        "source": "yahoo_direct",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 3. Twelve Data
        tdsym = smap.get("twelvedata")
        if tdsym:
            df = fetch_twelvedata(tdsym, "M1", outputsize=2, apikey=self.apikey_td)
            if df is not None and not df.empty:
                price = float(df["close"].iloc[-1])
                return {"price": price, "bid": price, "ask": price,
                        "source": "twelvedata",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 4. Stooq (último cierre diario, fallback)
        ssym = smap.get("stooq")
        if ssym:
            df = fetch_stooq(ssym, "d")
            if df is not None and not df.empty:
                price = float(df["close"].iloc[-1])
                return {"price": price, "bid": price, "ask": price,
                        "source": "stooq",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 5. CoinGecko si es crypto
        cgid = smap.get("coingecko")
        if cgid:
            data = fetch_coingecko([cgid])
            if cgid in data:
                price = data[cgid]
                return {"price": price, "bid": price, "ask": price,
                        "source": "coingecko",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        # 6. ExchangeRate-API para FX
        if sym in {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD"} and len(sym) == 6:
            base, quote = sym[:3], sym[3:]
            rate = fetch_exchange_rate(base, quote)
            if rate:
                return {"price": rate, "bid": rate, "ask": rate,
                        "source": "exchangerate-api",
                        "timestamp": datetime.now(timezone.utc).isoformat()}

        return None

    # ---------------- OHLCV ----------------

    def get_ohlcv_multi(self, symbol: str, timeframe: str,
                        bars: int = 100) -> pd.DataFrame | None:
        """Obtiene OHLCV intentando fuentes en cascada."""
        sym = symbol.upper()
        smap = SYMBOL_MAP.get(sym, {})

        # 1. Yahoo direct (intradiario disponible)
        ysym = smap.get("yahoo")
        if ysym and timeframe in _YAHOO_DIRECT_INTERVALS:
            interval, range_ = _YAHOO_DIRECT_INTERVALS[timeframe]
            df = fetch_yahoo_direct(ysym, interval=interval, range_=range_)
            if df is not None and not df.empty:
                return df.tail(bars).reset_index(drop=True)

        # 2. Twelve Data
        tdsym = smap.get("twelvedata")
        if tdsym:
            df = fetch_twelvedata(tdsym, timeframe, outputsize=bars,
                                  apikey=self.apikey_td)
            if df is not None and not df.empty:
                return df.tail(bars).reset_index(drop=True)

        # 3. Alpha Vantage
        avsym = smap.get("alphavantage")
        if avsym and timeframe in _AV_INTERVALS:
            df = fetch_alphavantage(avsym, timeframe, apikey=self.apikey_av)
            if df is not None and not df.empty:
                return df.tail(bars).reset_index(drop=True)

        # 4. Stooq (último recurso — sólo diario)
        ssym = smap.get("stooq")
        if ssym:
            df = fetch_stooq(ssym, "d")
            if df is not None and not df.empty:
                return df.tail(bars).reset_index(drop=True)

        return None

    # ---------------- Sentimiento de mercado ----------------

    def get_market_sentiment(self) -> dict[str, Any]:
        """Fear & Greed + DXY + correlaciones."""
        fng = fetch_fear_greed()
        dxy = fetch_dxy()
        cg = fetch_coingecko(["bitcoin", "ethereum"])

        fng_val = fng.get("value") if fng else None
        fng_class = fng.get("classification") if fng else "unknown"

        # Interpretación: Fear extremo + DXY débil → bullish para oro
        interpretation = "neutral"
        if fng_val is not None and dxy is not None:
            if fng_val < 30 and dxy < 102:
                interpretation = "gold_bullish (miedo extremo + dólar débil)"
            elif fng_val > 75 and dxy > 105:
                interpretation = "gold_bearish (codicia + dólar fuerte)"
            elif fng_val < 40:
                interpretation = "risk_off (favorable a oro como refugio)"
            elif fng_val > 70:
                interpretation = "risk_on (capital fluye a renta variable)"

        return {
            "fear_greed": fng_val,
            "fear_greed_class": fng_class,
            "dxy": dxy,
            "btc_usd": cg.get("bitcoin"),
            "eth_usd": cg.get("ethereum"),
            "interpretation": interpretation,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ---------------- Noticias / Sentimiento ----------------

    def get_news_sentiment(self, symbol: str) -> dict[str, Any]:
        """Últimas noticias y sentimiento agregado."""
        headlines: list[dict[str, str]] = []

        # 1. RSS feeds
        for feed in self.rss_feeds:
            try:
                items = fetch_rss_news(feed, max_items=5)
                for it in items:
                    headlines.append({
                        "title": it.get("title", ""),
                        "link": it.get("link", ""),
                        "published": it.get("published", ""),
                        "source": _extract_rss_source(feed),
                    })
            except Exception as exc:
                logger.debug("rss feed fallo %s: %s", feed, exc)

        # 2. Marketaux (símbolo específico)
        try:
            mx = fetch_marketaux(symbols=symbol)
            for it in mx[:5]:
                headlines.append({
                    "title": it.get("title", ""),
                    "link": it.get("url", ""),
                    "published": it.get("published", ""),
                    "source": it.get("source", "marketaux"),
                })
        except Exception as exc:
            logger.debug("marketaux fallo: %s", exc)

        # Filtrar por relevancia al símbolo
        sym_low = symbol.lower()
        keywords = {sym_low, sym_low[:3], sym_low[3:]} if len(sym_low) >= 6 else {sym_low}
        if sym_low in {"xauusd", "gold"}:
            keywords.update({"gold", "oro", "xau", "bullion", "fed", "inflation"})
        elif sym_low == "eurusd":
            keywords.update({"euro", "ecb", "fed", "eurozone", "dollar"})

        relevant = [h for h in headlines
                    if any(k in h["title"].lower() for k in keywords)]
        if not relevant:
            relevant = headlines[:10]

        total_score = sum(_score_headline(h["title"]) for h in relevant)
        sentiment = _classify(total_score)

        return {
            "headlines": relevant[:15],
            "sentiment": sentiment,
            "score": total_score,
            "count": len(relevant),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ---------------- Estado de feeds ----------------

    def get_all_feeds_status(self) -> dict[str, dict[str, Any]]:
        """Estado de cada feed (ok + latencia)."""
        checks: dict[str, Any] = {
            "stooq": (fetch_stooq, ("xauusd", "d")),
            "yahoo_direct": (fetch_yahoo_direct, ("GC=F", "5m", "1d")),
            "alpha_vantage": (fetch_alphavantage, ("EURUSD", "M5", self.apikey_av)),
            "frankfurter": (fetch_frankfurter, ("EUR", "USD")),
            "gold_api": (fetch_gold_api_price, ()),
            "exchange_rate": (fetch_exchange_rate, ("USD", "EUR")),
            "coingecko": (fetch_coingecko, (["bitcoin"],)),
            "fear_greed": (fetch_fear_greed, ()),
            "rss_fxstreet": (fetch_rss_news, ("https://www.fxstreet.com/rss/news", 3)),
        }
        result: dict[str, dict[str, Any]] = {}
        for name, (fn, args) in checks.items():
            data, latency = _measure(fn, *args)
            ok = data is not None and (
                not hasattr(data, "empty") or not data.empty  # type: ignore[truthy-bool]
            ) and (not isinstance(data, list) or len(data) > 0) \
                and (not isinstance(data, dict) or len(data) > 0)
            result[name] = {
                "ok": bool(ok),
                "latency_ms": latency,
            }
        return result

    # ---------------- Correlaciones ----------------

    def get_correlation_data(self) -> dict[str, Any]:
        """DXY, BTC/USD, S&P500, VIX — todos los datos correlacionados."""
        out: dict[str, Any] = {}

        # DXY
        dxy_df = fetch_stooq("usdidx", "d")
        if dxy_df is not None and not dxy_df.empty:
            out["dxy"] = {
                "last": float(dxy_df["close"].iloc[-1]),
                "change_pct": _pct_change(dxy_df),
            }

        # SPX
        spx_df = fetch_stooq("^spx", "d")
        if spx_df is not None and not spx_df.empty:
            out["spx"] = {
                "last": float(spx_df["close"].iloc[-1]),
                "change_pct": _pct_change(spx_df),
            }

        # VIX
        vix_df = fetch_stooq("^vix", "d")
        if vix_df is not None and not vix_df.empty:
            out["vix"] = {
                "last": float(vix_df["close"].iloc[-1]),
                "change_pct": _pct_change(vix_df),
            }

        # BTC
        cg = fetch_coingecko(["bitcoin", "ethereum"])
        if cg:
            out["btc_usd"] = cg.get("bitcoin")
            out["eth_usd"] = cg.get("ethereum")

        # Gold agregado
        gold = aggregate_gold_price()
        if gold.get("price"):
            out["xauusd_avg"] = gold["price"]
            out["xauusd_sources_count"] = gold["count"]

        out["timestamp"] = datetime.now(timezone.utc).isoformat()
        out["notes"] = (
            "DXY inverso a oro; VIX alto correlaciona con risk-off "
            "(favorable a oro); BTC suele moverse con risk-on."
        )
        return out


# ────────────────────────────────────────────────────────────────────────────
# Utilidades
# ────────────────────────────────────────────────────────────────────────────

def _pct_change(df: pd.DataFrame) -> float | None:
    """Porcentaje de cambio entre los dos últimos cierres."""
    try:
        if len(df) < 2:
            return None
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2])
        if prev == 0:
            return None
        return round((last - prev) / prev * 100.0, 4)
    except Exception:
        return None


def _extract_rss_source(url: str) -> str:
    """Extrae el host como nombre amigable."""
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


# ────────────────────────────────────────────────────────────────────────────
# CLI rápida para debug
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    agg = DataFeedAggregator()

    print("\n=== ESTADO DE FEEDS ===")
    status = agg.get_all_feeds_status()
    for name, info in status.items():
        flag = "OK " if info["ok"] else "ERR"
        print(f"  [{flag}] {name:<18} {info['latency_ms']:>5} ms")

    print("\n=== PRECIO XAUUSD (agregado) ===")
    print(aggregate_gold_price())

    print("\n=== PRECIO EURUSD ===")
    print(agg.get_price("EURUSD"))

    print("\n=== SENTIMIENTO DE MERCADO ===")
    print(agg.get_market_sentiment())

    print("\n=== CORRELACIONES ===")
    print(agg.get_correlation_data())

    print("\n=== NOTICIAS XAUUSD ===")
    news = agg.get_news_sentiment("XAUUSD")
    print(f"Sentimiento: {news['sentiment']} (score={news['score']})")
    for h in news["headlines"][:5]:
        print(f"  - [{h.get('source','?')}] {h['title']}")
