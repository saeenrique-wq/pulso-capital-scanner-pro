"""Notificaciones Telegram — Pulso Capital MX Scanner PRO."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}/{method}"

_MX_OFFSET = timezone(timedelta(hours=-6))

_NOMBRES = {
    "XAUUSD": "ORO",
    "GOLD":   "ORO",
    "EURUSD": "EURO / DÓLAR",
    "GBPUSD": "LIBRA / DÓLAR",
    "USDJPY": "DÓLAR / YEN",
    "BTCUSD": "BITCOIN",
    "ETHUSD": "ETHEREUM",
    "XRPUSD": "XRP / RIPPLE",
}

_NOMBRES_TF = {
    "M1":  "1 minuto",
    "M5":  "5 minutos",
    "M15": "15 minutos",
    "H1":  "1 hora",
    "H4":  "4 horas",
    "D1":  "Diario",
}

_DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


# ─────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────

def _enabled(config: dict[str, Any]) -> bool:
    tg = config.get("telegram", {})
    token = tg.get("token", "")
    chat_id = tg.get("chat_id", "")
    return bool(token) and "YOUR_" not in token and bool(chat_id)


def _post(config: dict[str, Any], method: str, payload: dict[str, Any]) -> bool:
    if not _enabled(config):
        logger.debug("Telegram desactivado o sin configurar")
        return False
    token = config["telegram"]["token"]
    url = _BASE.format(token=token, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram %s falló: %s — %s", method, resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("Telegram %s excepción: %s", method, exc)
        return False


def _send_text(config: dict[str, Any], texto: str) -> bool:
    return _post(config, "sendMessage", {
        "chat_id": config["telegram"]["chat_id"],
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _send_gif_con_caption(config: dict[str, Any], gif_ref: str, caption: str) -> bool:
    """Envía animación (file_id nativo o URL). Texto largo va por separado."""
    chat_id = config["telegram"]["chat_id"]
    if not gif_ref:
        return _send_text(config, caption)

    if len(caption) <= 900:
        ok = _post(config, "sendAnimation", {
            "chat_id": chat_id,
            "animation": gif_ref,
            "caption": caption,
            "parse_mode": "HTML",
        })
        if ok:
            return True
        return _send_text(config, caption)
    else:
        _post(config, "sendAnimation", {"chat_id": chat_id, "animation": gif_ref})
        return _send_text(config, caption)


def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if s in {"XAUUSD", "GOLD"}:
        return 0.1
    if s == "BTCUSD":
        return 1.0
    if s == "ETHUSD":
        return 0.1
    if s == "XRPUSD":
        return 0.0001
    return 0.0001


def _pips(a: float, b: float, symbol: str) -> float:
    pip = _pip_size(symbol)
    return round(abs(a - b) / pip, 1)


import json as _json
from pathlib import Path as _Path

_GIF_IDS_PATH = _Path(__file__).parent / "gif_ids.json"


def _load_gif_ids() -> dict[str, list[str]]:
    try:
        if _GIF_IDS_PATH.exists():
            return _json.loads(_GIF_IDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_gif_id(tipo: str, file_id: str) -> bool:
    """Guarda un file_id de Telegram para un tipo de GIF. Llamado desde telegram_commands."""
    try:
        data = _load_gif_ids()
        if tipo not in data:
            data[tipo] = []
        if file_id not in data[tipo]:
            data[tipo].append(file_id)
        _GIF_IDS_PATH.write_text(
            _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception:
        return False


_GIFS_POOL: dict[str, list[str]] = {
    "compra": [
        "https://media.giphy.com/media/3o7qDEq2bMbcbPRQ2c/giphy.gif",
        "https://media.giphy.com/media/26BRuo6sLetdllPAQ/giphy.gif",
        "https://media.giphy.com/media/l0MYyoYVblTGklp0k/giphy.gif",
        "https://media.giphy.com/media/xT9IgzoKnwFNmISR8I/giphy.gif",
        "https://media.giphy.com/media/3orieTkKTk7aXxKyCI/giphy.gif",
    ],
    "venta": [
        "https://media.giphy.com/media/l0HlHFRbmaZtBRhXG/giphy.gif",
        "https://media.giphy.com/media/3o7TKnCdBx5eMChGfu/giphy.gif",
        "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif",
        "https://media.giphy.com/media/9Y5BbDSkSTiY8/giphy.gif",
        "https://media.giphy.com/media/l4pTjOu0NsrLApt0c/giphy.gif",
    ],
    "tp1": [
        "https://media.giphy.com/media/5GoVLqeAOo6PK/giphy.gif",
        "https://media.giphy.com/media/3ohzdIuqJoo8QdKlnW/giphy.gif",
        "https://media.giphy.com/media/26tPplGWjN0xLybiU/giphy.gif",
        "https://media.giphy.com/media/l3q2HZoRlFdEjmMr6/giphy.gif",
    ],
    "tp2": [
        "https://media.giphy.com/media/g9582DNuQppxC/giphy.gif",
        "https://media.giphy.com/media/26tOZ42Mg6pbTUPHW/giphy.gif",
        "https://media.giphy.com/media/3o6gEdGnIDNW4Kd20A/giphy.gif",
        "https://media.giphy.com/media/l1J3mKbFJklGpGGPS/giphy.gif",
    ],
    "tp3": [
        "https://media.giphy.com/media/artj92V8o75VPL7AeQ/giphy.gif",
        "https://media.giphy.com/media/26tOZ42Mg6pbTUPHW/giphy.gif",
        "https://media.giphy.com/media/26gsaJrGkJJnQ5bte/giphy.gif",
        "https://media.giphy.com/media/l0MYwdebx8o0XI5LG/giphy.gif",
    ],
    "sl": [
        "https://media.giphy.com/media/3o7TKqnN349PBUtGFy/giphy.gif",
        "https://media.giphy.com/media/H1dxi6xdh4NGQCZSvz/giphy.gif",
        "https://media.giphy.com/media/26ufnwz3wDUli7GU0/giphy.gif",
        "https://media.giphy.com/media/3o6ZsZoMbME9YFzEkU/giphy.gif",
    ],
    "volatilidad": [
        "https://media.giphy.com/media/l0IykOsxLECVejOzm/giphy.gif",
        "https://media.giphy.com/media/3oEjHGnY8oB4BHCOCA/giphy.gif",
    ],
    "noticia": [
        "https://media.giphy.com/media/xT9IgzoKnwFNmISR8I/giphy.gif",
        "https://media.giphy.com/media/3o7TKP9ln2Dr6ze6f6/giphy.gif",
    ],
}


def _gif(config: dict[str, Any], clave: str) -> str:
    """Retorna file_id nativo (premium) si existe, sino URL del pool."""
    ids = _load_gif_ids().get(clave, [])
    if ids:
        return random.choice(ids)
    pool = _GIFS_POOL.get(clave, [])
    if pool:
        return random.choice(pool)
    return config.get("gifs", {}).get(clave, "")


def _mx_time(utc_dt: datetime | None = None) -> datetime:
    """Convierte UTC -> Ciudad de México (UTC-6, sin DST)."""
    if utc_dt is None:
        utc_dt = datetime.now(timezone.utc)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_MX_OFFSET)


def _fmt_mx_full(utc_dt: datetime | None = None) -> str:
    mx = _mx_time(utc_dt)
    dia = _DIAS_ES[mx.weekday()]
    mes = _MESES_ES[mx.month - 1]
    return f"{dia} {mx.day} de {mes}, {mx.strftime('%H:%M')} (hora CDMX)"


def _fmt_mx_short(utc_dt: datetime | None = None) -> str:
    mx = _mx_time(utc_dt)
    return mx.strftime("%H:%M") + " CDMX"


# ─────────────────────────────────────────────────────────────────
# Mensaje de arranque del scanner
# ─────────────────────────────────────────────────────────────────

def send_startup_message(config: dict[str, Any]) -> bool:
    ahora_mx = _fmt_mx_full()
    texto = (
        "🚀 <b>PULSO CAPITAL MX — SCANNER PRO ACTIVO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "El scanner está <b>en línea</b> y monitoreando:\n\n"
        "   • 🥇 <b>ORO</b> (XAUUSD)\n"
        "   • 💶 <b>EURO/DÓLAR</b> (EURUSD)\n\n"
        "📡 Fuente de precios: <b>Swissquote</b> (tiempo real)\n"
        f"🕐 {ahora_mx}\n\n"
        "<i>Las señales llegarán cuando el análisis encuentre\n"
        "una entrada válida. No spam — solo oportunidades reales.</i>\n\n"
        "<i>Pulso Capital MX 🇲🇽</i>"
    )
    return _send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Señal nueva
# ─────────────────────────────────────────────────────────────────

_TRADE_TYPE: dict[str, dict[str, str]] = {
    "M1": {
        "label":    "SCALP RÁPIDO",
        "emoji":    "⚡⚡⚡",
        "duracion": "1-5 minutos",
        "validez":  "5 min",
    },
    "M5": {
        "label":    "SCALP",
        "emoji":    "⚡⚡",
        "duracion": "5-20 minutos",
        "validez":  "15 min",
    },
    "M15": {
        "label":    "INTRADAY",
        "emoji":    "⚡",
        "duracion": "minutos a 1-2 horas",
        "validez":  "30 min",
    },
    "H1": {
        "label":    "SWING CORTO",
        "emoji":    "📊",
        "duracion": "horas a 1 día",
        "validez":  "8 horas",
    },
    "H4": {
        "label":    "SWING",
        "emoji":    "📈",
        "duracion": "1-3 días",
        "validez":  "48 horas",
    },
    "D1": {
        "label":    "POSICIÓN",
        "emoji":    "🏦",
        "duracion": "días a 1-2 semanas",
        "validez":  "7 días",
    },
}


def format_signal_message(signal: dict[str, Any]) -> str:
    sig_type   = signal.get("signal_type", "").upper()
    es_compra  = sig_type.startswith("BUY")
    symbol     = signal.get("symbol", "")
    tf         = signal.get("timeframe", "")
    entry      = float(signal.get("entry", 0))
    tp1        = float(signal.get("tp1", 0))
    tp2        = float(signal.get("tp2", 0))
    tp3        = float(signal.get("tp3", 0))
    sl         = float(signal.get("sl", 0))
    confidence = int(signal.get("confidence", 0))
    news_summary = str(signal.get("news_summary", "")).strip()

    nombre = _NOMBRES.get(symbol.upper(), symbol)
    es_oro = "XAU" in symbol.upper()
    digits = 2 if es_oro else 5
    fmt = lambda v: f"{v:.{digits}f}"

    def _pct(target: float) -> str:
        if entry <= 0:
            return ""
        pct = (target - entry) / entry * 100
        return f"{pct:+.2f}%"

    dist_sl  = abs(entry - sl)
    dist_tp1 = abs(tp1 - entry)
    dist_tp3 = abs(tp3 - entry)
    rr3 = round(dist_tp3 / dist_sl, 1) if dist_sl > 0 else 0

    tt = _TRADE_TYPE.get(tf, {"label": tf, "emoji": "📌", "validez": "?", "duracion": "?"})
    hora_mx = _fmt_mx_short()

    if es_compra:
        header = f"🟢🟢 <b>COMPRAR — {nombre}</b> 🟢🟢"
        accion_signo = "+"
    else:
        header = f"🔴🔴 <b>VENDER — {nombre}</b> 🔴🔴"
        accion_signo = "-"

    noticia = f"\n⚠️ {news_summary}" if news_summary and "Sin noticias" not in news_summary else ""

    texto = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{tt['emoji']} <b>{tt['label']}</b>  ·  {tf}  ·  {hora_mx}\n"
        f"\n"
        f"✅ <b>ENTRA AHORA, a precio de mercado</b>\n"
        f"📍 Precio de entrada: <code>{fmt(entry)}</code>\n"
        f"\n"
        f"🛑 <b>Stop Loss</b> (tu límite de pérdida):\n"
        f"     <code>{fmt(sl)}</code>  ({_pct(sl)})\n"
        f"\n"
        f"🎯 <b>TP1</b> (cierra una parte aquí): <code>{fmt(tp1)}</code>  ({_pct(tp1)})\n"
        f"🎯 <b>TP2</b> (cierra otra parte):     <code>{fmt(tp2)}</code>  ({_pct(tp2)})\n"
        f"🎯 <b>TP3</b> (objetivo final 🏆):     <code>{fmt(tp3)}</code>  ({_pct(tp3)})\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚖️ Por cada $1 que arriesgas (hasta el SL), puedes ganar hasta ${rr3} si llega al TP3 (RR 1:{rr3})\n"
        f"📊 Confianza del análisis: {confidence}%\n"
        f"⏳ Este tipo de señal ({tt['label']}) suele tardar <b>{tt.get('duracion','?')}</b> en desarrollarse — no esperes que llegue al TP en minutos. La iremos siguiendo y avisando aquí mismo.\n"
        f"💡 Estos precios son del activo (oro), no de dinero — ajusta cuánto arriesgas según tu propio tamaño de posición."
        f"{noticia}"
    )
    return texto


def send_result_summary(config: dict[str, Any], signal: dict[str, Any]) -> bool:
    """Breve resumen del resultado de la señal anterior, antes de enviar la siguiente."""
    symbol   = signal.get("symbol", "")
    sig_type = signal.get("signal_type", "")
    status   = signal.get("status", "")
    pnl      = float(signal.get("pnl_pips", 0) or 0)
    nombre   = _NOMBRES.get(symbol.upper(), symbol)
    accion   = "COMPRA" if "BUY" in sig_type.upper() else "VENTA"

    if status == "TP3":
        icono   = "🏆"
        titulo  = "GANAMOS — TODOS LOS OBJETIVOS"
        detalle = f"TP1 ✅  TP2 ✅  TP3 ✅  →  +{pnl:.0f} pips ganados"
    elif status == "SL":
        icono   = "❌"
        titulo  = "STOP LOSS"
        if pnl < 0:
            detalle = f"Mercado fue en contra  →  {pnl:.0f} pips"
        else:
            detalle = "Cerrado en breakeven (entrada) — sin pérdida ✅"
    elif status == "CANCELLED" and pnl > 0:
        # Llegó a TP1 o TP2 antes de expirar
        icono   = "✅"
        titulo  = "GANAMOS PARCIAL"
        detalle = (
            f"La señal llegó a objetivos y cerró parcialmente\n"
            f"   +{pnl:.0f} pips realizados — el resto expiró sin llegar al TP3"
        )
    else:
        icono   = "⏰"
        titulo  = "SEÑAL EXPIRADA"
        detalle = "El precio no llegó a ningún objetivo antes del tiempo límite"

    texto = (
        f"{icono} <b>{titulo} — {nombre}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{accion}  ·  {detalle}\n\n"
        f"🔄 <b>Nueva señal entrando ahora...</b>"
    )
    return _send_text(config, texto)


def send_tracking_update(
    config: dict[str, Any],
    signal: dict[str, Any],
    current_price: float,
) -> bool:
    symbol  = signal.get("symbol", "")
    is_buy  = "BUY" in signal.get("signal_type", "").upper()
    status  = signal.get("status", "ACTIVE")
    entry   = float(signal.get("entry", 0))
    tp1     = float(signal.get("tp1", 0))
    tp2     = float(signal.get("tp2", 0))
    tp3     = float(signal.get("tp3", 0))
    sl      = float(signal.get("sl", 0))
    pip     = _pip_size(symbol)
    digits  = 2 if "XAU" in symbol.upper() else 5
    fmt     = lambda v: f"{v:.{digits}f}"
    nombre  = _NOMBRES.get(symbol.upper(), symbol)
    accion  = "COMPRA" if is_buy else "VENTA"

    # Pips desde entrada — positivo = a favor, negativo = en contra
    mov = round((current_price - entry if is_buy else entry - current_price) / pip, 0)
    mov_txt = f"+{mov:.0f}p" if mov >= 0 else f"{mov:.0f}p"

    # TPs alcanzados y próximo objetivo
    if status == "TP2":
        tps_txt = "✅ TP1   ✅ TP2   ⬜ TP3"
        prox_label, prox_val = "TP3", tp3
    elif status == "TP1":
        tps_txt = "✅ TP1   ⬜ TP2   ⬜ TP3"
        prox_label, prox_val = "TP2", tp2
    else:
        tps_txt = "⬜ TP1   ⬜ TP2   ⬜ TP3"
        prox_label, prox_val = "TP1", tp1

    dist_prox = round(abs(current_price - prox_val) / pip)
    dist_sl   = round(abs(current_price - sl) / pip)

    texto = (
        f"📡 <b>{nombre} — {accion}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entrada: <code>{fmt(entry)}</code>\n"
        f"Precio:  <code>{fmt(current_price)}</code>  ({mov_txt} desde entrada)\n\n"
        f"{tps_txt}\n\n"
        f"🎯 {prox_label}: <code>{fmt(prox_val)}</code>  —  {dist_prox}p\n"
        f"🛑 SL:  <code>{fmt(sl)}</code>  —  {dist_sl}p"
    )
    return _send_text(config, texto)


def send_exit_warning(
    config: dict[str, Any],
    signal: dict[str, Any],
    current_price: float,
) -> bool:
    """Aviso de cierre preventivo — señal llegó 75% hacia el SL."""
    symbol   = signal.get("symbol", "")
    is_buy   = "BUY" in signal.get("signal_type", "").upper()
    entry    = float(signal.get("entry", 0))
    sl       = float(signal.get("sl", 0))
    nombre   = _NOMBRES.get(symbol.upper(), symbol)
    pip      = _pip_size(symbol)
    digits   = 2 if "XAU" in symbol.upper() else 5
    fmt      = lambda v: f"{v:.{digits}f}"
    pnl      = round((current_price - entry if is_buy else entry - current_price) / pip, 0)
    accion   = "COMPRA" if is_buy else "VENTA"

    texto = (
        f"🚨 <b>CIERRE PREVENTIVO — {nombre}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{accion}  ·  {pnl:.0f} pips  ·  SL era {fmt(sl)}\n\n"
        f"Cerramos antes de que toque el SL.\n"
        f"🔄 Buscando siguiente entrada..."
    )
    gif_url = _gif(config, "sl")
    if gif_url:
        return _send_gif_con_caption(config, gif_url, texto)
    return _send_text(config, texto)


def _post_raw(config: dict[str, Any], method: str, payload: dict[str, Any]) -> dict | None:
    """Igual que _post pero retorna el JSON completo para extraer message_id."""
    if not _enabled(config):
        return None
    token = config["telegram"]["token"]
    url = _BASE.format(token=token, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Telegram %s falló: %s — %s", method, resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram %s excepción: %s", method, exc)
    return None


def send_signal(config: dict[str, Any], signal_dict: dict[str, Any]) -> bool:
    es_compra = signal_dict.get("signal_type", "").upper().startswith("BUY")
    gif_key   = "compra" if es_compra else "venta"
    gif_url   = _gif(config, gif_key)
    texto     = format_signal_message(signal_dict)

    if gif_url:
        return _send_gif_con_caption(config, gif_url, texto)
    return _send_text(config, texto)


def send_signal_tracked(config: dict[str, Any], signal_dict: dict[str, Any]) -> int | None:
    """Envía señal nueva y retorna el message_id para threading de seguimiento."""
    if not _enabled(config):
        return None
    es_compra = signal_dict.get("signal_type", "").upper().startswith("BUY")
    gif_key   = "compra" if es_compra else "venta"
    gif_url   = _gif(config, gif_key)
    texto     = format_signal_message(signal_dict)
    chat_id   = config["telegram"]["chat_id"]

    data = None
    if gif_url:
        if len(texto) <= 900:
            data = _post_raw(config, "sendAnimation", {
                "chat_id": chat_id,
                "animation": gif_url,
                "caption": texto,
                "parse_mode": "HTML",
            })
            if not data:
                data = _post_raw(config, "sendMessage", {
                    "chat_id": chat_id,
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
        else:
            _post_raw(config, "sendAnimation", {"chat_id": chat_id, "animation": gif_url})
            data = _post_raw(config, "sendMessage", {
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
    else:
        data = _post_raw(config, "sendMessage", {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    if data and data.get("ok"):
        return int(data["result"]["message_id"])
    return None


def send_news_impulse_signal(
    config: dict[str, Any],
    signal: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    """Señal especial de impulso por noticia macro — formato llamativo."""
    symbol    = signal.get("symbol", "")
    sig_type  = signal.get("signal_type", "").upper()
    es_compra = sig_type.startswith("BUY")
    entry     = float(signal.get("entry", 0))
    tp1       = float(signal.get("tp1", 0))
    tp2       = float(signal.get("tp2", 0))
    tp3       = float(signal.get("tp3", 0))
    sl        = float(signal.get("sl", 0))
    nombre    = _NOMBRES.get(symbol.upper(), symbol)
    digits    = 2 if "XAU" in symbol.upper() else 5
    fmt       = lambda v: f"{v:.{digits}f}"
    hora_mx   = _fmt_mx_short()

    pips_tp1 = _pips(entry, tp1, symbol)
    pips_tp2 = _pips(entry, tp2, symbol)
    pips_tp3 = _pips(entry, tp3, symbol)
    pips_sl  = _pips(entry, sl,  symbol)
    rr       = round(pips_tp3 / pips_sl, 1) if pips_sl > 0 else 0

    titulo_evento = str(event.get("title", "Dato macro"))
    actual   = str(event.get("actual",   ""))
    forecast = str(event.get("forecast", "?"))
    moneda   = str(event.get("currency", ""))
    flecha   = "📈" if es_compra else "📉"
    impulso  = "ALCISTA" if es_compra else "BAJISTA"

    if es_compra:
        header = f"📰⚡ <b>NEWS IMPULSE — COMPRA {nombre}</b> ⚡📰"
    else:
        header = f"📰⚡ <b>NEWS IMPULSE — VENTA {nombre}</b> ⚡📰"

    texto = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡⚡ <b>SCALP</b>  ·  M5  ·  {hora_mx}\n\n"
        f"🗞 <b>{titulo_evento}</b>\n"
        f"   {moneda}: Actual <b>{actual}</b>  vs  Esperado <b>{forecast}</b>\n"
        f"   {flecha} Impulso <b>{impulso}</b>\n\n"
        f"📍 <b>ENTRADA:  <code>{fmt(entry)}</code></b>\n\n"
        f"🎯 TP1  <code>{fmt(tp1)}</code>  <b>+{pips_tp1:.0f}p</b>\n"
        f"🎯 TP2  <code>{fmt(tp2)}</code>  <b>+{pips_tp2:.0f}p</b>\n"
        f"🎯 TP3  <code>{fmt(tp3)}</code>  <b>+{pips_tp3:.0f}p</b>  🏆\n"
        f"🛑 SL   <code>{fmt(sl)}</code>  <b>−{pips_sl:.0f}p</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RR 1:{rr}  ·  Válida ~15 min  ·  Entra RÁPIDO"
    )

    gif_url = _gif(config, "noticia")
    if gif_url:
        return _send_gif_con_caption(config, gif_url, texto)
    return _send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Aviso de noticia próxima
# ─────────────────────────────────────────────────────────────────

def send_news_event_alert(
    config: dict[str, Any],
    event_dict: dict[str, Any],
    minutes_until: int,
) -> bool:
    titulo = str(event_dict.get("title", "Evento económico"))
    moneda = str(event_dict.get("currency", "USD"))
    impacto = str(event_dict.get("impact", "HIGH"))

    try:
        ev_dt_utc = datetime.fromisoformat(str(event_dict.get("event_time", "")))
        hora_mx = _fmt_mx_short(ev_dt_utc)
    except Exception:
        hora_mx = ""

    if minutes_until <= 12:
        header = "🚨 <b>¡NOTICIAS EN 10 MINUTOS!</b> 🚨"
        accion = (
            "🛑 <b>ACCIÓN INMEDIATA:</b>\n\n"
            "   ❌ <b>NO entres al mercado ahora</b>\n"
            "   🔒 Revisa que tu Stop Loss esté activado\n"
            "   💡 Si no tienes Stop Loss puesto →\n"
            "       cierra la operación y espera"
        )
    elif minutes_until <= 35:
        header = "⚠️ <b>NOTICIAS IMPORTANTES EN 30 MINUTOS</b>"
        accion = (
            "⚡ <b>¿QUÉ HACER?</b>\n\n"
            "   🔴 <b>NO abras nuevas operaciones</b> todavía\n"
            "   🔒 Si tienes operaciones abiertas:\n"
            "       verifica tu <b>Stop Loss</b>\n"
            "   ⏳ Espera que pase el dato y el mercado se calme"
        )
    else:
        header = "📅 <b>NOTICIA IMPORTANTE EN 1 HORA</b>"
        accion = (
            "💡 <b>Prepárate:</b>\n\n"
            "   → Revisa tus operaciones abiertas\n"
            "   → Confirma que todas tengan Stop Loss\n"
            "   → No abras operaciones muy grandes esta hora"
        )

    hora_linea = f"🕐 Hora del evento: <b>{hora_mx}</b>\n" if hora_mx else ""
    texto = (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📰 Evento: <b>{titulo}</b>\n"
        f"💱 Moneda afectada: <b>{moneda}</b>\n"
        f"🔥 Impacto: <b>{impacto}</b>\n"
        f"{hora_linea}"
        f"⏱ Tiempo restante: <b>{minutes_until} min</b>\n\n"
        f"{accion}\n\n"
        "<i>Las noticias de alto impacto pueden mover el mercado\n"
        "de forma brusca en segundos.</i>"
    )
    return _send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Apertura de sesión
# ─────────────────────────────────────────────────────────────────

def send_session_open(
    config: dict[str, Any],
    session_name: str,
    session_info: dict[str, Any],
) -> bool:
    texto = str(session_info.get("text", "")) or f"Apertura de {session_name}"
    gif_key = str(session_info.get("gif", ""))
    if gif_key:
        gif_url = _gif(config, gif_key)
        if gif_url:
            return _send_gif_con_caption(config, gif_url, texto)
    return _send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Actualizaciones de TP
# ─────────────────────────────────────────────────────────────────

def send_tp_update(
    config: dict[str, Any],
    signal: dict[str, Any],
    tp_level: str,
    pnl_pips: float,
    hold_rec: bool = False,
    hold_reason: str = "",
    reply_to_message_id: int | None = None,
) -> bool:
    symbol    = signal.get("symbol", "")
    tf        = signal.get("timeframe", "")
    sig_type  = signal.get("signal_type", "")
    entry     = float(signal.get("entry", 0))
    tp_price  = float(signal.get(tp_level.lower(), 0))
    sl        = float(signal.get("sl", 0))
    tp3_price = float(signal.get("tp3", 0))
    tp2_price = float(signal.get("tp2", 0))
    es_compra = "BUY" in sig_type.upper()
    dir_texto = "COMPRA" if es_compra else "VENTA"
    nombre    = _NOMBRES.get(symbol.upper(), symbol)

    pips_ganados = _pips(entry, tp_price, symbol)
    pips_sl      = _pips(entry, sl, symbol)
    pips_tp2     = _pips(entry, tp2_price, symbol)
    pips_tp3     = _pips(entry, tp3_price, symbol)
    rr_total     = round(pips_tp3 / pips_sl, 1) if pips_sl > 0 else 0

    if tp_level == "TP1":
        gif_key = "tp1"
        if hold_rec:
            guia = (
                f"🔥 <b>MOMENTUM FUERTE — AGUANTA POSICIÓN</b>\n"
                f"Mueve SL a entrada <code>{entry}</code>  →  riesgo CERO\n"
                f"TP3 en la mira: <code>{signal.get('tp3')}</code>  (+{pips_tp3:.0f}p)"
            )
        else:
            guia = (
                f"→ Cierra <b>1/3</b> ahora\n"
                f"→ Mueve SL a entrada <code>{entry}</code>\n"
                f"→ Próximo objetivo: <code>{signal.get('tp2')}</code>  (+{pips_tp2:.0f}p)"
            )
        texto = (
            f"✅ <b>TP1 — {nombre}  +{pips_ganados:.0f} pips</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{guia}"
        )

    elif tp_level == "TP2":
        gif_key = "tp2"
        if hold_rec:
            guia = (
                f"🚀 <b>AGUANTA EL RESTO</b>\n"
                f"Riesgo CERO — TP3: <code>{signal.get('tp3')}</code>  (+{pips_tp3:.0f}p)"
            )
        else:
            guia = (
                f"→ Cierra otro <b>1/3</b>\n"
                f"→ Deja el 34% hasta TP3: <code>{signal.get('tp3')}</code>  (+{pips_tp3:.0f}p)\n"
                f"→ Riesgo CERO"
            )
        texto = (
            f"💰 <b>TP2 — {nombre}  +{pips_ganados:.0f} pips</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{guia}"
        )

    else:  # TP3
        gif_key = "tp3"
        texto = (
            f"🏆 <b>WIN — {nombre}  +{pips_tp3:.0f} pips</b> 🏆\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"RR 1:{rr_total}  ·  {dir_texto}  ·  {tf}\n\n"
            f"Señal completada al 100% ✅\n"
            f"🔍 Buscando siguiente entrada..."
        )

    gif_url = _gif(config, gif_key)
    chat_id = config["telegram"]["chat_id"]
    if gif_url:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "animation": gif_url,
            "caption": texto,
            "parse_mode": "HTML",
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        data = _post_raw(config, "sendAnimation", payload)
        if data and data.get("ok"):
            return True
        # fallback texto plano
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    data = _post_raw(config, "sendMessage", payload)
    return bool(data and data.get("ok"))


# ─────────────────────────────────────────────────────────────────
# Stop Loss
# ─────────────────────────────────────────────────────────────────

def send_sl_update(
    config: dict[str, Any],
    signal: dict[str, Any],
    pnl_pips: float,
    reply_to_message_id: int | None = None,
) -> bool:
    symbol    = signal.get("symbol", "")
    tf        = signal.get("timeframe", "")
    sig_type  = signal.get("signal_type", "")
    entry     = float(signal.get("entry", 0))
    sl        = float(signal.get("sl", 0))
    es_compra = "BUY" in sig_type.upper()
    dir_texto = "COMPRA" if es_compra else "VENTA"
    nombre    = _NOMBRES.get(symbol.upper(), symbol)
    pips_risk = _pips(entry, sl, symbol)

    texto = (
        f"❌ <b>STOP LOSS — {nombre}  −{pips_risk:.0f} pips</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_texto}  ·  {tf}\n\n"
        f"Operación cerrada. El SL es tu seguro 💪\n"
        f"🔍 Buscando siguiente entrada..."
    )

    gif_url = _gif(config, "sl")
    chat_id = config["telegram"]["chat_id"]
    if gif_url:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "animation": gif_url,
            "caption": texto,
            "parse_mode": "HTML",
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        data = _post_raw(config, "sendAnimation", payload)
        if data and data.get("ok"):
            return True
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    data = _post_raw(config, "sendMessage", payload)
    return bool(data and data.get("ok"))


# ─────────────────────────────────────────────────────────────────
# Alertas y reportes
# ─────────────────────────────────────────────────────────────────

def send_alert(config: dict[str, Any], message: str) -> bool:
    return _send_text(config, message)


def send_daily_report(config: dict[str, Any], stats: dict[str, Any]) -> bool:
    wins  = stats.get('wins', 0)
    losses = stats.get('losses', 0)
    pips  = float(stats.get('total_pips', 0))
    wr    = float(stats.get('win_rate', 0))

    emoji_resultado = "💰" if pips >= 0 else "📉"
    emoji_wr = "🔥" if wr >= 60 else ("✅" if wr >= 50 else "⚠️")

    texto = (
        f"📊 <b>REPORTE DEL DÍA — SCANNER PRO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Fecha: {stats.get('date', '')}\n"
        f"\n"
        f"Señales generadas hoy: <b>{stats.get('total_signals', 0)}</b>\n"
        f"✅ Ganadoras: <b>{wins}</b>\n"
        f"❌ Stop Loss: <b>{losses}</b>\n"
        f"➖ Breakeven: <b>{stats.get('breakeven', 0)}</b>\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji_wr} <b>Win Rate: {wr:.1f}%</b>\n"
        f"{emoji_resultado} <b>Pips netos del día: {pips:+.1f} pips</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Pulso Capital MX — Scanner PRO</i>"
    )
    return _send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────────────────────────

def test_connection(config: dict[str, Any]) -> bool:
    if not _enabled(config):
        return False
    token = config["telegram"]["token"]
    url = _BASE.format(token=token, method="getMe")
    try:
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
