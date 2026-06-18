# Pulso Capital Scanner PRO

Bot de señales de trading (XAUUSD entre semana, BTC/ETH/XRP los fines de semana) que envía alertas a un grupo de Telegram, con confluencia de price action, indicadores técnicos y un sesgo macro basado en el índice del dólar (DXY).

## Características

- Estrategia dedicada para oro (XAUUSD): tendencia H4 + patrones armónicos + price action (engulfing, pin bar) + confirmaciones técnicas (ADX, EMAs, RSI, MACD, Bollinger).
- Modo fin de semana automático: cuando el mercado forex/oro cierra (sábado y domingo UTC), el scanner cambia a BTC/ETH/XRP.
- Score de confianza 0-100 con umbral mínimo configurable (por defecto 85).
- Sesgo macro complementario para oro: compara el cierre del DXY día a día y ajusta la confianza de la señal (±6 puntos) según si el dólar favorece o contradice la dirección de la señal.
- Cascada de fuentes de datos con redundancia: MT5 → puente MT4 → yfinance → respaldo HTTP directo a Yahoo / TwelveData / Alpha Vantage, para seguir funcionando aunque alguna fuente falle (pensado para correr 24/7 en un VPS sin depender de una terminal MT5 local).
- Notificaciones y comandos vía Telegram (con GIFs configurables para compra/venta/TP/SL).
- Dashboard / API con FastAPI.

## Instalación

```bash
pip install -r requirements.txt
cp config.example.json config.json
# Editar config.json con tu token y chat_id de Telegram
python main.py
```

## Estructura

- `main.py` — servidor FastAPI + loop de escaneo.
- `scanner.py` — motor de señales (`TradingScanner`).
- `brokers.py` — fuentes de precios/velas (MT5, MT4 bridge, yfinance, Swissquote, CoinGecko, data_feeds).
- `data_feeds.py` — agregador de datos macro/sentimiento (DXY, Fear & Greed, noticias, correlaciones).
- `indicators.py` / `patterns.py` — indicadores técnicos y reconocimiento de patrones.
- `telegram_bot.py` / `telegram_commands.py` — envío de señales y comandos del bot.
- `database.py` — persistencia de señales (SQLite).

## Despliegue 24/7

El proyecto no depende de MetaTrader 5 para funcionar (el import está protegido con `try/except`), así que puede correr en un VPS Linux sin GUI. Incluye una plantilla de servicio `systemd` (`scanner-pro.service`) para mantenerlo corriendo y reiniciarse solo si se cae.

## Aviso

Este proyecto es para fines educativos y de automatización personal. No constituye asesoría financiera. Operar con apalancamiento conlleva riesgo de pérdida total del capital.
