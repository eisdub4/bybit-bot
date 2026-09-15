@echo off
title Bybit 15m Intraday Session Breakout Bot (BTCUSDT - $100 Risk Base)
echo ============================================================
echo   BYBIT 15M INTRADAY SESSION BREAKOUT BOT (BTCUSDT)
echo   Account Mode: $100 Risk Base
echo   Interval: 15-Minute Candle Sync Loop
echo ============================================================
python main.py --strategy intraday --mode demo --symbol BTCUSDT --capital 100 --loop --interval-mins 15
pause
