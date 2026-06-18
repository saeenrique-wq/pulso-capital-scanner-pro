"""Backtesting module — analiza historial de señales."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import database as db


def run_backtest(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    Analiza el historial de señales cerradas y retorna métricas de rendimiento.
    Filtros opcionales por symbol y timeframe.
    """
    signals = db.get_signal_history(limit)

    # Filtrar solo señales cerradas
    closed = [
        s for s in signals
        if s.get("status") in ("TP1", "TP2", "TP3", "SL", "CANCELLED")
        and (symbol is None or s.get("symbol", "").upper() == symbol.upper())
        and (timeframe is None or s.get("timeframe", "") == timeframe)
    ]

    if not closed:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pips": 0.0,
            "avg_win_pips": 0.0,
            "avg_loss_pips": 0.0,
            "best_trade_pips": 0.0,
            "worst_trade_pips": 0.0,
            "by_symbol": {},
            "by_timeframe": {},
            "by_hour": {},
            "by_setup": {},
        }

    wins       = [s for s in closed if s.get("status") in ("TP1", "TP2", "TP3")]
    losses     = [s for s in closed if s.get("status") == "SL"]
    breakevens = [s for s in closed if s.get("status") == "CANCELLED"]

    gross_profit = sum(float(s.get("pnl_pips", 0) or 0) for s in wins)
    gross_loss   = abs(sum(float(s.get("pnl_pips", 0) or 0) for s in losses))
    total_pips   = sum(float(s.get("pnl_pips", 0) or 0) for s in closed)

    n_wins   = len(wins)
    n_losses = len(losses)
    n_closed = n_wins + n_losses

    win_rate       = (n_wins / n_closed * 100) if n_closed else 0.0
    profit_factor  = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    avg_win_pips   = (gross_profit / n_wins)   if n_wins   else 0.0
    avg_loss_pips  = (gross_loss   / n_losses) if n_losses else 0.0

    pips_list = [float(s.get("pnl_pips", 0) or 0) for s in closed]
    best_trade  = max(pips_list) if pips_list else 0.0
    worst_trade = min(pips_list) if pips_list else 0.0

    # Desglose por símbolo
    by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pips": 0.0})
    for s in closed:
        sym = s.get("symbol", "?")
        pnl = float(s.get("pnl_pips", 0) or 0)
        if s.get("status") in ("TP1", "TP2", "TP3"):
            by_symbol[sym]["wins"] += 1
        elif s.get("status") == "SL":
            by_symbol[sym]["losses"] += 1
        by_symbol[sym]["pips"] = round(by_symbol[sym]["pips"] + pnl, 2)

    # Desglose por timeframe
    by_tf: dict[str, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pips": 0.0})
    for s in closed:
        tf  = s.get("timeframe", "?")
        pnl = float(s.get("pnl_pips", 0) or 0)
        if s.get("status") in ("TP1", "TP2", "TP3"):
            by_tf[tf]["wins"] += 1
        elif s.get("status") == "SL":
            by_tf[tf]["losses"] += 1
        by_tf[tf]["pips"] = round(by_tf[tf]["pips"] + pnl, 2)

    # Desglose por hora UTC de creación
    by_hour: dict[str, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pips": 0.0})
    for s in closed:
        try:
            dt  = datetime.fromisoformat(str(s.get("created_at", "")).replace("Z", "+00:00"))
            hh  = f"{dt.hour:02d}:00"
        except Exception:
            hh = "??"
        pnl = float(s.get("pnl_pips", 0) or 0)
        if s.get("status") in ("TP1", "TP2", "TP3"):
            by_hour[hh]["wins"] += 1
        elif s.get("status") == "SL":
            by_hour[hh]["losses"] += 1
        by_hour[hh]["pips"] = round(by_hour[hh]["pips"] + pnl, 2)

    # Desglose por setup
    by_setup: dict[str, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pips": 0.0})
    for s in closed:
        setup = s.get("setup", "?") or "?"
        pnl   = float(s.get("pnl_pips", 0) or 0)
        if s.get("status") in ("TP1", "TP2", "TP3"):
            by_setup[setup]["wins"] += 1
        elif s.get("status") == "SL":
            by_setup[setup]["losses"] += 1
        by_setup[setup]["pips"] = round(by_setup[setup]["pips"] + pnl, 2)

    # Calcular win_rate en cada subgrupo
    def _wr(group: dict[str, Any]) -> float:
        n = group["wins"] + group["losses"]
        return round(group["wins"] / n * 100, 1) if n else 0.0

    for g in (by_symbol, by_tf, by_hour, by_setup):
        for v in g.values():
            v["win_rate"] = _wr(v)

    return {
        "total":           len(closed),
        "wins":            n_wins,
        "losses":          n_losses,
        "breakeven":       len(breakevens),
        "win_rate":        round(win_rate, 2),
        "profit_factor":   round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
        "total_pips":      round(total_pips, 2),
        "gross_profit":    round(gross_profit, 2),
        "gross_loss":      round(gross_loss, 2),
        "avg_win_pips":    round(avg_win_pips, 2),
        "avg_loss_pips":   round(avg_loss_pips, 2),
        "best_trade_pips": round(best_trade, 2),
        "worst_trade_pips": round(worst_trade, 2),
        "by_symbol":       dict(by_symbol),
        "by_timeframe":    dict(by_tf),
        "by_hour":         dict(sorted(by_hour.items())),
        "by_setup":        dict(by_setup),
    }
