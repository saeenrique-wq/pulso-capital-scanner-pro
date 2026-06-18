"""FastAPI entry point for the trading scanner."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import brokers
import database as db
import news as news_mod
import telegram_bot as tg
from scanner import TradingScanner
from telegram_commands import TelegramCommandHandler
import market_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scanner-pro")

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATES_DIR = ROOT / "templates"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.disconnect(d)


manager = ConnectionManager()


class AppState:
    def __init__(self) -> None:
        self.scanner: TradingScanner | None = None
        self.mt5_connected: bool = False
        self.last_signal_ids: set[int] = set()
        self.tasks: list[asyncio.Task[Any]] = []
        self.last_scan_at: str | None = None
        self.tg_handler: TelegramCommandHandler | None = None


state = AppState()


# ---------------- Background loops ----------------


async def scan_loop() -> None:
    while True:
        try:
            if state.scanner is None:
                await asyncio.sleep(5)
                continue
            loop = asyncio.get_running_loop()
            signals = await loop.run_in_executor(None, state.scanner.scan_all)
            state.last_scan_at = datetime.now(timezone.utc).isoformat()
            for sig in signals:
                sid = int(sig.get("id", 0))
                if sid and sid not in state.last_signal_ids:
                    state.last_signal_ids.add(sid)
                    await manager.broadcast({"type": "new_signal", "data": sig})
        except Exception as exc:
            logger.exception("scan_loop error: %s", exc)
        await asyncio.sleep(60)


async def monitor_loop() -> None:
    while True:
        try:
            if state.scanner is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, state.scanner.monitor_active_signals)
                await manager.broadcast({"type": "monitor_tick", "data": {"ts": datetime.now(timezone.utc).isoformat()}})
        except Exception as exc:
            logger.exception("monitor_loop error: %s", exc)
        await asyncio.sleep(30)


async def news_loop() -> None:
    while True:
        try:
            loop = asyncio.get_running_loop()
            events = await loop.run_in_executor(None, news_mod.get_high_impact_events, 24)
            for ev in events:
                db.save_news(ev)
            await manager.broadcast({"type": "news_update", "count": len(events)})
        except Exception as exc:
            logger.exception("news_loop error: %s", exc)
        await asyncio.sleep(900)


async def alerts_loop() -> None:
    """Verifica volatilidad, noticias y sentimiento cada 5 minutos."""
    await asyncio.sleep(30)  # espera inicial para que el servidor arranque
    while True:
        try:
            loop = asyncio.get_running_loop()
            symbols = CONFIG.get("trading", {}).get("symbols", ["XAUUSD", "EURUSD"])
            feed_agg = brokers.get_feed_aggregator()
            await loop.run_in_executor(
                None,
                market_alerts.run_all_checks,
                CONFIG, brokers, news_mod, feed_agg, symbols,
            )
        except Exception as exc:
            logger.warning("alerts_loop error: %s", exc)
        await asyncio.sleep(300)  # cada 5 minutos


async def news_impulse_loop() -> None:
    """Detecta noticias recién publicadas (últimos 5 min) y envía señal M5 de impulso."""
    _fired: dict[str, float] = {}  # ev_key → timestamp
    while True:
        try:
            if state.scanner is not None:
                loop = asyncio.get_running_loop()
                symbols = CONFIG.get("trading", {}).get("symbols", ["XAUUSD", "EURUSD"])
                now_ts = datetime.now(timezone.utc).timestamp()
                # limpiar claves viejas (>1h)
                _fired_clean = {k: v for k, v in _fired.items() if now_ts - v < 3600}
                _fired.clear(); _fired.update(_fired_clean)

                for sym in symbols:
                    events = await loop.run_in_executor(
                        None, news_mod.get_just_released_events, sym, 5
                    )
                    for ev in events:
                        ev_key = f"{ev.get('title','')}|{ev.get('event_time','')}|{sym}"
                        if ev_key in _fired:
                            continue
                        direction, explanation = news_mod.calc_news_direction(sym, ev)
                        _fired[ev_key] = now_ts
                        if direction is None:
                            logger.debug("News sin dirección para %s: %s", sym, explanation)
                            continue
                        # No crear si ya hay señal ACTIVE
                        active = db.get_active_signals()
                        if any(s.get("symbol") == sym and s.get("status") == "ACTIVE" for s in active):
                            logger.info("NEWS IMPULSE %s bloqueado — señal activa ya existe", sym)
                            continue
                        sig = await loop.run_in_executor(
                            None, state.scanner.calculate_news_signal, sym, direction, explanation
                        )
                        if sig is None:
                            continue
                        sid = db.save_signal(sig)
                        sig["id"] = sid
                        tg.send_news_impulse_signal(CONFIG, sig, ev)
                        logger.info(
                            "NEWS IMPULSE enviado: %s %s — %s", sym, direction, explanation[:70]
                        )
        except Exception as exc:
            logger.warning("news_impulse_loop error: %s", exc)
        await asyncio.sleep(60)


async def telegram_command_loop() -> None:
    """Recibe y procesa comandos del grupo Telegram cada 2 segundos."""
    while True:
        try:
            if state.tg_handler is not None:
                loop = asyncio.get_running_loop()
                updates = await loop.run_in_executor(None, state.tg_handler.obtener_updates)
                for upd in updates:
                    await loop.run_in_executor(None, state.tg_handler.procesar_update, upd)
        except Exception as exc:
            logger.warning("telegram_command_loop error: %s", exc)
        await asyncio.sleep(2)


async def daily_report_task() -> None:
    sent_for: str | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            day_key = now.date().isoformat()
            if now.hour == 22 and sent_for != day_key:
                stats = db.update_performance()
                tg.send_daily_report(CONFIG, stats)
                sent_for = day_key
        except Exception as exc:
            logger.exception("daily_report_task error: %s", exc)
        await asyncio.sleep(60)


# ---------------- Lifespan ----------------


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    db.init_db()
    logger.info("Database ready")

    try:
        state.mt5_connected = brokers.connect_mt5(CONFIG)
    except Exception as exc:
        logger.warning("MT5 connect raised: %s", exc)
        state.mt5_connected = False

    state.scanner = TradingScanner(CONFIG)
    logger.info("Scanner initialised (MT5=%s)", state.mt5_connected)

    state.tg_handler = TelegramCommandHandler(
        config=CONFIG,
        db_module=db,
        scanner_getter=lambda: state.scanner,
    )
    logger.info("Telegram command handler listo")

    try:
        tg.send_startup_message(CONFIG)
    except Exception:
        pass

    state.tasks = [
        asyncio.create_task(scan_loop()),
        asyncio.create_task(monitor_loop()),
        asyncio.create_task(news_loop()),
        asyncio.create_task(daily_report_task()),
        asyncio.create_task(telegram_command_loop()),
        asyncio.create_task(alerts_loop()),
        asyncio.create_task(news_impulse_loop()),
    ]
    try:
        yield
    finally:
        for t in state.tasks:
            t.cancel()
        for t in state.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            brokers.disconnect_mt5()
        except Exception:
            pass


app = FastAPI(title="SCANNER PRO", lifespan=lifespan)


# ---------------- Routes ----------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = TEMPLATES_DIR / "dashboard.html"
    if not html_path.exists():
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/signals/active")
async def api_active_signals() -> JSONResponse:
    return JSONResponse(db.get_active_signals())


@app.get("/api/signals/history")
async def api_signal_history() -> JSONResponse:
    return JSONResponse(db.get_signal_history(50))


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    return JSONResponse(db.get_performance_stats())


@app.get("/api/news")
async def api_news() -> JSONResponse:
    events = news_mod.get_high_impact_events(24)
    return JSONResponse(events)


@app.get("/api/scan/force")
async def api_force_scan() -> JSONResponse:
    if state.scanner is None:
        return JSONResponse({"ok": False, "error": "scanner not ready"}, status_code=503)
    loop = asyncio.get_running_loop()
    signals = await loop.run_in_executor(None, state.scanner.scan_all)
    state.last_scan_at = datetime.now(timezone.utc).isoformat()
    for sig in signals:
        sid = int(sig.get("id", 0))
        if sid and sid not in state.last_signal_ids:
            state.last_signal_ids.add(sid)
            await manager.broadcast({"type": "new_signal", "data": sig})
    return JSONResponse({"ok": True, "count": len(signals), "signals": signals})


@app.get("/api/status")
async def api_status() -> JSONResponse:
    ollama_ok = False
    try:
        resp = requests.get(
            CONFIG.get("ollama", {}).get("host", "http://localhost:11434") + "/api/tags",
            timeout=2,
        )
        ollama_ok = resp.status_code == 200
    except Exception:
        ollama_ok = False

    telegram_ok = False
    try:
        telegram_ok = tg.test_connection(CONFIG)
    except Exception:
        telegram_ok = False

    session = state.scanner.get_session_name() if state.scanner else "Unknown"

    mt4_info = brokers.mt4_bridge_info()

    return JSONResponse(
        {
            "mt5": state.mt5_connected,
            "mt4": mt4_info.get("active", False),
            "mt4_info": mt4_info,
            "ollama": ollama_ok,
            "telegram": telegram_ok,
            "session": session,
            "last_scan_at": state.last_scan_at,
            "yfinance": brokers.yfinance_available(),
            "utc": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/feeds/status")
async def api_feeds_status() -> JSONResponse:
    """Estado de cada feed externo (latencia + disponibilidad)."""
    agg = brokers.get_feed_aggregator()
    if agg is None:
        return JSONResponse(
            {"available": False, "error": "data_feeds module no cargado"},
            status_code=503,
        )
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(None, agg.get_all_feeds_status)
    return JSONResponse({"available": True, "feeds": status})


@app.get("/api/feeds/sentiment")
async def api_feeds_sentiment(symbol: str = "XAUUSD") -> JSONResponse:
    """Sentimiento del mercado (Fear & Greed) + noticias del símbolo."""
    agg = brokers.get_feed_aggregator()
    if agg is None:
        return JSONResponse(
            {"available": False, "error": "data_feeds module no cargado"},
            status_code=503,
        )
    loop = asyncio.get_running_loop()
    market = await loop.run_in_executor(None, agg.get_market_sentiment)
    news = await loop.run_in_executor(None, agg.get_news_sentiment, symbol)
    return JSONResponse({
        "available": True,
        "market": market,
        "news": news,
        "symbol": symbol,
    })


@app.get("/api/feeds/correlations")
async def api_feeds_correlations() -> JSONResponse:
    """DXY, BTC, S&P500, VIX y oro agregado — correlaciones macro."""
    agg = brokers.get_feed_aggregator()
    if agg is None:
        return JSONResponse(
            {"available": False, "error": "data_feeds module no cargado"},
            status_code=503,
        )
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, agg.get_correlation_data)
    return JSONResponse({"available": True, "data": data})


@app.get("/api/debug/scan")
async def api_debug_scan() -> JSONResponse:
    """Diagnóstico completo — muestra por qué se rechaza cada señal."""
    if state.scanner is None:
        return JSONResponse({"ok": False, "error": "scanner no listo"}, status_code=503)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, state.scanner.debug_scan)
    return JSONResponse({"ok": True, "diagnostico": result})


@app.get("/api/backtesting")
async def api_backtesting(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 500,
) -> JSONResponse:
    """Backtesting del historial de señales: win rate, profit factor, pips por símbolo/TF/hora."""
    import backtesting as bt
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: bt.run_backtest(symbol=symbol, timeframe=timeframe, limit=limit)
    )
    return JSONResponse({"ok": True, "backtest": result})


@app.post("/api/signals/cleanup")
async def api_cleanup_signals() -> JSONResponse:
    """Cierra señales ACTIVE con más de 24 horas para limpiar el bloqueo 'already active'."""
    from datetime import timedelta
    import database as _db
    active = _db.get_active_signals()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cancelled = 0
    for sig in active:
        try:
            created = datetime.fromisoformat(sig["created_at"].replace("Z", "+00:00"))
            if created < cutoff:
                _db.update_signal_status(int(sig["id"]), "CANCELLED", notes="Limpieza automática >24h")
                cancelled += 1
        except Exception:
            pass
    return JSONResponse({"ok": True, "canceladas": cancelled, "total_activas": len(active)})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "ts": datetime.now(timezone.utc).isoformat()})
        while True:
            # Keep alive: receive pings or any message
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if msg == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                await ws.send_json({"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


def main() -> None:
    server_cfg = CONFIG.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = int(server_cfg.get("port", 8080))
    uvicorn.run("main:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
