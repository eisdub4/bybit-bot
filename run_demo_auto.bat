@echo off
title Bybit Macro Engine - Automated Runner
echo Starting Bybit Demo Automated Runner ($100 Account Mode)...
python main.py --mode demo --capital 100 --loop --interval-hours 4
pause
