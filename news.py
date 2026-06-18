"""Economic calendar — ForexFactory JSON con cache persistente en disco (60 min)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

FF_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_FF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.forexfactory.com/",
    "Cache-Control": "no-cache",
}

SYMBOL_CURRENCY_MAP: dict[str, list[str]] = {
    "XAUUSD": ["USD", "XAU"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "AUDUSD": ["AUD", "USD"],
    "USDCAD": ["USD", "CAD"],
    "USDCHF": ["USD", "CHF"],
    "NZDUSD": ["NZD", "USD"],
}

_DISK_CACHE = Path(__file__).parent / "news_cache.json"
_CACHE_TTL = 3600.0          # 60 min en memoria
_DISK_TTL  = 7200.0          # 2 h en disco (sobrevive reinicios)
_RETRY_429_WAIT = 3600.0     # si recibimos 429, esperar 1 h antes de reintentar

_mem: dict[str, Any] = {"events": [], "fetched_at": 0.0}
_last_429_ts: float = 0.0


def _normalize_impact(raw: str) -> str:
    r = str(raw).strip().lower()
    if r in ("high", "high impact", "3"):
        return "HIGH"
    if r in ("medium", "moderate", "2"):
        return "MEDIUM"
    return "LOW"


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    fmts = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(raw, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def _load_disk_cache() -> list[dict[str, Any]]:
    """Lee eventos del caché en disco si son frescos (< 2h)."""
    try:
        if not _DISK_CACHE.exists():
            return []
        stat = _DISK_CACHE.stat()
        age = time.time() - stat.st_mtime
        if age > _DISK_TTL:
            return []
        payload = json.loads(_DISK_CACHE.read_text(encoding="utf-8"))
        events = payload.get("events", [])
        logger.info("Caché en disco cargado: %d eventos (%.0f min de antigüedad)", len(events), age / 60)
        return events
    except Exception:
        return []


def _save_disk_cache(events: list[dict[str, Any]]) -> None:
    try:
        _DISK_CACHE.write_text(
            json.dumps({"events": events, "saved_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("No se pudo guardar caché en disco: %s", exc)


def fetch_ff_calendar(force: bool = False) -> list[dict[str, Any]]:
    """
    Descarga el calendario semanal de ForexFactory.
    Prioridad: memoria (60 min) → disco (2 h) → HTTP.
    Si FF devuelve 429, espera 1 h antes de reintentar.
    """
    global _last_429_ts
    now_ts = time.time()

    # 1. Caché en memoria
    if not force and (now_ts - float(_mem.get("fetched_at", 0))) < _CACHE_TTL:
        cached = _mem.get("events") or []
        if cached:
            return list(cached)

    # 2. Caché en disco (sobrevive reinicios del servidor)
    if not force:
        disk_events = _load_disk_cache()
        if disk_events:
            _mem["events"] = disk_events
            _mem["fetched_at"] = now_ts
            return list(disk_events)

    # 3. Respetar backoff de 429
    if not force and (now_ts - _last_429_ts) < _RETRY_429_WAIT:
        wait_min = int((_RETRY_429_WAIT - (now_ts - _last_429_ts)) / 60)
        logger.debug("FF en rate-limit — reintentando en %d min", wait_min)
        return list(_mem.get("events") or [])

    # 4. Petición HTTP
    try:
        resp = requests.get(FF_JSON, headers=_FF_HEADERS, timeout=12)
        if resp.status_code == 429:
            _last_429_ts = now_ts
            logger.warning("FF devolvió 429 — esperando %.0f min", _RETRY_429_WAIT / 60)
            return list(_mem.get("events") or [])
        resp.raise_for_status()
        raw_events: list[dict[str, Any]] = resp.json()
    except Exception as exc:
        logger.warning("FF JSON fetch falló (%s) — usando caché", exc)
        return list(_mem.get("events") or [])

    events: list[dict[str, Any]] = []
    for e in raw_events:
        dt = _parse_dt(str(e.get("date", "")))
        if dt is None:
            continue
        events.append(
            {
                "title": str(e.get("title", "")).strip(),
                "currency": str(e.get("country", "")).strip().upper(),
                "impact": _normalize_impact(e.get("impact", "low")),
                "event_time": dt.isoformat(),
                "actual": e.get("actual"),
                "forecast": e.get("forecast"),
                "previous": e.get("previous"),
            }
        )

    if events:
        _mem["events"] = events
        _mem["fetched_at"] = now_ts
        _save_disk_cache(events)
        logger.info("Calendario FF actualizado desde HTTP: %d eventos", len(events))
    return events


def _event_dt(event: dict[str, Any]) -> datetime | None:
    try:
        t = datetime.fromisoformat(event["event_time"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    except Exception:
        return None


def get_high_impact_events(hours_ahead: int = 4) -> list[dict[str, Any]]:
    """Eventos HIGH en las próximas `hours_ahead` horas (desde ahora)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours_ahead)
    events = fetch_ff_calendar()
    out: list[dict[str, Any]] = []
    for e in events:
        if e.get("impact") != "HIGH":
            continue
        t = _event_dt(e)
        if t is None:
            continue
        if now <= t <= horizon:
            ev = dict(e)
            ev["minutes_until"] = int((t - now).total_seconds() // 60)
            out.append(ev)
    out.sort(key=lambda x: x["event_time"])
    return out


def get_all_week_events() -> list[dict[str, Any]]:
    """Todos los eventos de la semana ISO actual."""
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday_end = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    events = fetch_ff_calendar()
    out: list[dict[str, Any]] = []
    for e in events:
        t = _event_dt(e)
        if t is None:
            continue
        if monday <= t <= sunday_end:
            ev = dict(e)
            ev["minutes_until"] = int((t - now).total_seconds() // 60)
            out.append(ev)
    out.sort(key=lambda x: x["event_time"])
    return out


def get_upcoming_events_today() -> dict[str, list[dict[str, Any]]]:
    """Eventos HIGH+MEDIUM de las próximas 24h, agrupados por moneda."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    events = fetch_ff_calendar()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("impact") not in ("HIGH", "MEDIUM"):
            continue
        t = _event_dt(e)
        if t is None or not (now <= t <= horizon):
            continue
        ev = dict(e)
        ev["minutes_until"] = int((t - now).total_seconds() // 60)
        currency = ev.get("currency") or "OTHER"
        grouped.setdefault(currency, []).append(ev)
    for items in grouped.values():
        items.sort(key=lambda x: x["event_time"])
    return grouped


def get_next_major_event(symbol: str) -> dict[str, Any] | None:
    """Próximo evento HIGH para las monedas del símbolo, o None."""
    relevant = SYMBOL_CURRENCY_MAP.get(symbol.upper(), [])
    now = datetime.now(timezone.utc)
    events = fetch_ff_calendar()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for e in events:
        if e.get("impact") != "HIGH":
            continue
        cur = e.get("currency", "")
        if relevant and cur not in relevant:
            continue
        t = _event_dt(e)
        if t is None or t <= now:
            continue
        candidates.append((t, e))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    t, ev = candidates[0]
    out = dict(ev)
    out["minutes_until"] = int((t - now).total_seconds() // 60)
    return out


def is_news_time(symbol: str, buffer_hours: float = 2.0) -> bool:
    relevant = SYMBOL_CURRENCY_MAP.get(symbol.upper(), [])
    events = get_high_impact_events(hours_ahead=int(max(1, buffer_hours)))
    for e in events:
        if not relevant or e.get("currency", "") in relevant:
            return True
    return False


def get_news_summary(symbol: str) -> str:
    relevant = SYMBOL_CURRENCY_MAP.get(symbol.upper(), [])
    events = get_high_impact_events(hours_ahead=6)
    for e in events:
        if not relevant or e.get("currency", "") in relevant:
            minutes = e.get("minutes_until", 0)
            delta = f"+{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"+{minutes}m"
            return f"{e['title']} {delta} ({e['impact']} impacto)"
    return "Sin noticias relevantes próximas"


def _parse_news_value(val_str: str | None) -> float | None:
    """Parsea '207K', '3.2%', '-0.1', '1.2M' → float. Retorna None si no parseable."""
    if not val_str:
        return None
    s = str(val_str).strip().replace(",", ".").rstrip("%").strip()
    mult = 1.0
    if s.upper().endswith("K"):
        mult = 1_000.0; s = s[:-1]
    elif s.upper().endswith("M"):
        mult = 1_000_000.0; s = s[:-1]
    elif s.upper().endswith("B"):
        mult = 1_000_000_000.0; s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


def get_just_released_events(symbol: str, max_age_min: int = 5) -> list[dict[str, Any]]:
    """
    Eventos HIGH-impact del símbolo que ya tienen 'actual' publicado
    y se liberaron en los últimos max_age_min minutos.
    """
    relevant = SYMBOL_CURRENCY_MAP.get(symbol.upper(), [])
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_min)
    events = fetch_ff_calendar()
    out: list[dict[str, Any]] = []
    for e in events:
        if e.get("impact") != "HIGH":
            continue
        if not e.get("actual"):
            continue
        if relevant and e.get("currency", "") not in relevant:
            continue
        t = _event_dt(e)
        if t is None:
            continue
        if cutoff <= t <= now:
            ev = dict(e)
            ev["age_minutes"] = int((now - t).total_seconds() // 60)
            out.append(ev)
    out.sort(key=lambda x: x["event_time"], reverse=True)
    return out


def calc_news_direction(symbol: str, event: dict[str, Any]) -> tuple[str | None, str]:
    """
    Determina dirección de operación por sorpresa del dato macro.
    Retorna (direction, explanation): direction = 'BULLISH' | 'BEARISH' | None.
    """
    sym = symbol.upper()
    currency = event.get("currency", "")
    title = event.get("title", "")
    actual_val   = _parse_news_value(event.get("actual"))
    forecast_val = _parse_news_value(event.get("forecast"))
    previous_val = _parse_news_value(event.get("previous"))

    if actual_val is None:
        return None, "Sin valor actual parseable"

    reference = forecast_val if forecast_val is not None else previous_val
    if reference is None:
        return None, "Sin forecast/previous para comparar"

    # Para métricas negativas (desempleo, claims) menor = mejor
    is_negative = any(kw in title.lower() for kw in ("unemployment", "jobless", "claims", "desempleo"))
    beats = actual_val > reference
    if is_negative:
        beats = actual_val < reference

    # Solo actuar si la sorpresa es significativa
    margin_pct = abs(actual_val - reference) / abs(reference) * 100 if reference != 0 else 0
    if margin_pct < 1.5 and abs(actual_val - reference) < 0.05:
        return None, f"Sorpresa insuficiente ({margin_pct:.1f}%)"

    direction: str | None = None
    if currency == "USD":
        # USD fuerte → oro/EUR/GBP caen; JPY/CAD/CHF suben contra USD
        if sym in ("XAUUSD", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"):
            direction = "BEARISH" if beats else "BULLISH"
        elif sym in ("USDJPY", "USDCAD", "USDCHF"):
            direction = "BULLISH" if beats else "BEARISH"
    elif currency == "EUR" and sym == "EURUSD":
        direction = "BULLISH" if beats else "BEARISH"
    elif currency == "GBP" and sym == "GBPUSD":
        direction = "BULLISH" if beats else "BEARISH"
    elif currency == "AUD" and sym == "AUDUSD":
        direction = "BULLISH" if beats else "BEARISH"

    if direction is None:
        return None, f"Moneda {currency} no relevante para {sym}"

    resultado = "mejor de lo esperado" if beats else "peor de lo esperado"
    explanation = (
        f"📰 {title}: {event.get('actual')} vs {event.get('forecast', '?')} "
        f"({currency} {resultado}) → {direction}"
    )
    return direction, explanation


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== HIGH impact próximas 24h ===")
    for ev in get_high_impact_events(24):
        print(f"  {ev['event_time']}  [{ev['currency']}]  {ev['title']}  ({ev['minutes_until']} min)")
