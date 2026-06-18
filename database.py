"""SQLite database layer for the trading scanner."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "scanner.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            setup TEXT NOT NULL,
            entry REAL NOT NULL,
            tp1 REAL NOT NULL,
            tp2 REAL NOT NULL,
            tp3 REAL NOT NULL,
            sl REAL NOT NULL,
            rr REAL NOT NULL,
            confidence INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            closed_at TEXT,
            pnl_pips REAL DEFAULT 0,
            notes TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS market_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL DEFAULT 0,
            UNIQUE(symbol, timeframe, timestamp)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            currency TEXT NOT NULL,
            impact TEXT NOT NULL,
            event_time TEXT NOT NULL,
            actual TEXT,
            forecast TEXT,
            previous TEXT,
            UNIQUE(title, event_time, currency)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            total_signals INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            breakeven INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            total_pips REAL DEFAULT 0
        )
        """
    )

    # Migración: agregar telegram_message_id si no existe
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN telegram_message_id INTEGER DEFAULT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # columna ya existe

    conn.commit()
    conn.close()


def save_message_id(signal_id: int, message_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE signals SET telegram_message_id=? WHERE id=?",
        (message_id, signal_id),
    )
    conn.commit()
    conn.close()


def get_message_id(signal_id: int) -> int | None:
    conn = get_conn()
    cur = conn.execute(
        "SELECT telegram_message_id FROM signals WHERE id=?", (signal_id,)
    )
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return int(row[0])
    return None


def save_signal(signal: dict[str, Any]) -> int:
    """Insert a new signal and return its id."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signals (
            symbol, timeframe, signal_type, setup, entry, tp1, tp2, tp3,
            sl, rr, confidence, status, created_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal["symbol"],
            signal["timeframe"],
            signal["signal_type"],
            signal["setup"],
            float(signal["entry"]),
            float(signal["tp1"]),
            float(signal["tp2"]),
            float(signal["tp3"]),
            float(signal["sl"]),
            float(signal["rr"]),
            int(signal.get("confidence", 0)),
            signal.get("status", "ACTIVE"),
            signal.get("created_at", datetime.now(timezone.utc).isoformat()),
            signal.get("notes", ""),
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(new_id) if new_id else 0


def update_signal_status(
    signal_id: int,
    status: str,
    pnl_pips: float = 0.0,
    notes: str | None = None,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    closed_at = None
    if status in {"TP3", "SL", "CANCELLED"}:
        closed_at = datetime.now(timezone.utc).isoformat()

    # Al cancelar con pnl=0, preservar el pnl ya registrado (ej: señal en TP1 que expiró)
    if status == "CANCELLED" and pnl_pips == 0.0:
        pnl_sql = "pnl_pips"  # no tocar
        if notes is not None:
            cur.execute(
                """UPDATE signals SET status=?, closed_at=COALESCE(?, closed_at),
                   notes=? WHERE id=?""",
                (status, closed_at, notes, signal_id),
            )
        else:
            cur.execute(
                """UPDATE signals SET status=?, closed_at=COALESCE(?, closed_at)
                   WHERE id=?""",
                (status, closed_at, signal_id),
            )
    elif notes is not None:
        cur.execute(
            """UPDATE signals SET status=?, pnl_pips=?, closed_at=COALESCE(?, closed_at),
               notes=? WHERE id=?""",
            (status, pnl_pips, closed_at, notes, signal_id),
        )
    else:
        cur.execute(
            """UPDATE signals SET status=?, pnl_pips=?, closed_at=COALESCE(?, closed_at)
               WHERE id=?""",
            (status, pnl_pips, closed_at, signal_id),
        )
    conn.commit()
    conn.close()


def get_active_signals() -> list[dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM signals WHERE status IN ('ACTIVE', 'TP1', 'TP2')
           ORDER BY created_at DESC"""
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_last_closed_signal(symbol: str) -> dict[str, Any] | None:
    """Retorna la última señal completamente cerrada (TP3/SL/CANCELLED) de un símbolo."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM signals
           WHERE symbol=? AND status IN ('TP3','SL','CANCELLED')
           ORDER BY COALESCE(closed_at, created_at) DESC LIMIT 1""",
        (symbol,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_signal_history(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_news(event: dict[str, Any]) -> None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT OR IGNORE INTO news_events
               (title, currency, impact, event_time, actual, forecast, previous)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.get("title", ""),
                event.get("currency", ""),
                event.get("impact", ""),
                event.get("event_time", ""),
                event.get("actual"),
                event.get("forecast"),
                event.get("previous"),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def get_upcoming_news(hours: int = 4) -> list[dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    cur.execute(
        """SELECT * FROM news_events
           WHERE event_time >= ? AND event_time <= ?
           ORDER BY event_time ASC""",
        (now.isoformat(), horizon.isoformat()),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def update_performance() -> dict[str, Any]:
    """Aggregate signal results into a daily performance row."""
    conn = get_conn()
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()
    cur.execute(
        """SELECT status, pnl_pips FROM signals
           WHERE DATE(created_at) = ?""",
        (today,),
    )
    rows = cur.fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r["status"] in {"TP1", "TP2", "TP3"})
    losses = sum(1 for r in rows if r["status"] == "SL")
    breakeven = sum(1 for r in rows if r["status"] == "CANCELLED")
    pips = sum(float(r["pnl_pips"] or 0) for r in rows)
    closed = wins + losses
    win_rate = (wins / closed * 100) if closed else 0.0

    cur.execute(
        """INSERT INTO performance (date, total_signals, wins, losses, breakeven,
                                    win_rate, total_pips)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
             total_signals=excluded.total_signals,
             wins=excluded.wins,
             losses=excluded.losses,
             breakeven=excluded.breakeven,
             win_rate=excluded.win_rate,
             total_pips=excluded.total_pips""",
        (today, total, wins, losses, breakeven, win_rate, pips),
    )
    conn.commit()
    conn.close()
    return {
        "date": today,
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": round(win_rate, 2),
        "total_pips": round(pips, 2),
    }


def get_performance_stats() -> dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()
    cur.execute("SELECT * FROM performance WHERE date = ?", (today,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return update_performance()
    stats = dict(row)
    cur.execute(
        "SELECT COUNT(*) AS c FROM signals WHERE status IN ('ACTIVE','TP1','TP2')"
    )
    stats["active_signals"] = cur.fetchone()["c"]
    conn.close()
    return stats


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
