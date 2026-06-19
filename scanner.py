"""Trading scanner engine: pattern + indicator confluence."""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timezone
from typing import Any

import pandas as pd
import requests

import brokers
import data_feeds
import database as db
import news as news_mod
import telegram_bot as tg
from indicators import calc_ema, get_all_indicators
from patterns import analyze_patterns

logger = logging.getLogger(__name__)


def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if s in {"XAUUSD", "GOLD"}:
        return 0.1
    if s in {"BTCUSD", "BTCUSDT"}:
        return 1.0
    if s in {"ETHUSD", "ETHUSDT"}:
        return 0.1
    if s in {"XRPUSD", "XRPUSDT"}:
        return 0.0001
    return 0.0001


class TradingScanner:
    def __init__(
        self,
        config: dict[str, Any],
        database_module: Any = db,
        broker_module: Any = brokers,
        news_module: Any = news_mod,
        telegram_module: Any = tg,
    ) -> None:
        self.config = config
        self.db = database_module
        self.broker = broker_module
        self.news = news_module
        self.telegram = telegram_module
        self.trading_cfg = config.get("trading", {})
        self.ollama_cfg = config.get("ollama", {})

    # ---------------- Session / Trend helpers ----------------

    def _gold_macro_bias(self) -> tuple[str, str]:
        """
        Sesgo macro complementario para XAUUSD basado en DXY (índice USD).
        DXY sube → viento en contra del oro (bearish_gold).
        DXY baja → viento a favor del oro (bullish_gold).
        Cacheado 60s dentro de data_feeds — no penaliza performance del scan.
        """
        try:
            dxy_df = data_feeds.fetch_stooq("usdidx", "d")
            if dxy_df is None or len(dxy_df) < 2:
                return "neutral", "DXY no disponible"
            last = float(dxy_df["close"].iloc[-1])
            prev = float(dxy_df["close"].iloc[-2])
            chg_pct = ((last - prev) / prev * 100) if prev else 0.0
            if chg_pct > 0.15:
                return "bearish_gold", f"DXY {last:.2f} ({chg_pct:+.2f}%)"
            if chg_pct < -0.15:
                return "bullish_gold", f"DXY {last:.2f} ({chg_pct:+.2f}%)"
            return "neutral", f"DXY {last:.2f} plano ({chg_pct:+.2f}%)"
        except Exception as exc:
            logger.debug("gold_macro_bias error: %s", exc)
            return "neutral", "error obteniendo DXY"

    def is_weekend(self) -> bool:
        """True sábado/domingo UTC — mercado forex/oro cerrado, cripto sigue 24/7."""
        return datetime.now(timezone.utc).weekday() >= 5

    def _active_symbols(self) -> list[str]:
        """Lista de símbolos a escanear: oro entre semana, cripto en fin de semana."""
        if self.is_weekend():
            weekend_syms = self.trading_cfg.get("weekend_symbols", [])
            if weekend_syms:
                return weekend_syms
        return self.trading_cfg.get("symbols", [])

    def get_session_name(self) -> str:
        now = datetime.now(timezone.utc).time()
        sessions = self.trading_cfg.get("sessions", {})
        active: list[str] = []
        for name, sess in sessions.items():
            try:
                start = dtime.fromisoformat(sess["start"])
                end = dtime.fromisoformat(sess["end"])
            except Exception:
                continue
            if start <= now <= end:
                active.append(name.replace("_", " ").title())
        if len(active) >= 2:
            return "Overlap (" + " + ".join(active) + ")"
        if active:
            return active[0]
        return "Off-Hours"

    def get_trend_mtf(self, symbol: str) -> dict[str, str]:
        out: dict[str, str] = {"H1": "NEUTRAL", "H4": "NEUTRAL"}
        for tf in ("H1", "H4"):
            try:
                df = self.broker.get_ohlcv(symbol, tf, 220)
                if df is None or df.empty or len(df) < 30:
                    continue
                ema_period = min(200, max(20, len(df) - 1))
                ema_series = calc_ema(df["close"], ema_period)
                last_close = float(df["close"].iloc[-1])
                last_ema = float(ema_series.iloc[-1])
                if last_close > last_ema * 1.001:
                    out[tf] = "BULLISH"
                elif last_close < last_ema * 0.999:
                    out[tf] = "BEARISH"
                else:
                    out[tf] = "NEUTRAL"
            except Exception as exc:
                logger.warning("Trend MTF error %s/%s: %s", symbol, tf, exc)
        return out

    def _trend_superior(self, symbol: str, ref_tf: str) -> tuple[str, float]:
        """
        Devuelve (tendencia, rsi) del timeframe de referencia.
        ref_tf: "H4" para filtrar M15/H1; "D1" para filtrar H4.
        Tendencia: "BULLISH" | "BEARISH" | "NEUTRAL"
        """
        try:
            df = self.broker.get_ohlcv(symbol, ref_tf, 150)
            if df is None or df.empty or len(df) < 50:
                return "NEUTRAL", 50.0
            indics = get_all_indicators(df)
            rsi   = float(indics.get("rsi", 50))
            e20   = float(indics.get("ema20", 0))
            e50   = float(indics.get("ema50", 0))
            close = float(indics.get("close", 0))
            # Tendencia clara: precio y EMAs alineadas
            if close > e20 > e50 and rsi > 45:
                return "BULLISH", rsi
            if close < e20 < e50 and rsi < 55:
                return "BEARISH", rsi
            return "NEUTRAL", rsi
        except Exception:
            return "NEUTRAL", 50.0

    def _alineado_con_tendencia(
        self, symbol: str, direction: str, timeframe: str
    ) -> tuple[bool, str]:
        """
        Cadena de tendencia completa por timeframe.
        M1/M5 → H1 + H4 deben estar alineados.
        M15   → H4 debe ser BULLISH/BEARISH (no NEUTRAL).
        H1    → H4 y D1 sin contradicción.
        H4    → D1 sin contradicción.
        """
        if timeframe in ("M1", "M5"):
            h1,  _ = self._trend_superior(symbol, "H1")
            h4,  _ = self._trend_superior(symbol, "H4")
            # H4 contra → bloqueo duro
            if h4 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: H4 BAJISTA"
            if h4 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: H4 ALCISTA"
            # H1 contra → bloqueo duro
            if h1 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: H1 BAJISTA"
            if h1 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: H1 ALCISTA"
            # Ambos NEUTRAL = mercado indeciso, sin scalp
            if h4 == "NEUTRAL" and h1 == "NEUTRAL":
                return False, "H4+H1 ambos NEUTRAL — mercado sin dirección"

        elif timeframe == "M15":
            h4, _ = self._trend_superior(symbol, "H4")
            if h4 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: H4 BAJISTA"
            if h4 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: H4 ALCISTA"
            # M15 requiere H4 definido, no NEUTRAL
            if h4 == "NEUTRAL":
                return False, "H4 NEUTRAL — sin setup válido en M15"

        elif timeframe == "H1":
            h4, _ = self._trend_superior(symbol, "H4")
            d1, _ = self._trend_superior(symbol, "D1")
            if h4 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: H4 BAJISTA"
            if h4 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: H4 ALCISTA"
            if d1 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: D1 BAJISTA"
            if d1 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: D1 ALCISTA"

        elif timeframe == "H4":
            d1, _ = self._trend_superior(symbol, "D1")
            if d1 == "BEARISH" and direction == "BULLISH":
                return False, "BUY bloqueado: D1 BAJISTA"
            if d1 == "BULLISH" and direction == "BEARISH":
                return False, "SELL bloqueado: D1 ALCISTA"

        return True, ""

    # ---------------- Confidence ----------------

    def calculate_confidence(
        self,
        patterns: dict[str, Any],
        indicators: dict[str, Any],
        confirmations: list[str],
    ) -> int:
        score = 0  # base cero — hay que ganarse cada punto

        # Patrón armónico (máx 35)
        best_h = patterns.get("best_harmonic")
        pa = patterns.get("price_action", {})
        pin = pa.get("pin_bar", {})
        eng = pa.get("engulfing", {})

        if best_h and best_h.get("found"):
            h_conf = int(best_h.get("confidence", 60))
            if h_conf >= 85:
                score += 35
            elif h_conf >= 70:
                score += 25
            else:
                score += 15
        else:
            pin_str = int(pin.get("strength", 0)) if pin.get("found") else 0
            eng_str = int(eng.get("strength", 0)) if eng.get("found") else 0
            if pin_str >= 60:
                score += 20
            elif pin_str >= 40:
                score += 12
            if eng_str >= 60:
                score += 18
            elif eng_str >= 40:
                score += 10

        # Confirmaciones técnicas (máx 30 — 10 pts c/u, máx 3 que cuentan)
        score += min(30, len(confirmations) * 10)

        # Sesión (máx 15)
        session = self.get_session_name()
        if "Overlap" in session:
            score += 15
        elif "London" in session or "New York" in session:
            score += 10
        elif "Asia" in session:
            score += 3

        # ADX — fuerza de tendencia (máx 12)
        adx_v = float(indicators.get("adx", 20))
        if adx_v >= 32:
            score += 12
        elif adx_v >= 23:
            score += 7
        elif adx_v >= 16:
            score += 3

        # EMAs alineadas en cascada (c > e20 > e50) — máx 10
        c   = indicators.get("close", 0)
        e20 = indicators.get("ema20", 0)
        e50 = indicators.get("ema50", 0)
        if c and e20 and e50:
            if c > e20 > e50:
                score += 10
            elif c < e20 < e50:
                score += 10

        # RSI en zona de momentum óptima
        rsi = float(indicators.get("rsi", 50))
        if 30 < rsi < 50:
            score += 8
        elif 50 < rsi < 70:
            score += 8

        # Divergencia RSI confirmada
        if indicators.get("divergence_bullish") or indicators.get("divergence_bearish"):
            score += 10

        # MACD acelerando a favor
        macd_hist = float(indicators.get("macd_histogram", 0))
        macd_prev = float(indicators.get("macd_prev_hist", 0))
        if (macd_hist > 0 and macd_hist > macd_prev) or (macd_hist < 0 and macd_hist < macd_prev):
            score += 8

        # Bollinger extremo (toca banda)
        pct_b = indicators.get("bb_percent_b", 0.5)
        if pct_b < 0.08 or pct_b > 0.92:
            score += 8

        return int(max(0, min(100, round(score))))

    # ---------------- SL estructural + constructor de señal ----------------

    def _structural_sl(
        self,
        df: pd.DataFrame,
        direction: str,
        close: float,
        atr: float,
        tf: str = "M15",
    ) -> float:
        """
        SL en el swing reciente (últimas 7 velas), acotado por límites ATR del TF.
        TFs altos (H4/D1) usan límites más ajustados para mantener objetivos alcanzables.
        """
        atr_min, atr_max = self._SL_ATR_BOUNDS.get(tf, (0.5, 2.0))
        try:
            recent = df.iloc[-8:-1]
            if len(recent) < 3:
                raise ValueError("insuficientes")
            if direction == "BULLISH":
                swing = float(recent["low"].min())
                swing = max(swing, close - atr_max * atr)
                swing = min(swing, close - atr_min * atr)
                return swing
            else:
                swing = float(recent["high"].max())
                swing = min(swing, close + atr_max * atr)
                swing = max(swing, close + atr_min * atr)
                return swing
        except Exception:
            fallback = (atr_min + atr_max) / 2
            return (close - fallback * atr) if direction == "BULLISH" else (close + fallback * atr)

    def _build_signal(
        self,
        symbol: str,
        tf: str,
        close: float,
        direction: str,
        setup_name: str,
        confirmations: list[str],
        patterns: dict[str, Any],
        indicators: dict[str, Any],
        df: pd.DataFrame,
        mode: str = "TREND",
    ) -> dict[str, Any] | None:
        """
        Construye el dict de señal con SL estructural y RR fijo 3:1.
        TP1=1×riesgo, TP2=2×riesgo, TP3=3×riesgo.
        """
        atr = float(indicators.get("atr", 0))
        if atr <= 0:
            return None

        is_buy = direction == "BULLISH"
        sl = self._structural_sl(df, direction, close, atr, tf=tf)
        risk = abs(close - sl)

        if risk <= 0 or risk < atr * 0.15:
            return None

        m1, m2, m3 = self._TP_MULTS.get(tf, (1.5, 2.5, 4.0))

        if is_buy:
            signal_type = "BUY"
            tp1 = close + risk * m1
            tp2 = close + risk * m2
            tp3 = close + risk * m3
        else:
            signal_type = "SELL"
            tp1 = close - risk * m1
            tp2 = close - risk * m2
            tp3 = close - risk * m3

        sym_u = symbol.upper()
        if "XAU" in sym_u or sym_u in {"BTCUSD", "ETHUSD"}:
            digits = 2
        elif sym_u == "XRPUSD":
            digits = 4
        else:
            digits = 5
        rnd = lambda v: round(float(v), digits)

        confidence = self.calculate_confidence(patterns, indicators, confirmations)

        if "XAU" in sym_u or "GOLD" in sym_u:
            macro_bias, macro_reason = self._gold_macro_bias()
            if macro_bias == "bullish_gold" and direction == "BULLISH":
                confidence = min(100, confidence + 6)
                confirmations.append(f"Macro DXY a favor: {macro_reason}")
            elif macro_bias == "bearish_gold" and direction == "BEARISH":
                confidence = min(100, confidence + 6)
                confirmations.append(f"Macro DXY a favor: {macro_reason}")
            elif macro_bias == "bullish_gold" and direction == "BEARISH":
                confidence = max(0, confidence - 6)
                confirmations.append(f"Macro DXY en contra: {macro_reason}")
            elif macro_bias == "bearish_gold" and direction == "BULLISH":
                confidence = max(0, confidence - 6)
                confirmations.append(f"Macro DXY en contra: {macro_reason}")

        news_summary = self.news.get_news_summary(symbol)

        return {
            "symbol":        symbol,
            "timeframe":     tf,
            "signal_type":   signal_type,
            "setup":         setup_name,
            "entry":         rnd(close),
            "tp1":           rnd(tp1),
            "tp2":           rnd(tp2),
            "tp3":           rnd(tp3),
            "sl":            rnd(sl),
            "rr":            4.0,
            "atr":           round(atr, 4),
            "confidence":    confidence,
            "status":        "ACTIVE",
            "created_at":    datetime.now(timezone.utc).isoformat(),
            "notes":         "; ".join(confirmations),
            "confirmations": confirmations,
            "news_summary":  news_summary,
            "session":       self.get_session_name(),
            "market_mode":   mode,
        }

    # ---------------- Confluencia MTF ----------------

    def _check_confluencia(
        self, symbol: str, tf: str, direction: str
    ) -> tuple[bool, str]:
        """
        M5 solo puede entrar si M15 tiene setup activo en la misma dirección.
        M1 solo puede entrar si M5 tiene setup activo en la misma dirección.
        H1/H4/D1 no requieren confluencia de TF inferior.
        """
        import time as _t
        now = _t.time()

        if tf == "M5":
            key = f"{symbol}_M15"
            p = self._pending_confirmations.get(key)
            if not p:
                return False, "M5 sin tendencia M15 activa — espera que M15 forme setup"
            if now > p["expires"]:
                self._pending_confirmations.pop(key, None)
                return False, "M5: ventana de tendencia M15 expiró"
            if p["direction"] != direction:
                return False, f"M5 {direction} contradice M15 {p['direction']}"
            return True, f"M5 confirmado por M15 ({p['setup']})"

        if tf == "M1":
            key = f"{symbol}_M5"
            p = self._pending_confirmations.get(key)
            if not p:
                return False, "M1 sin confirmación M5 — espera que M5 forme setup"
            if now > p["expires"]:
                self._pending_confirmations.pop(key, None)
                return False, "M1: confirmación M5 expiró"
            if p["direction"] != direction:
                return False, f"M1 {direction} contradice M5 {p['direction']}"
            return True, "M1 entrada confirmada por M5"

        return True, ""  # H1/H4/D1 sin restricción de confluencia

    def _register_pending(
        self, symbol: str, tf: str, direction: str, setup: str
    ) -> None:
        """
        Registra un setup M15 o M5 como pendiente de confirmación para el siguiente TF.
        Solo aplica a M15 y M5.
        """
        import time as _t
        expiry = self._PENDING_EXPIRY_SEC.get(tf)
        if expiry is None:
            return
        key = f"{symbol}_{tf}"
        self._pending_confirmations[key] = {
            "direction": direction,
            "setup": setup,
            "expires": _t.time() + expiry,
        }
        # Limpiar entradas expiradas
        now = _t.time()
        self._pending_confirmations = {
            k: v for k, v in self._pending_confirmations.items()
            if now < v["expires"]
        }
        logger.info("Pending %s/%s %s registrado (%.0f min ventana)",
                    symbol, tf, direction, expiry / 60)

    # ---------------- Estrategia XAUUSD ----------------

    def _strategy_xauusd(
        self,
        symbol: str,
        tf: str,
        df: pd.DataFrame,
        patterns: dict[str, Any],
        indicators: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        XAUUSD: dos modos según ADX.
        TREND (ADX>=23): continuación de tendencia H4 con +DI/-DI + price action.
        RANGE (ADX 16-23): rebote en extremos Bollinger con RSI extremo.
        CHOP (ADX<16): skip.
        """
        close    = float(indicators.get("close", 0))
        atr      = float(indicators.get("atr", 0))
        rsi      = float(indicators.get("rsi", 50))
        adx      = float(indicators.get("adx", 20))
        plus_di  = float(indicators.get("plus_di", 20))
        minus_di = float(indicators.get("minus_di", 20))
        pct_b    = float(indicators.get("bb_percent_b", 0.5))
        e20      = float(indicators.get("ema20", 0))
        e50      = float(indicators.get("ema50", 0))
        mh       = float(indicators.get("macd_histogram", 0))
        mp       = float(indicators.get("macd_prev_hist", 0))

        pa    = patterns.get("price_action", {})
        pin   = pa.get("pin_bar", {})
        eng   = pa.get("engulfing", {})
        best_h = patterns.get("best_harmonic")

        # Sesión activa para TFs cortos
        if tf in ("M1", "M5", "M15"):
            session = self.get_session_name()
            if not any(s in session for s in ["London", "New York", "Overlap"]):
                return None

        # XAUUSD: solo opera en tendencia (ADX >= 25). Lateral = skip.
        if adx < 25:
            logger.info("XAUUSD/%s SKIP: ADX=%.1f — lateral, sin operación", tf, adx)
            return None

        mode = "TREND"
        direction: str | None = None
        setup_name = ""
        confirmations: list[str] = []

        if mode == "TREND":
            h4_trend, _ = self._trend_superior(symbol, "H4")

            # BUY: H4 alcista + +DI domina + RSI no sobrecomprado
            if h4_trend == "BULLISH" and plus_di > minus_di and rsi < 63:
                if best_h and best_h.get("found") and best_h.get("direction") == "BULLISH":
                    direction = "BULLISH"
                    setup_name = f"ORO Trend {best_h.get('name','Harmonic')}"
                    confirmations.append(f"Harmónico {best_h.get('name','')}")
                elif eng.get("found") and eng.get("direction") == "BULLISH" and eng.get("strength", 0) >= 38:
                    direction = "BULLISH"
                    setup_name = "ORO Trend Engulfing"
                    confirmations.append("Engulfing alcista")
                elif pin.get("found") and pin.get("direction") == "BULLISH" and pin.get("strength", 0) >= 38:
                    direction = "BULLISH"
                    setup_name = "ORO Trend Pin Bar"
                    confirmations.append("Pin bar alcista")

                if direction:
                    confirmations.append(f"ADX {adx:.0f} — tendencia real")
                    confirmations.append(f"+DI {plus_di:.0f} > -DI {minus_di:.0f}")
                    if close > e20 > e50:
                        confirmations.append("EMAs alineadas alcistas")
                    if mh > 0 and mh > mp:
                        confirmations.append("MACD acelerando alcista")
                    if rsi < 50:
                        confirmations.append(f"RSI {rsi:.0f} — impulso fresco")

            # SELL: H4 bajista + -DI domina + RSI no sobrevendido
            elif h4_trend == "BEARISH" and minus_di > plus_di and rsi > 37:
                if best_h and best_h.get("found") and best_h.get("direction") == "BEARISH":
                    direction = "BEARISH"
                    setup_name = f"ORO Trend {best_h.get('name','Harmonic')}"
                    confirmations.append(f"Harmónico {best_h.get('name','')}")
                elif eng.get("found") and eng.get("direction") == "BEARISH" and eng.get("strength", 0) >= 38:
                    direction = "BEARISH"
                    setup_name = "ORO Trend Engulfing"
                    confirmations.append("Engulfing bajista")
                elif pin.get("found") and pin.get("direction") == "BEARISH" and pin.get("strength", 0) >= 38:
                    direction = "BEARISH"
                    setup_name = "ORO Trend Pin Bar"
                    confirmations.append("Pin bar bajista")

                if direction:
                    confirmations.append(f"ADX {adx:.0f} — tendencia real")
                    confirmations.append(f"-DI {minus_di:.0f} > +DI {plus_di:.0f}")
                    if close < e20 < e50:
                        confirmations.append("EMAs alineadas bajistas")
                    if mh < 0 and mh < mp:
                        confirmations.append("MACD acelerando bajista")
                    if rsi > 50:
                        confirmations.append(f"RSI {rsi:.0f} — impulso bajista")

        if direction is None:
            return None

        # Filtro momentum en TFs cortos
        if tf in ("M1", "M5", "M15"):
            try:
                o3 = df["open"].iloc[-3:].values
                c3 = df["close"].iloc[-3:].values
                bears = sum(1 for o, c in zip(o3, c3) if c < o)
                bulls = sum(1 for o, c in zip(o3, c3) if c > o)
                if direction == "BULLISH" and bears >= 2:
                    return None
                if direction == "BEARISH" and bulls >= 2:
                    return None
            except Exception:
                pass

        # Alineación de tendencia — solo en TREND mode
        if mode == "TREND":
            alineado, razon = self._alineado_con_tendencia(symbol, direction, tf)
            if not alineado:
                logger.info("XAUUSD/%s %s bloqueado: %s", tf, direction, razon)
                return None

        # Confluencia: M5 requiere M15 activo, M1 requiere M5 activo
        conf_ok, conf_msg = self._check_confluencia(symbol, tf, direction)
        if not conf_ok:
            logger.info("XAUUSD/%s %s — %s", tf, direction, conf_msg)
            return None

        signal = self._build_signal(symbol, tf, close, direction, setup_name,
                                    confirmations, patterns, indicators, df, mode)
        if signal is not None:
            self._register_pending(symbol, tf, direction, setup_name)
        return signal

    # ---------------- Estrategia EURUSD ----------------

    def _strategy_eurusd(
        self,
        symbol: str,
        tf: str,
        df: pd.DataFrame,
        patterns: dict[str, Any],
        indicators: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        EURUSD: dos modos según ADX.
        TREND (ADX>=25): breakout con EMA cascade + +DI/-DI + MACD.
        RANGE (ADX 15-25): reversión en extremos Bollinger + RSI.
        CHOP (ADX<15): skip.
        """
        close    = float(indicators.get("close", 0))
        atr      = float(indicators.get("atr", 0))
        rsi      = float(indicators.get("rsi", 50))
        adx      = float(indicators.get("adx", 20))
        plus_di  = float(indicators.get("plus_di", 20))
        minus_di = float(indicators.get("minus_di", 20))
        pct_b    = float(indicators.get("bb_percent_b", 0.5))
        e20      = float(indicators.get("ema20", 0))
        e50      = float(indicators.get("ema50", 0))
        mh       = float(indicators.get("macd_histogram", 0))
        mp       = float(indicators.get("macd_prev_hist", 0))

        pa     = patterns.get("price_action", {})
        pin    = pa.get("pin_bar", {})
        eng    = pa.get("engulfing", {})
        best_h = patterns.get("best_harmonic")

        # Sesión activa para TFs cortos
        if tf in ("M1", "M5", "M15"):
            session = self.get_session_name()
            if not any(s in session for s in ["London", "New York", "Overlap"]):
                return None

        # ADX — filtro de chop
        if adx < 15:
            logger.info("EURUSD/%s SKIP: ADX=%.1f chop", tf, adx)
            return None

        mode = "TREND" if adx >= 25 else "RANGE"
        direction: str | None = None
        setup_name = ""
        confirmations: list[str] = []

        if mode == "TREND":
            di_margin = plus_di - minus_di

            # BUY: +DI domina por al menos 3pts + EMA cascade alcista + MACD positivo
            if di_margin >= 3 and close > e20 > e50 and mh > 0 and 38 <= rsi <= 63:
                if best_h and best_h.get("found") and best_h.get("direction") == "BULLISH":
                    direction = "BULLISH"
                    setup_name = f"EUR Trend {best_h.get('name','Harmonic')}"
                    confirmations.append(f"Harmónico {best_h.get('name','')}")
                elif eng.get("found") and eng.get("direction") == "BULLISH":
                    direction = "BULLISH"
                    setup_name = "EUR Trend Engulfing"
                    confirmations.append("Engulfing alcista")
                elif pin.get("found") and pin.get("direction") == "BULLISH":
                    direction = "BULLISH"
                    setup_name = "EUR Trend Pin Bar"
                    confirmations.append("Pin bar alcista")

                if direction:
                    confirmations.append(f"ADX {adx:.0f} — tendencia fuerte")
                    confirmations.append("EMA20 > EMA50 — estructura alcista")
                    confirmations.append("MACD positivo")
                    if mh > mp:
                        confirmations.append("MACD acelerando")

            # SELL: -DI domina + EMA cascade bajista + MACD negativo
            elif -di_margin >= 3 and close < e20 < e50 and mh < 0 and 37 <= rsi <= 62:
                if best_h and best_h.get("found") and best_h.get("direction") == "BEARISH":
                    direction = "BEARISH"
                    setup_name = f"EUR Trend {best_h.get('name','Harmonic')}"
                    confirmations.append(f"Harmónico {best_h.get('name','')}")
                elif eng.get("found") and eng.get("direction") == "BEARISH":
                    direction = "BEARISH"
                    setup_name = "EUR Trend Engulfing"
                    confirmations.append("Engulfing bajista")
                elif pin.get("found") and pin.get("direction") == "BEARISH":
                    direction = "BEARISH"
                    setup_name = "EUR Trend Pin Bar"
                    confirmations.append("Pin bar bajista")

                if direction:
                    confirmations.append(f"ADX {adx:.0f} — tendencia fuerte")
                    confirmations.append("EMA20 < EMA50 — estructura bajista")
                    confirmations.append("MACD negativo")
                    if mh < mp:
                        confirmations.append("MACD acelerando bajista")

        else:  # RANGE mode — reversión en extremos
            # BUY: banda inferior + RSI oversold
            if pct_b < 0.10 and rsi < 37:
                rev_pin = pin.get("found") and pin.get("direction") == "BULLISH" and pin.get("strength", 0) >= 30
                rev_eng = eng.get("found") and eng.get("direction") == "BULLISH" and eng.get("strength", 0) >= 30
                rev_hrm = best_h and best_h.get("found") and best_h.get("direction") == "BULLISH"
                if rev_pin or rev_eng or rev_hrm:
                    direction = "BULLISH"
                    if rev_hrm:
                        setup_name = f"EUR Range {best_h.get('name','Harmonic')}"
                        confirmations.append(f"Harmónico {best_h.get('name','')}")
                    else:
                        setup_name = "EUR Range Reversal BUY"
                        confirmations.append("Pin bar soporte" if rev_pin else "Engulfing soporte")
                    confirmations.append(f"RSI {rsi:.0f} — sobreventa")
                    confirmations.append("Banda inferior Bollinger")
                    confirmations.append(f"ADX {adx:.0f} — rango")

            # SELL: banda superior + RSI overbought
            elif pct_b > 0.90 and rsi > 63:
                rev_pin = pin.get("found") and pin.get("direction") == "BEARISH" and pin.get("strength", 0) >= 30
                rev_eng = eng.get("found") and eng.get("direction") == "BEARISH" and eng.get("strength", 0) >= 30
                rev_hrm = best_h and best_h.get("found") and best_h.get("direction") == "BEARISH"
                if rev_pin or rev_eng or rev_hrm:
                    direction = "BEARISH"
                    if rev_hrm:
                        setup_name = f"EUR Range {best_h.get('name','Harmonic')}"
                        confirmations.append(f"Harmónico {best_h.get('name','')}")
                    else:
                        setup_name = "EUR Range Reversal SELL"
                        confirmations.append("Pin bar resistencia" if rev_pin else "Engulfing resistencia")
                    confirmations.append(f"RSI {rsi:.0f} — sobrecompra")
                    confirmations.append("Banda superior Bollinger")
                    confirmations.append(f"ADX {adx:.0f} — rango")

        if direction is None:
            return None

        # Filtro momentum en TFs cortos
        if tf in ("M1", "M5", "M15"):
            try:
                o3 = df["open"].iloc[-3:].values
                c3 = df["close"].iloc[-3:].values
                bears = sum(1 for o, c in zip(o3, c3) if c < o)
                bulls = sum(1 for o, c in zip(o3, c3) if c > o)
                if direction == "BULLISH" and bears >= 2:
                    return None
                if direction == "BEARISH" and bulls >= 2:
                    return None
            except Exception:
                pass

        # Alineación de tendencia — solo en TREND mode
        if mode == "TREND":
            alineado, razon = self._alineado_con_tendencia(symbol, direction, tf)
            if not alineado:
                logger.info("EURUSD/%s %s bloqueado: %s", tf, direction, razon)
                return None

        # Confluencia: M5 requiere M15 activo, M1 requiere M5 activo
        conf_ok, conf_msg = self._check_confluencia(symbol, tf, direction)
        if not conf_ok:
            logger.info("EURUSD/%s %s — %s", tf, direction, conf_msg)
            return None

        signal = self._build_signal(symbol, tf, close, direction, setup_name,
                                    confirmations, patterns, indicators, df, mode)
        if signal is not None:
            self._register_pending(symbol, tf, direction, setup_name)
        return signal

    # ---------------- Signal construction (routing por símbolo) ----------------

    def calculate_signal(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        patterns: dict[str, Any],
        indicators: dict[str, Any],
    ) -> dict[str, Any] | None:
        if df is None or df.empty:
            return None
        if float(indicators.get("atr", 0.0) or 0.0) <= 0:
            return None

        sym_up = symbol.upper()
        if "XAU" in sym_up or "GOLD" in sym_up:
            return self._strategy_xauusd(symbol, timeframe, df, patterns, indicators)
        if "EUR" in sym_up and "USD" in sym_up:
            return self._strategy_eurusd(symbol, timeframe, df, patterns, indicators)
        # Fallback genérico para otros pares
        return self._strategy_generic(symbol, timeframe, df, patterns, indicators)

    def _strategy_generic(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        patterns: dict[str, Any],
        indicators: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Estrategia genérica para pares no especializados."""
        close    = float(indicators.get("close", 0))
        atr      = float(indicators.get("atr", 0))
        rsi      = float(indicators.get("rsi", 50))
        adx      = float(indicators.get("adx", 20))
        plus_di  = float(indicators.get("plus_di", 20))
        minus_di = float(indicators.get("minus_di", 20))
        e20      = float(indicators.get("ema20", 0))
        e50      = float(indicators.get("ema50", 0))
        mh       = float(indicators.get("macd_histogram", 0))
        mp       = float(indicators.get("macd_prev_hist", 0))

        if adx < 16:
            return None

        pa     = patterns.get("price_action", {})
        pin    = pa.get("pin_bar", {})
        eng    = pa.get("engulfing", {})
        best_h = patterns.get("best_harmonic")

        is_crypto = symbol.upper() in {"BTCUSD", "ETHUSD", "XRPUSD"}
        if timeframe in ("M1", "M5", "M15") and not is_crypto:
            # Filtro de sesión de liquidez forex — no aplica a cripto (24/7).
            session = self.get_session_name()
            if not any(s in session for s in ["London", "New York", "Overlap"]):
                return None

        direction: str | None = None
        setup_name = ""
        confirmations: list[str] = []

        # BUY
        if (plus_di > minus_di and close > e20 > e50 and mh > 0 and rsi < 65):
            if best_h and best_h.get("found") and best_h.get("direction") == "BULLISH":
                direction, setup_name = "BULLISH", f"Harmónico {best_h.get('name','')}"
                confirmations.append(setup_name)
            elif eng.get("found") and eng.get("direction") == "BULLISH":
                direction, setup_name = "BULLISH", "Engulfing alcista"
                confirmations.append(setup_name)
            elif pin.get("found") and pin.get("direction") == "BULLISH":
                direction, setup_name = "BULLISH", "Pin bar alcista"
                confirmations.append(setup_name)
            if direction:
                confirmations += [f"ADX {adx:.0f}", f"+DI {plus_di:.0f}>-DI {minus_di:.0f}",
                                   "EMAs alcistas", "MACD positivo"]

        # SELL
        elif (minus_di > plus_di and close < e20 < e50 and mh < 0 and rsi > 35):
            if best_h and best_h.get("found") and best_h.get("direction") == "BEARISH":
                direction, setup_name = "BEARISH", f"Harmónico {best_h.get('name','')}"
                confirmations.append(setup_name)
            elif eng.get("found") and eng.get("direction") == "BEARISH":
                direction, setup_name = "BEARISH", "Engulfing bajista"
                confirmations.append(setup_name)
            elif pin.get("found") and pin.get("direction") == "BEARISH":
                direction, setup_name = "BEARISH", "Pin bar bajista"
                confirmations.append(setup_name)
            if direction:
                confirmations += [f"ADX {adx:.0f}", f"-DI {minus_di:.0f}>+DI {plus_di:.0f}",
                                   "EMAs bajistas", "MACD negativo"]

        if direction is None:
            return None

        alineado, razon = self._alineado_con_tendencia(symbol, direction, timeframe)
        if not alineado:
            return None

        return self._build_signal(symbol, timeframe, close, direction, setup_name,
                                  confirmations, patterns, indicators, df, "TREND")

    # ---------------- Validation ----------------

    def validate_signal(
        self, signal: dict[str, Any], symbol: str
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        cfg = self.trading_cfg

        if signal.get("rr", 0) < float(cfg.get("min_rr", 1.8)):
            reasons.append(f"RR too low ({signal.get('rr')})")
        if signal.get("confidence", 0) < int(cfg.get("min_confidence", 52)):
            reasons.append(f"Confidence below threshold ({signal.get('confidence')})")
        if len(signal.get("confirmations", [])) < 1:
            reasons.append("Sin confirmaciones")

        if self.news.is_news_time(symbol, float(cfg.get("news_buffer_hours", 2))):
            reasons.append("High-impact news within buffer")

        # Spread: límite fijo por símbolo (más realista que cálculo ATR)
        _MAX_SPREAD_PIPS = {
            "XAUUSD": 12.0, "EURUSD": 3.0, "GBPUSD": 4.0,
            # Cripto: get_spread() devuelve USD crudo, no pips — umbrales en USD.
            "BTCUSD": 80.0, "ETHUSD": 5.0, "XRPUSD": 0.01,
        }
        try:
            spread = self.broker.get_spread(symbol)
            max_spread = _MAX_SPREAD_PIPS.get(symbol.upper(), 5.0) * float(cfg.get("max_spread_multiplier", 1.5))
            if spread > max_spread:
                reasons.append(f"Spread demasiado alto ({spread:.1f} pips, max {max_spread:.1f})")
        except Exception:
            pass

        active = self.db.get_active_signals()
        max_active = int(cfg.get("max_active_signals", 3))
        if len(active) >= max_active:
            reasons.append(f"Max active signals reached ({len(active)}/{max_active})")

        # Rechazar solo si hay señal ACTIVE en el MISMO símbolo+timeframe (no TP1/TP2
        # ya parcialmente cerradas). Antes bloqueaba por símbolo completo — con D1/H4
        # viviendo varios días, eso dejaba sin señales nuevas a los demás timeframes.
        tf = signal.get("timeframe", "")
        already_symbol_tf = any(
            s.get("symbol") == symbol and s.get("timeframe") == tf and s.get("status") == "ACTIVE"
            for s in active
        )
        if already_symbol_tf:
            reasons.append(f"Ya existe señal activa para {symbol}/{tf}")

        return (len(reasons) == 0, reasons)

    # ---------------- Per-symbol analysis ----------------

    def analyze_symbol(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        try:
            df = self.broker.get_ohlcv(symbol, timeframe, 250)
        except Exception as exc:
            logger.warning("get_ohlcv failed %s/%s: %s", symbol, timeframe, exc)
            return None
        if df is None or df.empty or len(df) < 50:
            return None
        indicators = get_all_indicators(df)
        if not indicators.get("valid"):
            return None
        patterns = analyze_patterns(df)
        signal = self.calculate_signal(symbol, timeframe, df, patterns, indicators)
        if signal is None:
            return None
        valid, reasons = self.validate_signal(signal, symbol)
        signal["valid"] = valid
        signal["validation_reasons"] = reasons
        if not valid:
            logger.info("Signal rejected %s/%s: %s", symbol, timeframe, reasons)
            return None
        return signal

    # ---------------- Signal Reviewer (anti-basura) ----------------

    # Historial: {(symbol, timeframe, direction): timestamp}
    _recent_signals: dict[tuple[str, str, str], float] = {}

    # Ventana anti-duplicado por timeframe (segundos) — misma dirección solamente
    _DUPLICATE_WINDOW_TF: dict[str, int] = {
        "M1": 900,    # 15 min
        "M5": 2700,   # 45 min
        "M15": 5400,  # 90 min
        "H1": 10800,  # 3 horas
        "H4": 43200,  # 12 horas
        "D1": 86400,  # 1 día
    }

    _MIN_CONFIDENCE: dict[str, int] = {
        "M1": 85, "M5": 85, "M15": 85, "H1": 82, "H4": 80, "D1": 80,
    }

    # Confluencia MTF: M5 solo entra si M15 lo confirma, M1 solo si M5 lo confirma
    _pending_confirmations: dict[str, dict] = {}
    _PENDING_EXPIRY_SEC: dict[str, int] = {
        "M15": 1800,  # 30 min — ventana para que M5 confirme
        "M5":   900,  # 15 min — ventana para que M1 entre
    }

    # Tiempo máximo que una señal puede estar ACTIVE antes de auto-expirar.
    # H4/D1 apuntan a objetivos de varios días (TP3 = 2-2.5x ATR del timeframe)
    # — expirarlas en 12h las mataba antes de que pudieran moverse. Ver
    # análisis 2026-06-18: 48/73 señales históricas se cancelaron en ~0 pips
    # por este límite, no por la estrategia en sí.
    _MAX_SIGNAL_AGE_MIN: dict[str, int] = {
        "M1": 20, "M5": 45, "M15": 90, "H1": 480, "H4": 2880, "D1": 10080,
    }

    # Límites ATR para SL estructural por TF: (min_mult, max_mult)
    # TFs altos → SL más ajustado para que los objetivos sean alcanzables
    _SL_ATR_BOUNDS: dict[str, tuple[float, float]] = {
        "M1":  (0.5, 1.5),
        "M5":  (0.5, 1.5),
        "M15": (0.5, 1.5),
        "H1":  (0.5, 1.5),
        "H4":  (0.4, 1.2),
        "D1":  (0.3, 0.9),
    }

    # Multiplicadores TP por TF: (tp1_mult, tp2_mult, tp3_mult)
    # TFs altos → ratios más conservadores (los movimientos en USD ya son grandes)
    _TP_MULTS: dict[str, tuple[float, float, float]] = {
        "M1":  (1.5, 2.5, 4.0),
        "M5":  (1.5, 2.5, 4.0),
        "M15": (1.5, 2.5, 4.0),
        "H1":  (1.2, 2.0, 3.0),
        "H4":  (1.0, 1.5, 2.5),
        "D1":  (1.0, 1.5, 2.0),
    }

    def review_signal(self, signal: dict[str, Any]) -> tuple[bool, str]:
        """
        Revisor de calidad antes de enviar a Telegram.
        Retorna (aprobada, motivo_rechazo).
        Pasa por 4 capas: reglas duras → coherencia técnica → duplicados → Ollama.
        """
        sym = signal.get("symbol", "")
        tf  = signal.get("timeframe", "M5")
        sig_type = signal.get("signal_type", "")
        is_buy = "BUY" in sig_type.upper()
        conf   = int(signal.get("confidence", 0))
        rr     = float(signal.get("rr", 0))
        entry  = float(signal.get("entry", 0))
        sl     = float(signal.get("sl", 0))
        notes  = signal.get("notes", "")

        # ── CAPA 1: Reglas duras ─────────────────────────────────────────────
        min_conf = self._MIN_CONFIDENCE.get(tf, 70)
        if conf < min_conf:
            return False, f"Confianza {conf}% < mínimo {min_conf}% para {tf}"
        if rr < 1.2:
            return False, f"RR {rr} demasiado bajo (mínimo 1.2)"
        if entry <= 0 or sl <= 0:
            return False, "Entrada o SL inválidos"
        if len(signal.get("confirmations", [])) < 2:
            return False, f"Confirmaciones insuficientes ({len(signal.get('confirmations', []))}/2 requeridas)"

        # ── CAPA 2: Coherencia técnica ───────────────────────────────────────
        rsi_val = signal.get("rsi")
        if rsi_val is not None:
            rsi_val = float(rsi_val)
            if is_buy and rsi_val > 72:
                return False, f"BUY con RSI={rsi_val:.0f} — sobrecompra (máx 72)"
            if not is_buy and rsi_val < 28:
                return False, f"SELL con RSI={rsi_val:.0f} — sobreventa (mín 28)"

        # ── Actualizar a precio actual y recalcular con riesgo estructural preservado ─
        got_price = False
        try:
            price_info = self.broker.get_current_price(sym)
            src = price_info.get("source", "?")
            current = (price_info.get("bid", 0) + price_info.get("ask", 0)) / 2
            if current > 0:
                got_price = True
                logger.info("Precio actual %s: %.5f [%s]", sym, current, src)

                # Preservar distancia de riesgo estructural (SL del swing original),
                # escalada por movimiento de precio desde la señal.
                original_risk = abs(entry - sl)
                price_scale = (current / entry) if entry > 0 else 1.0
                risk_adj = original_risk * price_scale
                if risk_adj <= 0:
                    return False, "Riesgo recalculado inválido"

                sym_u = sym.upper()
                if "XAU" in sym_u or sym_u in {"BTCUSD", "ETHUSD"}:
                    digits = 2
                elif sym_u == "XRPUSD":
                    digits = 4
                else:
                    digits = 5
                rnd = lambda v: round(float(v), digits)

                # TP ratios: 1.5:2.5:4.0 — TP1 ya supera el riesgo en 50%
                signal["entry"] = rnd(current)
                if is_buy:
                    signal["sl"]  = rnd(current - risk_adj)
                    signal["tp1"] = rnd(current + risk_adj * 1.5)
                    signal["tp2"] = rnd(current + risk_adj * 2.5)
                    signal["tp3"] = rnd(current + risk_adj * 4.0)
                else:
                    signal["sl"]  = rnd(current + risk_adj)
                    signal["tp1"] = rnd(current - risk_adj * 1.5)
                    signal["tp2"] = rnd(current - risk_adj * 2.5)
                    signal["tp3"] = rnd(current - risk_adj * 4.0)

                new_rr = round(abs(signal["tp3"] - current) / risk_adj, 2)
                if new_rr < 2.5:
                    return False, f"RR recalculado {new_rr:.2f} < 2.5"

                min_pip_target = int(self.trading_cfg.get("min_pip_target", 0))
                if min_pip_target > 0:
                    pips_tp3_calc = abs(signal["tp3"] - current) / _pip_size(sym)
                    if pips_tp3_calc < min_pip_target:
                        return False, f"TP3 a {pips_tp3_calc:.0f}p — grupo pide mín {min_pip_target}p"

                signal["rr"] = new_rr
                signal["current_price"] = rnd(current)
                signal["price_source"] = src
        except Exception as exc:
            logger.warning("review_signal recálculo error %s: %s", sym, exc)

        if not got_price:
            return False, f"No se pudo obtener precio actual para {sym}"

        # ── CAPA 3: Anti-duplicados — solo bloquea la misma dirección ──────────
        direction = "BUY" if is_buy else "SELL"
        now_ts = datetime.now(timezone.utc).timestamp()
        window = self._DUPLICATE_WINDOW_TF.get(tf, 3600)
        key = (sym, tf, direction)
        last_ts = self._recent_signals.get(key, 0)
        if now_ts - last_ts < window:
            mins_ago = int((now_ts - last_ts) / 60)
            return False, f"Duplicado {sym}/{tf}/{direction} (hace {mins_ago} min)"

        # ── CAPA 4: Ollama — solo si está habilitado Y responde rápido ───────
        if self.ollama_cfg.get("enabled", False):
            veredicto = self._ollama_review(signal)
            if veredicto == "RECHAZAR":
                return False, "Ollama rechazó la señal"

        # ── APROBADA ─────────────────────────────────────────────────────────
        key = (sym, tf, direction)
        self._recent_signals[key] = now_ts
        max_window = max(self._DUPLICATE_WINDOW_TF.values())
        self._recent_signals = {
            k: v for k, v in self._recent_signals.items()
            if now_ts - v < max_window
        }
        return True, "OK"

    def _ollama_review(self, signal: dict[str, Any]) -> str:
        """
        Pregunta a Ollama si la señal es válida.
        Responde exactamente APROBAR o RECHAZAR.
        Timeout 8s — si no responde, aprueba por defecto.
        """
        host  = self.ollama_cfg.get("host", "http://localhost:11434")
        model = self.ollama_cfg.get("model", "llama3.2:3b")
        prompt = (
            f"Eres un revisor de señales de trading. Analiza esta señal y responde SOLO con "
            f"APROBAR o RECHAZAR (una sola palabra).\n\n"
            f"Señal: {signal.get('signal_type')} {signal.get('symbol')} {signal.get('timeframe')}\n"
            f"Setup: {signal.get('setup')}\n"
            f"Entrada: {signal.get('entry')} | SL: {signal.get('sl')} | "
            f"TP3: {signal.get('tp3')} | RR: {signal.get('rr')}\n"
            f"Confianza: {signal.get('confidence')}%\n"
            f"Confirmaciones: {signal.get('notes')}\n"
            f"Sesión: {signal.get('session')}\n"
            f"Noticias: {signal.get('news_summary', 'ninguna')}\n\n"
            f"RECHAZAR si: RR < 2, confirmaciones débiles, noticias de alto impacto próximas, "
            f"sesión de baja liquidez, setup poco claro.\n"
            f"APROBAR si: todo parece sólido y accionable.\n"
            f"Responde solo APROBAR o RECHAZAR:"
        )
        try:
            resp = requests.post(
                f"{host}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=8,
            )
            if resp.status_code == 200:
                text = resp.json().get("response", "").strip().upper()
                if "RECHAZAR" in text:
                    logger.info("Ollama RECHAZÓ señal %s/%s", signal.get("symbol"), signal.get("timeframe"))
                    return "RECHAZAR"
        except Exception as exc:
            logger.debug("Ollama review timeout/error: %s — aprobando por defecto", exc)
        return "APROBAR"

    # ---------------- Scan all symbols ----------------

    def scan_all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        symbols = self._active_symbols()
        timeframes = self.trading_cfg.get("timeframes", [])
        # D1→H4→H1→M15 primero para que registren pending antes de que M5/M1 lean
        _order = {"D1": 0, "H4": 1, "H1": 2, "M15": 3, "M5": 4, "M1": 5}
        timeframes_sorted = sorted(timeframes, key=lambda x: _order.get(x, 9))
        for sym in symbols:
            for tf in timeframes_sorted:
                try:
                    sig = self.analyze_symbol(sym, tf)
                except Exception as exc:
                    logger.exception("analyze_symbol crashed para %s/%s: %s", sym, tf, exc)
                    continue
                if sig is None:
                    continue
                try:
                    aprobada, motivo = self.review_signal(sig)
                    sig["review_ok"] = aprobada
                    sig["review_reason"] = motivo
                    if not aprobada:
                        logger.info("SEÑAL BLOQUEADA %s/%s — %s", sym, tf, motivo)
                        continue
                    sid = self.db.save_signal(sig)
                    sig["id"] = sid
                    out.append(sig)
                    try:
                        msg_id = self.telegram.send_signal_tracked(self.config, sig)
                        if msg_id and sid:
                            self.db.save_message_id(sid, msg_id)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.warning("Error procesando señal %s/%s: %s", sym, tf, exc)
        return out

    # ---------------- Análisis de momentum para guía TP ----------------

    def _momentum_hold_recommendation(
        self, signal: dict[str, Any], current_price: float
    ) -> tuple[bool, str]:
        """
        Analiza si el momentum actual justifica aguantar hasta TP3 en lugar
        de cerrar parciales. Retorna (aguantar: bool, razón: str).
        Puntuación 0-5: >=3 → aguantar, <3 → cierre parcial estándar.
        """
        symbol  = signal.get("symbol", "")
        tf      = signal.get("timeframe", "H1")
        is_buy  = "BUY" in signal.get("signal_type", "")
        tp3     = float(signal.get("tp3", 0))
        atr     = float(signal.get("atr", 0) or 0)

        try:
            df = self.broker.get_ohlcv(symbol, tf, 50)
            if df is None or df.empty or len(df) < 20:
                return False, "sin datos suficientes"

            indics = get_all_indicators(df)
            rsi        = float(indics.get("rsi", 50))
            macd_hist  = float(indics.get("macd_histogram", 0))
            macd_prev  = float(indics.get("macd_prev_hist", 0))
            e20        = float(indics.get("ema20", 0))
            e50        = float(indics.get("ema50", 0))

            score = 0
            factores: list[str] = []

            # 1. RSI: momentum sin agotamiento
            if is_buy:
                if 45 < rsi < 68:
                    score += 1
                    factores.append(f"RSI {rsi:.0f} con fuerza alcista")
                elif rsi >= 68:
                    factores.append(f"RSI {rsi:.0f} — sobrecompra")
            else:
                if 32 < rsi < 55:
                    score += 1
                    factores.append(f"RSI {rsi:.0f} con fuerza bajista")
                elif rsi <= 32:
                    factores.append(f"RSI {rsi:.0f} — sobreventa")

            # 2. MACD acelerando en la dirección
            if is_buy and macd_hist > 0 and macd_hist > macd_prev:
                score += 1
                factores.append("MACD acelerando alcista")
            elif not is_buy and macd_hist < 0 and macd_hist < macd_prev:
                score += 1
                factores.append("MACD acelerando bajista")

            # 3. EMAs alineadas a favor
            if is_buy and e20 > e50 and current_price > e20:
                score += 1
                factores.append("EMAs alineadas alcista")
            elif not is_buy and e20 < e50 and current_price < e20:
                score += 1
                factores.append("EMAs alineadas bajista")

            # 4. Sesión con liquidez
            session = self.get_session_name()
            if "Overlap" in session:
                score += 2
                factores.append("Overlap Londres+NY (máxima liquidez)")
            elif "London" in session or "New York" in session:
                score += 1
                factores.append(f"Sesión {session} activa")

            # 5. TP3 alcanzable: distancia < 6× ATR
            if tp3 > 0 and atr > 0:
                dist_tp3 = abs(current_price - tp3)
                if dist_tp3 < 6 * atr:
                    score += 1
                    pips_tp3 = round(dist_tp3 / _pip_size(symbol), 0)
                    factores.append(f"TP3 a solo {pips_tp3:.0f} pips")
                else:
                    factores.append("TP3 lejos — reconsiderar")

            aguantar = score >= 3
            resumen = " | ".join(factores) if factores else "análisis neutral"
            return aguantar, resumen

        except Exception as exc:
            logger.debug("momentum_hold_recommendation error: %s", exc)
            return False, "error en análisis"

    # ---------------- Active signal monitoring ----------------

    def check_partial_closes(
        self, signal: dict[str, Any], current_price: float
    ) -> None:
        sig_id = int(signal["id"])
        symbol = signal["symbol"]
        is_buy = "BUY" in signal["signal_type"].upper()
        status = signal["status"]
        entry = float(signal["entry"])
        tp1 = float(signal["tp1"])
        tp2 = float(signal["tp2"])
        tp3 = float(signal["tp3"])
        sl = float(signal["sl"])
        pip = _pip_size(symbol)

        def pips(diff: float) -> float:
            return round(diff / pip, 2) if pip > 0 else round(diff, 2)

        # Obtener message_id para threading (reply_to)
        try:
            reply_id = self.db.get_message_id(sig_id)
        except Exception:
            reply_id = None

        # Stop loss
        if (is_buy and current_price <= sl) or ((not is_buy) and current_price >= sl):
            pnl = pips((sl - entry) if is_buy else (entry - sl))
            self.db.update_signal_status(sig_id, "SL", pnl_pips=pnl, notes="Hit stop loss")
            try:
                self.telegram.send_sl_update(self.config, signal, pnl, reply_to_message_id=reply_id)
            except Exception:
                pass
            self._immediate_rescan(symbol)
            return

        # Targets
        if is_buy:
            if status == "ACTIVE" and current_price >= tp1:
                pnl = pips(tp1 - entry)
                self.db.update_signal_status(sig_id, "TP1", pnl_pips=pnl, notes="TP1 reached")
                hold, razon = self._momentum_hold_recommendation(signal, current_price)
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP1", pnl, hold_rec=hold, hold_reason=razon, reply_to_message_id=reply_id)
                except Exception:
                    pass
                return
            if status == "TP1" and current_price >= tp2:
                pnl = pips(tp2 - entry)
                self.db.update_signal_status(sig_id, "TP2", pnl_pips=pnl, notes="TP2 reached")
                hold, razon = self._momentum_hold_recommendation(signal, current_price)
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP2", pnl, hold_rec=hold, hold_reason=razon, reply_to_message_id=reply_id)
                except Exception:
                    pass
                return
            if status == "TP2" and current_price >= tp3:
                pnl = pips(tp3 - entry)
                self.db.update_signal_status(sig_id, "TP3", pnl_pips=pnl, notes="TP3 reached")
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP3", pnl, reply_to_message_id=reply_id)
                except Exception:
                    pass
                self._immediate_rescan(symbol)
                return
        else:
            if status == "ACTIVE" and current_price <= tp1:
                pnl = pips(entry - tp1)
                self.db.update_signal_status(sig_id, "TP1", pnl_pips=pnl, notes="TP1 reached")
                hold, razon = self._momentum_hold_recommendation(signal, current_price)
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP1", pnl, hold_rec=hold, hold_reason=razon, reply_to_message_id=reply_id)
                except Exception:
                    pass
                return
            if status == "TP1" and current_price <= tp2:
                pnl = pips(entry - tp2)
                self.db.update_signal_status(sig_id, "TP2", pnl_pips=pnl, notes="TP2 reached")
                hold, razon = self._momentum_hold_recommendation(signal, current_price)
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP2", pnl, hold_rec=hold, hold_reason=razon, reply_to_message_id=reply_id)
                except Exception:
                    pass
                return
            if status == "TP2" and current_price <= tp3:
                pnl = pips(entry - tp3)
                self.db.update_signal_status(sig_id, "TP3", pnl_pips=pnl, notes="TP3 reached")
                try:
                    self.telegram.send_tp_update(self.config, signal, "TP3", pnl, reply_to_message_id=reply_id)
                except Exception:
                    pass
                self._immediate_rescan(symbol)
                return

    # Intervalos de seguimiento por timeframe (segundos entre mensajes)
    _TRACKING_INTERVAL_SEC: dict[str, int] = {
        "M1": 120, "M5": 300, "M15": 600, "H1": 1200, "H4": 3600, "D1": 10800,
    }
    _last_tracking_sent: dict[int, float] = {}

    def calculate_news_signal(
        self, symbol: str, direction: str, news_explanation: str
    ) -> dict[str, Any] | None:
        """Señal de entrada rápida M5 por impulso de noticia macro."""
        try:
            df = self.broker.get_ohlcv(symbol, "M5", 100)
            if df is None or df.empty or len(df) < 20:
                return None
            indicators = get_all_indicators(df)
            if not indicators.get("valid"):
                return None
            atr = float(indicators.get("atr", 0) or 0)
            if atr <= 0:
                return None

            close = float(df["close"].iloc[-1])
            signal_type = "BUY" if direction == "BULLISH" else "SELL"

            # SL ajustado (1×ATR), TPs ampliados (1.5/3/5×ATR) para capturar impulso
            if direction == "BULLISH":
                sl  = close - 1.0 * atr
                tp1 = close + 1.5 * atr
                tp2 = close + 3.0 * atr
                tp3 = close + 5.0 * atr
            else:
                sl  = close + 1.0 * atr
                tp1 = close - 1.5 * atr
                tp2 = close - 3.0 * atr
                tp3 = close - 5.0 * atr

            risk   = abs(close - sl)
            reward = abs(tp3 - close)
            rr     = round(reward / risk, 2) if risk > 0 else 0.0
            digits = 2 if "XAU" in symbol.upper() else 5
            rnd    = lambda v: round(float(v), digits)

            return {
                "symbol":        symbol,
                "timeframe":     "M5",
                "signal_type":   signal_type,
                "setup":         "NEWS IMPULSE",
                "entry":         rnd(close),
                "tp1":           rnd(tp1),
                "tp2":           rnd(tp2),
                "tp3":           rnd(tp3),
                "sl":            rnd(sl),
                "rr":            rr,
                "atr":           round(atr, 4),
                "confidence":    82,
                "status":        "ACTIVE",
                "created_at":    datetime.now(timezone.utc).isoformat(),
                "notes":         news_explanation,
                "confirmations": ["NEWS IMPULSE", "Dato macro publicado", news_explanation[:60]],
                "news_summary":  news_explanation,
                "session":       self.get_session_name(),
            }
        except Exception as exc:
            logger.warning("calculate_news_signal error %s: %s", symbol, exc)
            return None

    def _immediate_rescan(self, symbol: str) -> None:
        """Resumen del cierre + nueva señal si existe."""
        # 1. Enviar resultado de la señal que acaba de cerrar
        try:
            last = self.db.get_last_closed_signal(symbol)
            if last:
                self.telegram.send_result_summary(self.config, last)
        except Exception:
            pass
        # 2. Buscar siguiente señal — solo si el símbolo sigue activo hoy
        #    (evita que una EURUSD vieja siga regenerándose tras quitarla
        #    del set entre semana, o que cripto se cuele fuera de fin de semana).
        if symbol.upper() not in {s.upper() for s in self._active_symbols()}:
            logger.info("Rescan omitido para %s — no está en los símbolos activos hoy", symbol)
            return
        try:
            timeframes = self.trading_cfg.get("timeframes", ["H1", "H4"])
            for tf in timeframes:
                sig = self.analyze_symbol(symbol, tf)
                if sig is None:
                    continue
                aprobada, motivo = self.review_signal(sig)
                if not aprobada:
                    continue
                sid = self.db.save_signal(sig)
                sig["id"] = sid
                self.telegram.send_signal(self.config, sig)
                logger.info("Rescan post-cierre: nueva señal %s/%s enviada", symbol, tf)
                return
        except Exception as exc:
            logger.debug("Rescan inmediato error %s: %s", symbol, exc)

    def monitor_active_signals(self) -> None:
        now = datetime.now(timezone.utc)
        active = self.db.get_active_signals()
        universo = {s.upper() for s in self.trading_cfg.get("symbols", [])} | \
                   {s.upper() for s in self.trading_cfg.get("weekend_symbols", [])}
        for sig in active:
            try:
                tf = sig.get("timeframe", "H1")
                symbol = sig.get("symbol", "")
                sig_id = int(sig.get("id", 0))

                # 0. Símbolo retirado por completo de la configuración (ej. EURUSD
                #    quitado del bot) — cancelar de inmediato, no esperar la edad máxima.
                if universo and symbol.upper() not in universo:
                    self.db.update_signal_status(
                        sig_id, "CANCELLED",
                        notes=f"{symbol} ya no está en la configuración del bot",
                    )
                    logger.info("Señal %s (%s) cancelada — símbolo retirado de config", sig_id, symbol)
                    try:
                        last = self.db.get_last_closed_signal(symbol)
                        if last:
                            self.telegram.send_result_summary(self.config, last)
                    except Exception:
                        pass
                    continue

                # 1. Expirar por edad máxima
                max_age_min = self._MAX_SIGNAL_AGE_MIN.get(tf, 240)
                try:
                    created = datetime.fromisoformat(sig["created_at"].replace("Z", "+00:00"))
                    age_min = (now - created).total_seconds() / 60
                    if age_min > max_age_min:
                        self.db.update_signal_status(
                            sig_id, "CANCELLED",
                            notes=f"Auto-expirada por edad ({int(age_min)} min > {max_age_min})",
                        )
                        logger.info("Auto-expirada %s %s/%s — edad %d min", sig_id, symbol, tf, int(age_min))
                        self._immediate_rescan(symbol)
                        continue
                except Exception:
                    pass

                # 2. Obtener precio actual
                price_info = self.broker.get_current_price(symbol)
                price = (price_info.get("bid", 0) + price_info.get("ask", 0)) / 2 or price_info.get("bid", 0)
                if price <= 0:
                    continue

                # 3. Cancelar si el precio se alejó demasiado de la entrada (>3 ATR)
                entry = float(sig.get("entry", 0))
                atr = float(sig.get("atr", 0) or 0)
                if entry > 0 and atr > 0:
                    drift = abs(price - entry)
                    max_drift = 3.0 * atr
                    if drift > max_drift:
                        pips = round(drift / _pip_size(symbol), 1)
                        self.db.update_signal_status(
                            sig_id, "CANCELLED",
                            notes=f"Precio derivó {pips} pips de la entrada ({entry:.4g}) — zona inválida",
                        )
                        logger.info("Señal %s cancelada — drift %s pips (3× ATR)", sig_id, pips)
                        self._immediate_rescan(symbol)
                        continue

                # 4. Seguimiento periódico (no aplica para M1/M5 — se resuelven solos)
                if tf in ("M1", "M5"):
                    self.check_partial_closes(sig, price)
                    continue

                sig_id_int = int(sig_id)
                interval = self._TRACKING_INTERVAL_SEC.get(tf, 1200)
                last_track = self._last_tracking_sent.get(sig_id_int, 0)
                if now.timestamp() - last_track >= interval:
                    try:
                        self.telegram.send_tracking_update(self.config, sig, price)
                        self._last_tracking_sent[sig_id_int] = now.timestamp()
                    except Exception:
                        pass

                self.check_partial_closes(sig, price)
            except Exception as exc:
                logger.warning("Monitor failed for signal %s: %s", sig.get("id"), exc)

    # ---------------- Debug / Diagnóstico ----------------

    def debug_scan(self) -> list[dict[str, Any]]:
        """Retorna diagnóstico completo por símbolo/timeframe sin filtrar señales."""
        out: list[dict[str, Any]] = []
        symbols = self._active_symbols()
        timeframes = self.trading_cfg.get("timeframes", [])
        for sym in symbols:
            for tf in timeframes:
                diag: dict[str, Any] = {"symbol": sym, "timeframe": tf}
                try:
                    df = self.broker.get_ohlcv(sym, tf, 250)
                    if df is None or df.empty or len(df) < 50:
                        diag["status"] = "sin_datos"
                        diag["filas"] = int(len(df)) if df is not None else 0
                        out.append(diag)
                        continue
                    diag["filas"] = len(df)
                    indicators = get_all_indicators(df)
                    diag["indicadores_ok"] = indicators.get("valid", False)
                    diag["rsi"] = round(float(indicators.get("rsi", 0)), 1)
                    diag["atr"] = round(float(indicators.get("atr", 0)), 4)
                    diag["close"] = round(float(indicators.get("close", 0)), 2)
                    patterns = analyze_patterns(df)
                    pa = patterns.get("price_action", {})
                    diag["pin_bar"] = pa.get("pin_bar", {}).get("found", False)
                    diag["engulfing"] = pa.get("engulfing", {}).get("found", False)
                    brk = pa.get("breakout", {})
                    diag["breakout"] = brk.get("found", False)
                    diag["breakout_dir"] = brk.get("direction") if brk.get("found") else None
                    diag["harmonic"] = bool((patterns.get("best_harmonic") or {}).get("found"))
                    signal = self.calculate_signal(sym, tf, df, patterns, indicators)
                    if signal is None:
                        diag["status"] = "sin_señal"
                        diag["razon"] = "no se detectó patrón/dirección"
                        out.append(diag)
                        continue
                    diag["signal_type"] = signal.get("signal_type")
                    diag["confidence"] = signal.get("confidence")
                    diag["rr"] = signal.get("rr")
                    diag["setup"] = signal.get("setup")
                    diag["confirmaciones"] = signal.get("confirmations", [])
                    valid, reasons = self.validate_signal(signal, sym)
                    diag["validate_ok"] = valid
                    diag["validate_razones"] = reasons
                    if valid:
                        # Revisar sin modificar _recent_signals (solo diagnóstico)
                        conf = int(signal.get("confidence", 0))
                        rr = float(signal.get("rr", 0))
                        min_conf = self._MIN_CONFIDENCE.get(tf, 70)
                        if conf < min_conf:
                            diag["review_ok"] = False
                            diag["review_razon"] = f"Confianza {conf}% < mínimo {min_conf}% para {tf}"
                            diag["status"] = "BLOQUEADA_REVIEW"
                        elif rr < 1.5:
                            diag["review_ok"] = False
                            diag["review_razon"] = f"RR {rr} < 1.5"
                            diag["status"] = "BLOQUEADA_REVIEW"
                        else:
                            is_buy = "BUY" in signal.get("signal_type", "")
                            direction = "BUY" if is_buy else "SELL"
                            key = (sym, tf, direction)
                            import time as _time_mod
                            now = _time_mod.time()
                            last_ts = self._recent_signals.get(key, 0)
                            if now - last_ts < self._DUPLICATE_WINDOW_TF.get(tf, 3600):
                                mins_ago = int((now - last_ts) / 60)
                                diag["review_ok"] = False
                                diag["review_razon"] = f"Duplicado — hace {mins_ago} min"
                                diag["status"] = "BLOQUEADA_DUPLICADO"
                            else:
                                diag["review_ok"] = True
                                diag["review_razon"] = "pasaría revisión"
                                diag["status"] = "LISTA_PARA_ENVIAR"
                    else:
                        diag["review_ok"] = False
                        diag["review_razon"] = "omitido (validate falló)"
                        diag["status"] = "BLOQUEADA_VALIDATE"
                except Exception as exc:
                    diag["status"] = "error"
                    diag["error"] = str(exc)
                out.append(diag)
        return out

    # ---------------- Ollama ----------------

    def analyze_with_ollama(self, symbol: str, analysis_data: dict[str, Any]) -> str:
        if not self.ollama_cfg.get("enabled", False):
            return ""
        host = self.ollama_cfg.get("host", "http://localhost:11434")
        model = self.ollama_cfg.get("model", "llama3.2:3b")
        prompt = (
            f"You are a trading analyst. Symbol: {symbol}. "
            f"Data: {analysis_data}. "
            "Provide a concise 3-sentence analysis: trend, key risk, suggested bias."
        )
        try:
            resp = requests.post(
                f"{host}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return str(data.get("response", "")).strip()
        except Exception as exc:
            logger.info("Ollama analysis unavailable: %s", exc)
        return ""
