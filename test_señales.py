"""
Script de diagnóstico rápido — corre FUERA del servidor.
Uso: python test_señales.py
"""
import json, sys, time
sys.path.insert(0, r"C:\Users\saems\scanner")

with open(r"C:\Users\saems\scanner\config.json") as f:
    cfg = json.load(f)

print("=" * 60)
print(f"min_confidence: {cfg['trading']['min_confidence']}")
print(f"min_rr:         {cfg['trading']['min_rr']}")
print("=" * 60)

import brokers, database as db
from scanner import TradingScanner
from indicators import get_all_indicators
from patterns import analyze_patterns

db.init_db()
scanner = TradingScanner(cfg)

symbols    = cfg["trading"]["symbols"]
timeframes = cfg["trading"]["timeframes"]

total_señales = 0
for sym in symbols:
    for tf in timeframes:
        t0 = time.time()
        df = brokers.get_ohlcv(sym, tf, 250)
        if df is None or df.empty or len(df) < 50:
            print(f"  {sym}/{tf}: SIN DATOS ({len(df) if df is not None else 0} filas)")
            continue

        inds = get_all_indicators(df)
        pats = analyze_patterns(df)
        sig  = scanner.calculate_signal(sym, tf, df, pats, inds)

        if sig is None:
            pa   = pats.get("price_action", {})
            ph   = pats.get("best_harmonic", {})
            print(f"  {sym}/{tf}: sin patrón "
                  f"[pin={pa.get('pin_bar',{}).get('found',False)} "
                  f"eng={pa.get('engulfing',{}).get('found',False)} "
                  f"brk={pa.get('breakout',{}).get('found',False)} "
                  f"harm={ph.get('found',False)}]")
            continue

        valid, reasons = scanner.validate_signal(sig, sym)
        approved, review_reason = scanner.review_signal(sig) if valid else (False, "validate falló")

        status = "✅ ENVIADA" if (valid and approved) else "❌ BLOQUEADA"
        print(
            f"  {sym}/{tf}: {sig['signal_type']} conf={sig['confidence']}% "
            f"RR={sig['rr']} setup={sig['setup']}"
        )
        print(f"    confs: {sig.get('confirmations')}")
        if not valid:
            print(f"    VALIDATE FAIL: {reasons}")
        elif not approved:
            print(f"    REVIEW BLOCK: {review_reason}")
        else:
            print(f"    {status}")
            total_señales += 1
        elapsed = time.time() - t0
        print(f"    ({elapsed:.1f}s)")

print("=" * 60)
print(f"TOTAL SEÑALES APTAS: {total_señales}")
