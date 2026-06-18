"""
Manejador de comandos Telegram — Pulso Capital MX.
Los administradores pueden solicitar señales por temporalidad,
tipo de entrada, forzar escaneos y consultar estadísticas.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

# IDs de admins permanentes (nunca se eliminan aunque no sean admin del grupo)
_ADMINS_FIJOS = {6409245507}  # @RavenSuport


class TelegramCommandHandler:
    def __init__(
        self,
        config: dict[str, Any],
        db_module: Any,
        scanner_getter: Callable[[], Any | None],
    ) -> None:
        self.config = config
        self.db = db_module
        self._get_scanner = scanner_getter
        self._token = config.get("telegram", {}).get("token", "")
        self._chat_id = str(config.get("telegram", {}).get("chat_id", ""))
        self._offset = 0
        # caché de admins: chat_id → (set de user_ids, timestamp)
        self._admin_cache: dict[str, tuple[set[int], float]] = {}

    # ─────────────────────────────────────────────────────────────
    # Utilidades API
    # ─────────────────────────────────────────────────────────────

    def _api(self, method: str, payload: dict | None = None, timeout: int = 10) -> dict | None:
        if not self._token:
            return None
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            resp = requests.post(url, json=payload or {}, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("Telegram API %s error: %s", method, exc)
        return None

    def _send(self, texto: str, chat_id: str | None = None) -> None:
        cid = chat_id or self._chat_id
        self._api("sendMessage", {
            "chat_id": cid,
            "text": texto,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        })

    # ─────────────────────────────────────────────────────────────
    # Verificación de administradores
    # ─────────────────────────────────────────────────────────────

    def _obtener_admins(self, chat_id: str) -> set[int]:
        ahora = time.time()
        cached = self._admin_cache.get(chat_id)
        if cached and (ahora - cached[1]) < 300:  # caché 5 minutos
            return cached[0]
        resultado = self._api("getChatAdministrators", {"chat_id": chat_id})
        if resultado and resultado.get("ok"):
            ids = {m["user"]["id"] for m in resultado.get("result", [])}
        else:
            ids = set()
        ids |= _ADMINS_FIJOS
        self._admin_cache[chat_id] = (ids, ahora)
        return ids

    def es_admin(self, user_id: int, chat_id: str) -> bool:
        if user_id in _ADMINS_FIJOS:
            return True
        return user_id in self._obtener_admins(chat_id)

    # ─────────────────────────────────────────────────────────────
    # Polling de actualizaciones
    # ─────────────────────────────────────────────────────────────

    def obtener_updates(self) -> list[dict]:
        resultado = self._api(
            "getUpdates",
            {"offset": self._offset, "timeout": 5, "limit": 50},
            timeout=12,
        )
        if not resultado or not resultado.get("ok"):
            return []
        updates = resultado.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # Estado de captura de GIFs: user_id → tipo pendiente
    _pending_gif: dict[int, str] = {}

    def procesar_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat_id = str(msg["chat"]["id"])
        user_id = int(msg["from"]["id"])
        username = msg["from"].get("username") or msg["from"].get("first_name", "Usuario")

        if chat_id != self._chat_id:
            return

        # ── Captura de GIF premium si admin está en modo setgif ──────────────
        tipo_pendiente = self._pending_gif.get(user_id)
        if tipo_pendiente and self.es_admin(user_id, chat_id):
            anim  = msg.get("animation") or msg.get("video")
            stick = msg.get("sticker")
            doc   = msg.get("document")
            file_id = None
            if anim:
                file_id = anim.get("file_id")
            elif stick:
                file_id = stick.get("file_id")
            elif doc and (doc.get("mime_type", "").startswith("video") or
                          doc.get("mime_type", "").startswith("image/gif")):
                file_id = doc.get("file_id")
            if file_id:
                try:
                    import telegram_bot as _tg
                    _tg.save_gif_id(tipo_pendiente, file_id)
                    self._send(
                        f"✅ GIF guardado para <b>{tipo_pendiente.upper()}</b>\n"
                        f"<code>{file_id[:40]}...</code>\n"
                        f"El scanner lo usará en los próximos mensajes."
                    )
                except Exception as exc:
                    self._send(f"❌ Error guardando GIF: {exc}")
                del self._pending_gif[user_id]
                return

        texto = (msg.get("text") or msg.get("caption") or "").strip()
        if not texto.startswith("/"):
            return

        partes = texto.lower().split()
        cmd = partes[0].lstrip("/").split("@")[0]
        args = partes[1:] if len(partes) > 1 else []

        logger.info("Comando /%s de @%s (id=%s)", cmd, username, user_id)

        dispatch = {
            "ayuda":        self._cmd_ayuda,
            "help":         self._cmd_ayuda,
            "start":        self._cmd_ayuda,
            "señales":      lambda: self._cmd_señales(args),
            "senales":      lambda: self._cmd_señales(args),
            "señal":        lambda: self._cmd_señales(args),
            "senal":        lambda: self._cmd_señales(args),
            "activas":      self._cmd_activas,
            "activos":      self._cmd_activas,
            "m1":           lambda: self._cmd_por_tf("M1"),
            "m5":           lambda: self._cmd_por_tf("M5"),
            "m15":          lambda: self._cmd_por_tf("M15"),
            "h1":           lambda: self._cmd_por_tf("H1"),
            "h4":           lambda: self._cmd_por_tf("H4"),
            "d1":           lambda: self._cmd_por_tf("D1"),
            "scalp":        lambda: self._cmd_por_tf_lista(["M1", "M5"], "Scalping"),
            "intraday":     lambda: self._cmd_por_tf_lista(["M15", "H1"], "Intraday"),
            "intra":        lambda: self._cmd_por_tf_lista(["M15", "H1"], "Intraday"),
            "swing":        lambda: self._cmd_por_tf_lista(["H4", "D1"], "Swing"),
            "compra":       lambda: self._cmd_por_tipo("BUY"),
            "compras":      lambda: self._cmd_por_tipo("BUY"),
            "buy":          lambda: self._cmd_por_tipo("BUY"),
            "venta":        lambda: self._cmd_por_tipo("SELL"),
            "ventas":       lambda: self._cmd_por_tipo("SELL"),
            "sell":         lambda: self._cmd_por_tipo("SELL"),
            "xauusd":       lambda: self._cmd_por_simbolo("XAUUSD"),
            "oro":          lambda: self._cmd_por_simbolo("XAUUSD"),
            "gold":         lambda: self._cmd_por_simbolo("XAUUSD"),
            "eurusd":       lambda: self._cmd_por_simbolo("EURUSD"),
            "euro":         lambda: self._cmd_por_simbolo("EURUSD"),
            "escanear":     lambda: self._cmd_escanear(user_id, chat_id, username),
            "scan":         lambda: self._cmd_escanear(user_id, chat_id, username),
            "buscar":       lambda: self._cmd_escanear(user_id, chat_id, username),
            "estado":       self._cmd_estado,
            "status":       self._cmd_estado,
            "stats":        self._cmd_stats,
            "estadisticas": self._cmd_stats,
            "rendimiento":  self._cmd_stats,
            "setgif":       lambda: self._cmd_setgif(args, user_id, chat_id, username),
            "gifs":         lambda: self._cmd_gifs_status(user_id, chat_id),
            "objetivo":     lambda: self._cmd_objetivo(args, user_id, chat_id),
            "target":       lambda: self._cmd_objetivo(args, user_id, chat_id),
            "pips":         lambda: self._cmd_objetivo(args, user_id, chat_id),
        }

        handler = dispatch.get(cmd)
        if handler:
            try:
                handler()
            except Exception as exc:
                logger.exception("Error en comando /%s: %s", cmd, exc)
                self._send("⚠️ Error procesando el comando. Intenta de nuevo.")

    # ─────────────────────────────────────────────────────────────
    # Comandos
    # ─────────────────────────────────────────────────────────────

    def _cmd_ayuda(self) -> None:
        self._send(
            "🤖 <b>SCANNER PRO — Comandos disponibles</b>\n\n"
            "<b>📊 Señales por temporalidad:</b>\n"
            "/m1  /m5  /m15  /h1  /h4  /d1\n\n"
            "<b>📦 Paquetes de temporalidad:</b>\n"
            "/scalp — M1 + M5  (operaciones rápidas)\n"
            "/intraday — M15 + H1  (día a día)\n"
            "/swing — H4 + D1  (tendencia)\n\n"
            "<b>🔎 Por tipo de entrada:</b>\n"
            "/compra — Solo señales de compra (BUY)\n"
            "/venta — Solo señales de venta (SELL)\n\n"
            "<b>💱 Por símbolo:</b>\n"
            "/xauusd  /oro — Señales de Oro\n"
            "/eurusd  /euro — Señales de EUR/USD\n\n"
            "<b>📋 Ver activas:</b>\n"
            "/señales — Todas las señales activas\n"
            "/activas — Detalle de señales en seguimiento\n\n"
            "<b>📈 Información:</b>\n"
            "/estado — Estado actual del scanner\n"
            "/stats — Estadísticas del día\n\n"
            "<b>⚡ Solo administradores:</b>\n"
            "/escanear — Forzar escaneo inmediato\n"
            "/objetivo [N] — Filtrar señales con TP3 &lt; N pips\n"
            "/setgif [tipo] — Configurar GIF premium\n"
            "/gifs — Ver GIFs configurados"
        )

    def _cmd_señales(self, args: list[str]) -> None:
        if args:
            tf_arg = args[0].upper()
            if tf_arg in ("M1", "M5", "M15", "H1", "H4", "D1"):
                self._cmd_por_tf(tf_arg)
                return
        self._cmd_activas()

    def _cmd_activas(self) -> None:
        activas = self.db.get_active_signals()
        if not activas:
            self._send(
                "📭 <b>Sin señales activas ahora mismo.</b>\n\n"
                "💡 El scanner revisa el mercado cada minuto.\n"
                "Usa /escanear para forzar un análisis inmediato."
            )
            return
        self._send(f"📊 <b>Señales activas: {len(activas)}</b>")
        for s in activas:
            self._send(_formato_señal(s))

    def _cmd_por_tf(self, tf: str) -> None:
        activas = self.db.get_active_signals()
        señales = [s for s in activas if s["timeframe"] == tf]
        if not señales:
            self._send(
                f"📭 Sin señales activas en <b>{tf}</b> ahora mismo.\n\n"
                f"💡 Usa /escanear para analizar el mercado ahora."
            )
            return
        self._send(f"📊 <b>Señales {tf} — {len(señales)} activa(s):</b>")
        for s in señales:
            self._send(_formato_señal(s))

    def _cmd_por_tf_lista(self, tfs: list[str], nombre: str) -> None:
        activas = self.db.get_active_signals()
        señales = [s for s in activas if s["timeframe"] in tfs]
        if not señales:
            self._send(
                f"📭 Sin señales de <b>{nombre}</b> ({'/'.join(tfs)}) ahora mismo."
            )
            return
        self._send(f"📊 <b>{nombre} ({'/'.join(tfs)}) — {len(señales)} señal(es):</b>")
        for s in señales:
            self._send(_formato_señal(s))

    def _cmd_por_tipo(self, tipo: str) -> None:
        activas = self.db.get_active_signals()
        señales = [s for s in activas if tipo in s.get("signal_type", "").upper()]
        emoji = "🚀" if tipo == "BUY" else "📉"
        label = "COMPRA (BUY)" if tipo == "BUY" else "VENTA (SELL)"
        if not señales:
            self._send(f"📭 Sin señales de <b>{label}</b> activas ahora mismo.")
            return
        self._send(f"{emoji} <b>Señales de {label} — {len(señales)} activa(s):</b>")
        for s in señales:
            self._send(_formato_señal(s))

    def _cmd_por_simbolo(self, simbolo: str) -> None:
        activas = self.db.get_active_signals()
        señales = [s for s in activas if s.get("symbol", "").upper() == simbolo]
        nombre = "Oro (XAUUSD)" if simbolo == "XAUUSD" else simbolo
        if not señales:
            self._send(f"📭 Sin señales activas para <b>{nombre}</b> ahora mismo.")
            return
        self._send(f"📊 <b>{nombre} — {len(señales)} señal(es) activa(s):</b>")
        for s in señales:
            self._send(_formato_señal(s))

    def _cmd_escanear(self, user_id: int, chat_id: str, username: str) -> None:
        if not self.es_admin(user_id, chat_id):
            self._send(
                f"⚠️ @{username} — solo los administradores pueden forzar un escaneo.\n"
                "Usa /señales para ver las señales activas."
            )
            return
        self._send("🔍 <b>Iniciando escaneo de mercado...</b>\nAnalizando patrones en XAUUSD y EURUSD.")
        scanner = self._get_scanner()
        if scanner is None:
            self._send("⚠️ El scanner no está listo todavía. Intenta en unos segundos.")
            return
        try:
            señales = scanner.scan_all()
            if señales:
                self._send(
                    f"✅ <b>Escaneo completo</b>\n"
                    f"Se encontraron {len(señales)} señal(es) nueva(s).\n"
                    "Los detalles ya fueron enviados arriba."
                )
            else:
                self._send(
                    "✅ <b>Escaneo completo</b>\n"
                    "Sin nuevas señales en este momento.\n\n"
                    "El mercado no presenta setups accionables ahora. "
                    "El scanner continúa monitoreando automáticamente."
                )
        except Exception as exc:
            logger.warning("Error en /escanear: %s", exc)
            self._send("⚠️ Error durante el escaneo. Intenta nuevamente.")

    def _cmd_estado(self) -> None:
        activas = self.db.get_active_signals()
        stats = self.db.get_performance_stats()
        self._send(
            "⚙️ <b>Estado del Scanner PRO</b>\n\n"
            f"🟢 Scanner: Activo\n"
            f"📊 Señales activas ahora: {len(activas)}\n"
            f"📈 Señales generadas hoy: {stats.get('total_signals', 0)}\n"
            f"✅ Ganadoras: {stats.get('wins', 0)}\n"
            f"❌ Stop Loss: {stats.get('losses', 0)}\n"
            f"🎯 Win Rate hoy: {stats.get('win_rate', 0):.1f}%\n"
            f"💰 Pips netos hoy: {stats.get('total_pips', 0):+.1f}\n\n"
            "Usa /señales para ver las señales activas."
        )

    def _cmd_stats(self) -> None:
        stats = self.db.get_performance_stats()
        wins = stats.get('wins', 0)
        losses = stats.get('losses', 0)
        total = stats.get('total_signals', 0)
        pips = stats.get('total_pips', 0)
        emoji_pips = "💰" if pips >= 0 else "📉"
        self._send(
            "📈 <b>Estadísticas de hoy</b>\n\n"
            f"Señales generadas: {total}\n"
            f"✅ Ganadoras (TP1/TP2/TP3): {wins}\n"
            f"❌ Stop Loss: {losses}\n"
            f"➖ Breakeven: {stats.get('breakeven', 0)}\n"
            f"🎯 Win Rate: {stats.get('win_rate', 0):.1f}%\n"
            f"{emoji_pips} Pips netos: {pips:+.1f} pips\n\n"
            "📊 El reporte diario completo se envía automáticamente a las 22:00 UTC."
        )

    def _cmd_setgif(self, args: list[str], user_id: int, chat_id: str, username: str) -> None:
        if not self.es_admin(user_id, chat_id):
            self._send("❌ Solo admins pueden configurar GIFs.")
            return
        _tipos_validos = ["compra", "venta", "tp1", "tp2", "tp3", "sl", "noticia", "volatilidad"]
        if not args or args[0] not in _tipos_validos:
            self._send(
                "📸 <b>Configurar GIF Premium</b>\n\n"
                "Uso: <code>/setgif [tipo]</code>\n\n"
                "Tipos disponibles:\n"
                "  compra  venta  tp1  tp2  tp3  sl  noticia  volatilidad\n\n"
                "Ejemplo:\n"
                "1️⃣ Escribe: <code>/setgif compra</code>\n"
                "2️⃣ Envía el GIF o sticker que quieres usar\n"
                "3️⃣ El scanner lo usará automáticamente 🚀"
            )
            return
        tipo = args[0]
        self._pending_gif[user_id] = tipo
        self._send(
            f"📸 Modo captura activado para <b>{tipo.upper()}</b>\n\n"
            f"Envía ahora el GIF o sticker que quieres usar.\n"
            f"<i>(Tienes 5 minutos — funciona con GIFs, stickers y videos cortos)</i>"
        )
        # Limpiar después de 5 minutos si no envía nada
        import threading
        def _timeout():
            import time; time.sleep(300)
            if self._pending_gif.get(user_id) == tipo:
                del self._pending_gif[user_id]
        threading.Thread(target=_timeout, daemon=True).start()

    def _cmd_objetivo(self, args: list[str], user_id: int, chat_id: str) -> None:
        if not self.es_admin(user_id, chat_id):
            self._send("❌ Solo administradores pueden cambiar el objetivo de pips.")
            return
        if not args:
            current = self.config.get("trading", {}).get("min_pip_target", 0)
            self._send(
                f"🎯 <b>Objetivo mínimo de pips</b>\n\n"
                f"Actual: <b>{current} pips</b> {'(desactivado)' if current == 0 else ''}\n\n"
                f"Uso: <code>/objetivo 50</code>\n"
                f"Filtra señales con TP3 &lt; 50 pips.\n\n"
                f"Referencia por temporalidad:\n"
                f"  M1/M5 → 5-30 pips\n"
                f"  M15/H1 → 30-100 pips\n"
                f"  H4/D1 → 100+ pips\n\n"
                f"<code>/objetivo 0</code> desactiva el filtro."
            )
            return
        try:
            n = int(args[0])
            if n < 0:
                raise ValueError
        except (ValueError, IndexError):
            self._send("❌ Valor inválido. Ejemplo: <code>/objetivo 50</code>")
            return

        self.config.setdefault("trading", {})["min_pip_target"] = n
        # Persistir en config.json
        try:
            import json as _json_mod
            from pathlib import Path as _Path_mod
            config_path = _Path_mod(__file__).parent / "config.json"
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_disk = _json_mod.load(f)
            cfg_disk.setdefault("trading", {})["min_pip_target"] = n
            with open(config_path, "w", encoding="utf-8") as f:
                _json_mod.dump(cfg_disk, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("No se pudo guardar objetivo en config.json: %s", exc)

        if n == 0:
            self._send("✅ Filtro de pips <b>desactivado</b> — el scanner enviará señales de cualquier tamaño.")
        else:
            self._send(
                f"✅ <b>Objetivo actualizado: mínimo {n} pips en TP3</b>\n\n"
                f"Las señales con TP3 &lt; {n} pips serán ignoradas.\n"
                f"Para más pips usa H4 o D1.\n"
                f"Para pips rápidos usa M5 o M15.\n\n"
                f"Usa <code>/objetivo 0</code> para desactivar."
            )

    def _cmd_gifs_status(self, user_id: int, chat_id: str) -> None:
        if not self.es_admin(user_id, chat_id):
            return
        try:
            import telegram_bot as _tg
            data = _tg._load_gif_ids()
            lines = ["🎬 <b>GIFs Premium configurados:</b>\n"]
            for tipo in ["compra", "venta", "tp1", "tp2", "tp3", "sl", "noticia", "volatilidad"]:
                count = len(data.get(tipo, []))
                icon = "✅" if count > 0 else "⬜"
                lines.append(f"  {icon} <b>{tipo}</b>: {count} GIF(s)")
            lines.append("\nUsa <code>/setgif [tipo]</code> para agregar más.")
            self._send("\n".join(lines))
        except Exception as exc:
            self._send(f"Error: {exc}")


# ─────────────────────────────────────────────────────────────────
# Formato de señal para respuestas del bot
# ─────────────────────────────────────────────────────────────────

def _formato_señal(s: dict) -> str:
    es_compra = "BUY" in s.get("signal_type", "").upper()
    emoji = "🚀" if es_compra else "📉"
    tipo_es = "COMPRA" if es_compra else "VENTA"

    _estado_map = {
        "ACTIVE": "🟢 En seguimiento",
        "TP1":    "✅ TP1 alcanzado — SL movido a entrada",
        "TP2":    "✅✅ TP2 alcanzado — dejando correr al TP3",
    }
    estado = _estado_map.get(s.get("status", "ACTIVE"), s.get("status", ""))
    es_buy = "BUY" in s.get("signal_type", "").upper()
    direccion = "📈 COMPRA" if es_buy else "📉 VENTA"

    return (
        f"{emoji} <b>{s.get('symbol')} — {direccion} — {s.get('timeframe')}</b>\n"
        f"📍 Entrada: <code>{s.get('entry')}</code>\n"
        f"🎯 TP1: <code>{s.get('tp1')}</code>  TP2: <code>{s.get('tp2')}</code>  TP3: <code>{s.get('tp3')}</code>\n"
        f"🛑 SL: <code>{s.get('sl')}</code>  |  RR 1:{s.get('rr')}  |  {s.get('confidence')}%\n"
        f"Estado: {estado}"
    )
