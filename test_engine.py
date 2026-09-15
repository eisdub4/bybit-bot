"""Unit checks for engine reliability features."""

import json
import os
import time
import numpy as np
import pandas as pd
from macro_engine import (
    ASSET_UNIVERSE, Analytics, AssetConfig, Backtester,
    MacroStrategy, PaperTrader, _cache_stale, _cache_path,
)


def test_cache_ttl():
    # Fresh file → not stale
    path = "test_cache_ttl.csv"
    pd.DataFrame({"a": [1]}).to_csv(path)
    assert not _cache_stale(path), "Fresh cache file should not be stale"

    # Simulate old mtime
    old_mtime = time.time() - 90_000  # > 23 hours
    os.utime(path, (old_mtime, old_mtime))
    assert _cache_stale(path), "Old cache file should be stale"

    # Missing file → stale
    assert _cache_stale("nonexistent_file.csv"), "Missing file should be stale"
    os.remove(path)
    print("[PASS] Cache TTL")


def test_circuit_breaker():
    ledger_file = "test_ledger_cb.json"
    if os.path.exists(ledger_file):
        os.remove(ledger_file)

    trader = PaperTrader(ledger_file=ledger_file)
    # Simulate a 4% daily loss (exceeds 3% threshold)
    trader.state["circuit_breaker"]["day_realized_pnl"]  = -4000.0
    trader.state["circuit_breaker"]["day_start_capital"] = 100_000.0
    trader.state["circuit_breaker"]["day_utc"]           = "2099-01-01"  # far future so no reset

    triggered = trader._check_circuit_breaker()
    assert triggered, "Circuit breaker should trigger on 4% loss"
    assert trader.state["circuit_breaker"]["active"], "Circuit breaker should be marked active"

    trader._save()  # ensure file exists before cleanup
    os.remove(ledger_file)
    print("[PASS] Circuit Breaker")


def test_portfolio_heat():
    ledger_file = "test_ledger_heat.json"
    if os.path.exists(ledger_file):
        os.remove(ledger_file)

    trader = PaperTrader(ledger_file=ledger_file)
    # Fill to max (3 positions)
    for i in range(3):
        trader.state["open_positions"][f"FAKE{i}"] = {"correlation_group": None}

    gold = next(a for a in ASSET_UNIVERSE if a.ticker == "GC=F")
    can_open = trader._can_open(gold.ticker, gold.correlation_group)
    assert not can_open, "Should block 4th position (max 3)"

    trader._save()
    os.remove(ledger_file)
    print("[PASS] Portfolio Heat Cap")


def test_correlation_lock():
    ledger_file = "test_ledger_corr.json"
    if os.path.exists(ledger_file):
        os.remove(ledger_file)

    trader = PaperTrader(ledger_file=ledger_file)
    # Open Gold (precious_metals group)
    trader.state["open_positions"]["GC=F"] = {"correlation_group": "precious_metals"}

    silver = next(a for a in ASSET_UNIVERSE if a.ticker == "SI=F")
    can_open = trader._can_open(silver.ticker, silver.correlation_group)
    assert not can_open, "Silver should be blocked when Gold is open (same correlation group)"

    trader._save()
    os.remove(ledger_file)
    print("[PASS] Correlation Lock")


def test_indicator_and_signal():
    dates = pd.date_range(start="2024-01-01", periods=100, freq="D", tz="UTC")
    df = pd.DataFrame({
        "gold_close":  2000.0 + np.sin(np.linspace(0, 10, 100)) * 50,
        "gold_high":   2010.0 + np.sin(np.linspace(0, 10, 100)) * 50,
        "gold_low":    1990.0 + np.sin(np.linspace(0, 10, 100)) * 50,
        "dxy_close":   104.0  - np.sin(np.linspace(0, 10, 100)) * 2,
        "yield_close": 4.2    - np.sin(np.linspace(0, 10, 100)) * 0.2,
    }, index=dates)

    features = MacroStrategy.compute_indicators(df, corr_window=10, z_window=15, atr_period=5)
    assert not features.empty
    assert "z_score" in features.columns and "atr" in features.columns

    eq_df, trades = Backtester(initial_capital=100_000.0).run(features)
    assert not eq_df.empty
    metrics = Analytics.calculate_metrics(eq_df, trades)
    assert "sharpe_ratio" in metrics and "max_drawdown_pct" in metrics
    print("[PASS] Indicators & Signal Engine")


def test_intraday_breakout():
    from macro_engine import SessionBreakoutStrategy, IntradayBacktester

    dates = pd.date_range(start="2026-09-01 00:00:00", periods=96*3, freq="15min", tz="UTC")
    df_15m = pd.DataFrame({
        "open":  2500.0 + np.sin(np.linspace(0, 15, len(dates))) * 15,
        "high":  2505.0 + np.sin(np.linspace(0, 15, len(dates))) * 15,
        "low":   2495.0 + np.sin(np.linspace(0, 15, len(dates))) * 15,
        "close": 2501.0 + np.sin(np.linspace(0, 15, len(dates))) * 15,
        "volume": 100.0,
    }, index=dates)

    features = SessionBreakoutStrategy.compute_indicators(df_15m, atr_period=14, ema_period=20)
    assert not features.empty
    assert "atr" in features.columns and "ema50" in features.columns

    high_p, low_p = SessionBreakoutStrategy.get_asian_range_for_today(df_15m, target_date=dates[0].date())
    assert high_p is not None and low_p is not None
    assert high_p >= low_p

    bt = IntradayBacktester(initial_capital=100.0, risk_pct=0.015)
    eq_df, trades = bt.run(features)
    assert not eq_df.empty
    metrics = Analytics.calculate_metrics(eq_df, trades, initial_capital=100.0)
    assert "total_trades" in metrics
    print("[PASS] Intraday Session Breakout Engine")


if __name__ == "__main__":
    test_cache_ttl()
    test_circuit_breaker()
    test_portfolio_heat()
    test_correlation_lock()
    test_indicator_and_signal()
    test_intraday_breakout()
    print("\n[ALL TESTS PASSED]")
