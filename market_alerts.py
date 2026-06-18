"""
Alertas de mercado para el grupo Pulso Capital MX.
Detecta: aperturas de sesión, noticias importantes, volatilidad,
sentimiento de mercado, briefing matutino y resumen semanal.
Todo en español sencillo para traders principiantes en México.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import telegram_bot as tg

logger = logging.getLogger(__name__)

_MX_OFFSET = timezone(timedelta(hours=-6))

_DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


# ─────────────────────────────────────────────────────────────────
# Anti-spam: registro de cuándo se envió cada tipo de alerta
# ─────────────────────────────────────────────────────────────────
_ultima_alerta: dict[str, float] = {}

_COOLDOWN: dict[str, int] = {
    "noticia_60min":     50 * 60,
    "noticia_30min":     25 * 60,
    "noticia_10min":     8 * 60,
    "noticia_pasada":    45 * 60,
    "volatilidad_alta":  20 * 60,
    "movimiento_brusco": 20 * 60,
    "miedo_extremo":     60 * 60,
    "codicia_extrema":   60 * 60,
    "vix_alto":          90 * 60,
    "briefing_matutino": 23 * 60 * 60,
    "preview_semanal":   6 * 24 * 60 * 60,
    "sesion_asia":       23 * 60 * 60,
    "sesion_london":     23 * 60 * 60,
    "sesion_ny":         23 * 60 * 60,
    "sesion_london_close": 23 * 60 * 60,
    "sesion_ny_close":   23 * 60 * 60,
}


def _puede_enviar(clave: str) -> bool:
    ahora = time.time()
    cooldown = _COOLDOWN.get(clave, 1800)
    ultimo = _ultima_alerta.get(clave, 0)
    return (ahora - ultimo) >= cooldown


def _registrar(clave: str) -> None:
    _ultima_alerta[clave] = time.time()


def _gif_alerta(config: dict[str, Any], clave: str) -> str:
    return config.get("gifs", {}).get(clave, "")


def _enviar_alerta(config: dict[str, Any], texto: str, gif_key: str = "") -> None:
    gif_url = _gif_alerta(config, gif_key) if gif_key else ""
    if gif_url:
        tg._send_gif_con_caption(config, gif_url, texto)
    else:
        tg._send_text(config, texto)


# ─────────────────────────────────────────────────────────────────
# Helpers de tiempo (México UTC-6, sin DST)
# ─────────────────────────────────────────────────────────────────

def _mx_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(_MX_OFFSET)


def _mx_from_utc(utc_dt: datetime) -> datetime:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_MX_OFFSET)


def _fmt_fecha_es(dt: datetime) -> str:
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]}"


def _fmt_hora_mx(utc_dt: datetime) -> str:
    return _mx_from_utc(utc_dt).strftime("%H:%M")


# ─────────────────────────────────────────────────────────────────
# Detección de sesión actual
# ─────────────────────────────────────────────────────────────────

def _es_fin_de_semana() -> bool:
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    if wd == 5:
        return True
    if wd == 6:
        return now.hour < 22
    if wd == 4 and now.hour >= 22:
        return True
    return False


def sesion_actual() -> str:
    now = datetime.now(timezone.utc)
    h = now.hour
    if _es_fin_de_semana():
        return "Cerrado (fin de semana)"
    if 13 <= h < 17:
        return "Overlap Londres + Nueva York"
    if 8 <= h < 13:
        return "Londres"
    if 17 <= h < 22:
        return "Nueva York"
    if h < 9 or h >= 22:
        return "Asia / Sídney"
    return "Transición"


# ─────────────────────────────────────────────────────────────────
# 1. Alertas de apertura de sesión
# ─────────────────────────────────────────────────────────────────

def _eventos_hoy_alto_impacto(news_module: Any) -> list[dict[str, Any]]:
    try:
        if hasattr(news_module, "get_upcoming_events_today"):
            agrupado = news_module.get_upcoming_events_today()
            todos: list[dict[str, Any]] = []
            for evs in agrupado.values():
                todos.extend(evs)
            return [e for e in todos if e.get("impact") == "HIGH"]
        return list(news_module.get_high_impact_events(hours_ahead=24))
    except Exception:
        return []


def _formato_eventos_lista(eventos: list[dict[str, Any]], limite: int = 6) -> str:
    if not eventos:
        return "   • Sin eventos de alto impacto programados\n"
    eventos = sorted(eventos, key=lambda e: e.get("event_time", ""))[:limite]
    lineas: list[str] = []
    for e in eventos:
        try:
            t = datetime.fromisoformat(str(e["event_time"]))
            hora = _fmt_hora_mx(t)
        except Exception:
            hora = "--:--"
        cur = e.get("currency", "")
        titulo = str(e.get("title", ""))[:55]
        lineas.append(f"   • <b>{hora}</b> CDMX  [{cur}]  {titulo}")
    return "\n".join(lineas) + "\n"


def verificar_sesion(config: dict[str, Any], news_module: Any) -> None:
    """Avisa cuando abren/cierran las sesiones clave."""
    if _es_fin_de_semana():
        return

    ahora = datetime.now(timezone.utc)
    hora = ahora.hour
    minuto = ahora.minute

    if not (0 <= minuto < 5):
        return

    if hora == 0:
        clave = "sesion_asia"
        if _puede_enviar(clave):
            texto = (
                "🌏 <b>ABRE LA SESIÓN DE ASIA / SÍDNEY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 <b>Horario:</b> 18:00 (anoche) — 03:00 CDMX\n"
                "📊 Volumen <b>bajo</b>, movimientos lentos.\n\n"
                "💡 <b>¿Qué esperar?</b>\n"
                "   • Movimientos pequeños en ORO y EUR\n"
                "   • Mejor para vigilar, no para entrar a lo loco\n"
                "   • El gran movimiento empieza con Londres (2:00 AM CDMX)\n\n"
                "🔍 El scanner sigue monitoreando."
            )
            _enviar_alerta(config, texto)
            _registrar(clave)

    elif hora == 8:
        clave = "sesion_london"
        if _puede_enviar(clave):
            eventos = _eventos_hoy_alto_impacto(news_module)
            lista_ev = _formato_eventos_lista(eventos, limite=6)
            texto = (
                "🇬🇧 <b>¡ABRE LA SESIÓN DE LONDRES!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 <b>Horario:</b> 02:00 — 11:00 CDMX\n"
                "📈 Sesión <b>de alta liquidez</b>. Empieza el movimiento real.\n\n"
                "💡 <b>¿Qué esperar?</b>\n"
                "   • ORO y EURO suelen <b>moverse fuerte</b>\n"
                "   • Spreads bajan = costos de operar más baratos\n"
                "   • Las señales del scanner son más confiables\n\n"
                "📰 <b>Eventos económicos clave de hoy:</b>\n"
                f"{lista_ev}\n"
                "🔍 <b>Scanner: monitoreando activamente</b>.\n"
                "Las señales llegarán cuando el análisis confirme una entrada."
            )
            _enviar_alerta(config, texto, "volatilidad")
            _registrar(clave)

    elif hora == 13:
        clave = "sesion_ny"
        if _puede_enviar(clave):
            eventos = _eventos_hoy_alto_impacto(news_module)
            eventos_usd = [e for e in eventos if e.get("currency") == "USD"]
            lista_ev = _formato_eventos_lista(eventos_usd, limite=6)
            texto = (
                "🇺🇸 <b>¡ABRE NUEVA YORK — OVERLAP CON LONDRES!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 <b>Horario NY:</b> 07:00 — 16:00 CDMX\n"
                "🔥 <b>Overlap London+NY: 07:00 — 11:00 CDMX</b>\n"
                "Este es el <b>MEJOR momento del día</b> para operar.\n\n"
                "💡 <b>¿Qué esperar?</b>\n"
                "   • Máximo volumen y volatilidad del día\n"
                "   • Datos económicos de EEUU se publican ahora\n"
                "   • <b>ORO</b> se correlaciona inversamente con <b>DXY</b>\n"
                "     (si el dólar sube, el oro suele bajar)\n\n"
                "📰 <b>Datos USD de hoy:</b>\n"
                f"{lista_ev}\n"
                "🔍 Scanner en modo activo. Estate pendiente."
            )
            _enviar_alerta(config, texto, "volatilidad")
            _registrar(clave)

    elif hora == 17:
        clave = "sesion_london_close"
        if _puede_enviar(clave):
            texto = (
                "🌆 <b>CIERRA LONDRES — Sigue Nueva York</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 <b>Hora:</b> 11:00 CDMX\n\n"
                "💡 El volumen baja, pero NY sigue activa hasta las 16:00 CDMX.\n"
                "   • Esperar reversiones menores tras el cierre europeo\n"
                "   • Si tienes operaciones abiertas: ajusta tu SL\n\n"
                "🔍 Scanner sigue monitoreando."
            )
            _enviar_alerta(config, texto)
            _registrar(clave)

    elif hora == 22:
        clave = "sesion_ny_close"
        if _puede_enviar(clave):
            texto = (
                "🌙 <b>CIERRE DE NUEVA YORK — FIN DEL DÍA OPERATIVO</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 <b>Hora:</b> 16:00 CDMX\n\n"
                "Las principales sesiones del día cerraron.\n"
                "Entramos a la fase de <b>baja liquidez</b>.\n\n"
                "💡 <b>Recomendación:</b>\n"
                "   ⚠️ Evita abrir nuevas operaciones esta noche\n"
                "   🔒 Si tienes operaciones abiertas: revisa tu Stop Loss\n"
                "   😴 Asia abre a las 18:00 CDMX (poco movimiento)\n\n"
                "✅ Scanner sigue activo. Buenas noches."
            )
            _enviar_alerta(config, texto)
            _registrar(clave)


# ─────────────────────────────────────────────────────────────────
# 2. Alertas de noticias importantes (60/30/10 min)
# ─────────────────────────────────────────────────────────────────

def verificar_noticias(config: dict[str, Any], news_module: Any) -> None:
    try:
        eventos = news_module.get_high_impact_events(hours_ahead=2)
    except Exception:
        return

    for evento in eventos:
        minutos = int(evento.get("minutes_until", 999))

        if 55 <= minutos <= 65 and _puede_enviar("noticia_60min"):
            try:
                tg.send_news_event_alert(config, evento, minutos)
            except Exception:
                continue
            _registrar("noticia_60min")

        elif 25 <= minutos <= 35 and _puede_enviar("noticia_30min"):
            try:
                tg.send_news_event_alert(config, evento, minutos)
            except Exception:
                continue
            _registrar("noticia_30min")

        elif 5 <= minutos <= 12 and _puede_enviar("noticia_10min"):
            try:
                tg.send_news_event_alert(config, evento, minutos)
            except Exception:
                continue
            _registrar("noticia_10min")


# ─────────────────────────────────────────────────────────────────
# 3. Alertas de volatilidad alta (basada en movimiento de precio)
# ─────────────────────────────────────────────────────────────────

def verificar_volatilidad(
    config: dict[str, Any],
    broker_module: Any,
    symbols: list[str],
) -> None:
    if _es_fin_de_semana():
        return

    for symbol in symbols:
        try:
            df = broker_module.get_ohlcv(symbol, "M5", 20)
            if df is None or df.empty or len(df) < 10:
                continue

            highs  = df["high"].values
            lows   = df["low"].values
            closes = df["close"].values

            def atr_n(n: int) -> float:
                trs = []
                for i in range(1, min(n + 1, len(closes))):
                    tr = max(
                        highs[-i] - lows[-i],
                        abs(highs[-i] - closes[-i - 1]),
                        abs(lows[-i] - closes[-i - 1]),
                    )
                    trs.append(tr)
                return sum(trs) / len(trs) if trs else 0

            atr_reciente = atr_n(3)
            atr_normal   = atr_n(15)

            if atr_normal <= 0:
                continue

            ratio = atr_reciente / atr_normal
            nombre = {"XAUUSD": "el ORO", "EURUSD": "el EURO/DÓLAR"}.get(
                symbol.upper(), symbol
            )

            pip_size = 0.1 if "XAU" in symbol.upper() else 0.0001
            rango_15min = (max(highs[-3:]) - min(lows[-3:])) / pip_size

            if ratio >= 2.5 and _puede_enviar("volatilidad_alta"):
                texto = (
                    f"🌋 <b>¡MERCADO MUY VOLÁTIL AHORA!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<b>{nombre.upper()}</b> se mueve {ratio:.1f}x más\n"
                    f"rápido de lo normal en este momento.\n\n"
                    f"📊 Rango últimos 15 min: <b>{rango_15min:.0f} pips</b>\n\n"
                    "⚡ <b>¿QUÉ HACER?</b>\n\n"
                    "   🔒 Verifica que tu <b>Stop Loss esté puesto</b>\n"
                    "   ⚠️ <b>Reduce el tamaño de tus operaciones</b>\n"
                    "   🛑 Si no tienes experiencia:\n"
                    "       <b>espera a que el mercado se calme</b>\n\n"
                    "<i>Alta volatilidad = mayor ganancia posible\n"
                    "pero también mayor pérdida. Sé cuidadoso.</i>"
                )
                _enviar_alerta(config, texto, "volatilidad")
                _registrar("volatilidad_alta")

            elif ratio >= 1.8 and _puede_enviar("movimiento_brusco"):
                texto = (
                    f"⚡ <b>MOVIMIENTO INUSUAL EN {nombre.upper()}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"El precio se movió <b>{rango_15min:.0f} pips</b> en 15 min.\n"
                    f"Eso es <b>{ratio:.1f}x más de lo normal</b>.\n\n"
                    "💡 <b>Recomendación:</b>\n"
                    "   → Si tienes posiciones abiertas: revisa tus Stop Loss\n"
                    "   → Antes de entrar: espera a que el precio se estabilice"
                )
                _enviar_alerta(config, texto)
                _registrar("movimiento_brusco")

        except Exception as exc:
            logger.debug("Error verificar_volatilidad %s: %s", symbol, exc)


# ─────────────────────────────────────────────────────────────────
# 4. Alertas de sentimiento (Fear & Greed)
# ─────────────────────────────────────────────────────────────────

def verificar_sentimiento(config: dict[str, Any], feed_aggregator: Any) -> None:
    if feed_aggregator is None:
        return
    try:
        from data_feeds import fetch_fear_greed
        fng = fetch_fear_greed()
        if fng is None:
            return

        valor    = int(fng.get("value", 50))
        anterior = int(fng.get("previous_close", 50))
        cambio   = valor - anterior

        if valor <= 20 and _puede_enviar("miedo_extremo"):
            texto = (
                "😱 <b>MIEDO EXTREMO EN EL MERCADO</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"El índice Fear & Greed está en <b>{valor}/100</b>.\n"
                "Casi todos están vendiendo con pánico.\n\n"
                "💡 <b>¿Qué significa?</b>\n\n"
                "   📉 Los precios pueden caer MÁS antes de recuperarse\n"
                "   📈 Históricamente, miedo extremo es oportunidad de compra\n"
                "       (pero con riesgo)\n\n"
                "⚠️ <b>Recomendación:</b>\n"
                "   → <b>Reduce el tamaño</b> de tus operaciones hoy\n"
                "   → Espera confirmación antes de entrar\n"
                "   → Nunca operes sin Stop Loss en días así"
            )
            _enviar_alerta(config, texto)
            _registrar("miedo_extremo")

        elif valor >= 80 and _puede_enviar("codicia_extrema"):
            texto = (
                "🤑 <b>CODICIA EXTREMA EN EL MERCADO</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"El índice Fear & Greed está en <b>{valor}/100</b>.\n"
                "Casi todos compran eufóricamente.\n\n"
                "💡 <b>¿Qué significa?</b>\n\n"
                "   ⚠️ Mercados sobrecomprados pueden corregir bruscamente\n"
                "   🔴 Mayor riesgo de reversión del precio\n\n"
                "⚠️ <b>Recomendación:</b>\n"
                "   → <b>No te dejes llevar por la euforia</b>\n"
                "   → Apega tu operativa a tu Stop Loss\n"
                "   → Reduce el tamaño si vas a operar hoy"
            )
            _enviar_alerta(config, texto)
            _registrar("codicia_extrema")

        elif abs(cambio) >= 20 and _puede_enviar("movimiento_brusco"):
            dir_cambio = "subió" if cambio > 0 else "bajó"
            emocion = "más codicia" if cambio > 0 else "más miedo"
            texto = (
                f"📊 <b>CAMBIO BRUSCO EN EL SENTIMIENTO</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"El Fear & Greed {dir_cambio} <b>{abs(cambio)} puntos</b> hoy.\n"
                f"El mercado pasó a tener <b>{emocion}</b>.\n\n"
                "💡 Los cambios bruscos de sentimiento\n"
                "pueden generar movimientos inesperados.\n\n"
                "   → Mantén tus Stop Loss activos\n"
                "   → Espera confirmación antes de entrar"
            )
            _enviar_alerta(config, texto)
            _registrar("movimiento_brusco")

    except Exception as exc:
        logger.debug("Error verificar_sentimiento: %s", exc)


# ─────────────────────────────────────────────────────────────────
# 5. Briefing matutino (07:30 UTC = 01:30 CDMX)
# ─────────────────────────────────────────────────────────────────

def verificar_briefing_matutino(
    config: dict[str, Any],
    news_module: Any,
) -> None:
    if _es_fin_de_semana():
        return

    now = datetime.now(timezone.utc)
    if not (now.hour == 7 and 30 <= now.minute < 35):
        return
    if not _puede_enviar("briefing_matutino"):
        return

    mx = _mx_now()
    fecha = _fmt_fecha_es(mx)

    eventos = _eventos_hoy_alto_impacto(news_module)
    lista_ev = _formato_eventos_lista(eventos, limite=8)

    if eventos:
        recomendacion = (
            "⚠️ Día con datos importantes — opera con cautela cerca de cada noticia.\n"
            "Reduce tamaño 30–60 min antes de cada evento HIGH."
        )
    else:
        recomendacion = (
            "✅ Sin noticias HIGH programadas hoy.\n"
            "Día técnico — las señales del scanner deberían funcionar normal."
        )

    texto = (
        "☀️ <b>BRIEFING MATUTINO — PULSO CAPITAL MX</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 <b>{fecha}</b>\n"
        f"🕐 {mx.strftime('%H:%M')} CDMX\n\n"
        "⏰ <b>Aperturas de sesión hoy (CDMX):</b>\n"
        "   • 🌏 Asia: ya en curso (cierra 03:00)\n"
        "   • 🇬🇧 Londres: <b>02:00 — 11:00</b>\n"
        "   • 🇺🇸 Nueva York: <b>07:00 — 16:00</b>\n"
        "   • 🔥 Overlap London+NY: <b>07:00 — 11:00</b>  (mejor momento)\n\n"
        "📰 <b>Eventos económicos HIGH de hoy:</b>\n"
        f"{lista_ev}\n"
        f"💡 <b>Recomendación general:</b>\n{recomendacion}\n\n"
        "🔍 Scanner activo todo el día.\n"
        "<i>Pulso Capital MX 🇲🇽</i>"
    )
    _enviar_alerta(config, texto)
    _registrar("briefing_matutino")


# ─────────────────────────────────────────────────────────────────
# 6. Preview semanal (Lunes 07:00 UTC = 01:00 CDMX)
# ─────────────────────────────────────────────────────────────────

def verificar_preview_semanal(
    config: dict[str, Any],
    news_module: Any,
) -> None:
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:
        return
    if not (now.hour == 7 and now.minute < 5):
        return
    if not _puede_enviar("preview_semanal"):
        return

    try:
        if hasattr(news_module, "get_all_week_events"):
            todos = news_module.get_all_week_events()
        else:
            todos = news_module.get_high_impact_events(hours_ahead=24 * 7)
    except Exception:
        todos = []

    alto = [e for e in todos if e.get("impact") == "HIGH"]
    alto.sort(key=lambda e: e.get("event_time", ""))

    lineas: list[str] = []
    if not alto:
        lineas.append("   • Sin eventos HIGH detectados esta semana")
    else:
        for e in alto[:25]:
            try:
                t = datetime.fromisoformat(str(e["event_time"]))
                mx = _mx_from_utc(t)
                dia = _DIAS_ES[mx.weekday()][:3]
                hora = mx.strftime("%H:%M")
                cur = e.get("currency", "")
                titulo = str(e.get("title", ""))[:50]
                lineas.append(f"   • <b>{dia}</b> {hora} CDMX [{cur}] {titulo}")
            except Exception:
                continue

    cuerpo = "\n".join(lineas)

    texto = (
        "📆 <b>RESUMEN SEMANAL — PULSO CAPITAL MX</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Eventos HIGH detectados: <b>{len(alto)}</b>\n\n"
        f"{cuerpo}\n\n"
        "💡 <b>Cómo usar este calendario:</b>\n"
        "   • 60 min antes de cada evento: prepara tus posiciones\n"
        "   • 30 min antes: no abras nuevas operaciones\n"
        "   • 10 min antes: cierra o protege con SL ajustado\n"
        "   • 15 min después: evalúa la reacción antes de entrar\n\n"
        "🔍 Recibirás avisos automáticos antes de cada noticia.\n"
        "<i>Pulso Capital MX 🇲🇽</i>"
    )
    _enviar_alerta(config, texto)
    _registrar("preview_semanal")


# ─────────────────────────────────────────────────────────────────
# 7. Función principal
# ─────────────────────────────────────────────────────────────────

def run_all_checks(
    config: dict[str, Any],
    broker_module: Any,
    news_module: Any,
    feed_aggregator: Any,
    symbols: list[str] | None = None,
) -> None:
    """
    Ejecuta todas las verificaciones de alertas.
    Se llama desde alerts_loop en main.py cada 5 minutos.
    """
    if symbols is None:
        symbols = ["XAUUSD", "EURUSD"]

    if _es_fin_de_semana():
        logger.debug("Fin de semana: solo briefing/preview se evalúan")
        try:
            verificar_preview_semanal(config, news_module)
        except Exception as exc:
            logger.debug("preview_semanal error: %s", exc)
        return

    try:
        verificar_sesion(config, news_module)
    except Exception as exc:
        logger.debug("verificar_sesion error: %s", exc)

    try:
        verificar_noticias(config, news_module)
    except Exception as exc:
        logger.debug("verificar_noticias error: %s", exc)

    try:
        verificar_volatilidad(config, broker_module, symbols)
    except Exception as exc:
        logger.debug("verificar_volatilidad error: %s", exc)

    try:
        verificar_sentimiento(config, feed_aggregator)
    except Exception as exc:
        logger.debug("verificar_sentimiento error: %s", exc)

    try:
        verificar_briefing_matutino(config, news_module)
    except Exception as exc:
        logger.debug("verificar_briefing_matutino error: %s", exc)

    try:
        verificar_preview_semanal(config, news_module)
    except Exception as exc:
        logger.debug("verificar_preview_semanal error: %s", exc)
