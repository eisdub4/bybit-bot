"""Main CLI for Multi-Asset Macro Quantitative Engine."""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from macro_engine import (
    ASSET_UNIVERSE, Analytics, AssetConfig, Backtester, BybitClient, BybitData,
    IntradayBacktester, MacroData, MacroStrategy, PaperTrader,
    SessionBreakoutStrategy, log,
)

load_dotenv()
log.info("Engine starting. .env present: %s", os.path.exists(".env"))


def _print_metrics(metrics: dict):
    print("-" * 60)
    for k, v in metrics.items():
        print(f"  {k.replace('_', ' ').title():<28}: {v}")
    print("-" * 60)


def run_backtest(args):
    # --- Bybit 4H XAUUSDT path ---
    if args.source == "bybit":
        print("=" * 60)
        print("  MACRO QUANT ENGINE — BYBIT 4H XAUUSDT BACKTEST")
        print("=" * 60)
        print("\n  Fetching Bybit 4H klines + DXY/Yield (may take ~30s on first run)...")
        try:
            df = BybitData.fetch_4h(start=args.start, end=args.end)
            print(f"  Loaded {len(df)} 4H bars from {df.index[0]} to {df.index[-1]}")
            # 4H-appropriate windows: ~90 bars ≈ 15 trading days
            features = MacroStrategy.compute_indicators(df, corr_window=90, z_window=180, atr_period=42)
            bt = Backtester(
                initial_capital=args.capital,
                risk_pct_per_trade=args.risk_pct,
                sl_atr_mult=args.sl_atr,
                tp_atr_mult=args.tp_atr,
                base_spread=0.20,   # Bybit XAUUSDT typical spread
                slippage=0.10,
            )
            eq_df, trades = bt.run(features)
            metrics = Analytics.calculate_metrics(eq_df, trades, initial_capital=args.capital)
            print("\n[XAUUSDT 4H] Gold / Bybit Perpetual")
            _print_metrics(metrics)

            if args.monte_carlo and trades:
                print("\n  Running Monte Carlo (5,000 runs)...")
                mc = Analytics.monte_carlo_simulation(trades, runs=5000, initial_capital=args.capital)
                for k, v in mc.items():
                    print(f"  {k.replace('_', ' ').title():<28}: {v}")
                print("-" * 60)

            if args.wfa:
                print("\n  Walk-Forward Analysis (Bybit 4H)...")
                wfa = Analytics.walk_forward_analysis(features)
                print(f"  {'Test Period':<20} | {'Trades':<7} | {'Return %':<10} | {'Max DD %':<10} | {'Sharpe':<8}")
                print("  " + "-" * 62)
                for r in wfa:
                    print(f"  {r['test_period']:<20} | {r['trades']:<7} | {r['return_pct']:<10} | {r['max_dd_pct']:<10} | {r['sharpe']:<8}")
                print("-" * 60)
        except Exception as e:
            print(f"  ERROR: {e}")
            log.error("Bybit 4H backtest failed: %s", e)
        return

    # --- Default: yfinance daily multi-asset path ---
    print("=" * 60)
    print("  MACRO QUANT ENGINE — MULTI-ASSET DAILY BACKTEST")
    print("=" * 60)

    assets: list[AssetConfig] = ASSET_UNIVERSE
    if args.assets:
        tickers = set(args.assets.split(","))
        assets = [a for a in ASSET_UNIVERSE if a.ticker in tickers]

    all_trades: list[dict] = []
    combined_return = 0.0

    for asset in assets:
        print(f"\n[{asset.name}] ({asset.ticker})")
        try:
            df       = MacroData.fetch_asset(asset, start=args.start, end=args.end)
            features = MacroStrategy.compute_indicators(df)
            bt       = Backtester(
                initial_capital=args.capital,
                risk_pct_per_trade=args.risk_pct,
                sl_atr_mult=args.sl_atr,
                tp_atr_mult=args.tp_atr,
            )
            eq_df, trades = bt.run(features)
            metrics = Analytics.calculate_metrics(eq_df, trades, initial_capital=args.capital)
            _print_metrics(metrics)
            all_trades.extend(trades)
            combined_return += metrics.get("total_return_pct", 0)
        except Exception as e:
            print(f"  ERROR: {e}")
            log.error("Backtest failed for %s: %s", asset.ticker, e)

    if len(assets) > 1:
        print("\n[PORTFOLIO COMBINED]")
        print(f"  {'Total Return Pct (Sum)':<28}: {round(combined_return, 2)}")
        print(f"  {'Total Trades (All Assets)':<28}: {len(all_trades)}")

    if args.monte_carlo and all_trades:
        print("\n  Running Monte Carlo (5,000 runs)...")
        mc = Analytics.monte_carlo_simulation(all_trades, runs=5000, initial_capital=args.capital)
        for k, v in mc.items():
            print(f"  {k.replace('_', ' ').title():<28}: {v}")
        print("-" * 60)

    if args.wfa:
        gold = next((a for a in assets if a.ticker == "GC=F"), assets[0])
        print(f"\n  Walk-Forward Analysis ({gold.name})...")
        try:
            df       = MacroData.fetch_asset(gold, start=args.start, end=args.end)
            features = MacroStrategy.compute_indicators(df)
            wfa      = Analytics.walk_forward_analysis(features)
            print(f"  {'Test Period':<20} | {'Trades':<7} | {'Return %':<10} | {'Max DD %':<10} | {'Sharpe':<8}")
            print("  " + "-" * 62)
            for r in wfa:
                print(f"  {r['test_period']:<20} | {r['trades']:<7} | {r['return_pct']:<10} | {r['max_dd_pct']:<10} | {r['sharpe']:<8}")
        except Exception as e:
            print(f"  WFA error: {e}")
        print("-" * 60)


def run_paper(args):
    # --- Bybit paper mode: GC=F daily signals → Bybit XAUUSDT live execution ---
    if args.source == "bybit":
        print("=" * 60)
        print("  MACRO QUANT ENGINE — BYBIT PAPER TRADER")
        print("  (Signals: GC=F daily | Execution: Bybit XAUUSDT live price)")
        print("=" * 60)
        try:
            # 1. Live price from Bybit
            current_price = BybitData.fetch_latest_gold_price()
            if not current_price:
                print("  ERROR: Could not fetch live Bybit XAUUSDT price.")
                return

            # 2. Signal computed from GC=F daily bars (proven, years of history)
            gold_asset = next(a for a in ASSET_UNIVERSE if a.ticker == "GC=F")
            df       = MacroData.fetch_asset(gold_asset, start="2022-01-01")
            features = MacroStrategy.compute_indicators(df)
            last     = features.iloc[-1]
            sig      = MacroStrategy.generate_signal(last)
            atr      = float(last["atr"])  # daily ATR in $/oz — same scale as XAUUSDT

            print(f"  Live XAUUSDT Price    : ${current_price:.2f}")
            print(f"  Signal (GC=F daily)   : {'BUY' if sig == 1 else 'NEUTRAL'}")
            print(f"  Macro Z-Score         : {round(float(last['z_score']), 2)}")
            print(f"  DXY Correlation       : {round(float(last['corr_dxy']), 2)}")
            print(f"  ATR (14-bar daily)    : ${atr:.2f}")
            print(f"  EMA Trend (50d)       : ${round(float(last['ema_trend']), 2)}")
            print("-" * 60)

            # 3. Bracket order parameters using Bybit live price + GC=F ATR
            sl_dist = atr * args.sl_atr
            tp_dist = atr * args.tp_atr
            size    = round((args.capital * args.risk_pct) / sl_dist, 4) if sl_dist > 0 else 0
            sl      = round(current_price - sl_dist, 2)
            tp      = round(current_price + tp_dist, 2)

            # 4. Load & update persistent ledger
            trader  = PaperTrader(ledger_file="bybit_paper_ledger.json", initial_capital=args.capital)
            trader._reset_or_update_cb()

            pos = trader.state["open_positions"].get("XAUUSDT")

            # Check if existing position hit SL or TP
            if pos:
                entry = pos["entry_price"]
                pside = pos["side"]
                actions = []
                closed = False; exit_price = 0.0; reason = ""
                if current_price <= pos["stop_loss"]:
                    exit_price, reason, closed = pos["stop_loss"], "Stop Loss", True
                elif current_price >= pos["take_profit"]:
                    exit_price, reason, closed = pos["take_profit"], "Take Profit", True

                if closed:
                    pnl = (exit_price - entry) * pos["size"]
                    trader.state["capital"] += pnl
                    trader.state["circuit_breaker"]["day_realized_pnl"] += pnl
                    pos.update({"exit_price": exit_price, "pnl": pnl,
                                "exit_time": datetime.now(timezone.utc).isoformat(),
                                "close_reason": reason})
                    trader.state["closed_trades"].append(pos)
                    del trader.state["open_positions"]["XAUUSDT"]
                    print(f"  CLOSED: {pside} @ ${exit_price:.2f} ({reason})  PnL: ${pnl:.4f}")
                else:
                    unrealized = (current_price - entry) * pos["size"]
                    print(f"  OPEN POSITION: LONG @ ${entry:.2f}")
                    print(f"  TP: ${pos['take_profit']:.2f} | SL: ${pos['stop_loss']:.2f}")
                    print(f"  Unrealized PnL: ${unrealized:.4f}")

            # Open new position if signal fires and flat
            if "XAUUSDT" not in trader.state["open_positions"]:
                if trader._check_circuit_breaker():
                    print("  Circuit Breaker ACTIVE — no new entries today.")
                elif sig == 1 and size > 0:
                    trader.state["open_positions"]["XAUUSDT"] = {
                        "ticker": "XAUUSDT", "name": "Gold (Bybit)",
                        "correlation_group": "precious_metals",
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "side": "LONG", "size": size,
                        "entry_price": current_price,
                        "stop_loss": sl, "take_profit": tp,
                    }
                    print(f"  NEW SIGNAL — LONG XAUUSDT")
                    print(f"  Entry  : ${current_price:.2f}")
                    print(f"  TP     : ${tp:.2f}  (+${tp_dist:.2f})")
                    print(f"  SL     : ${sl:.2f}  (-${sl_dist:.2f})")
                    print(f"  Size   : {size} oz  (${size * current_price:.4f} notional)")
                    print(f"  Risk   : ${round(args.capital * args.risk_pct, 4):.4f}  ({args.risk_pct*100:.1f}% of ${args.capital})")
                    log.info("Paper LONG XAUUSDT @ %.2f TP=%.2f SL=%.2f size=%.4f", current_price, tp, sl, size)
                else:
                    print("  Status: No signal — flat, monitoring...")

            print("-" * 60)
            print(f"  Paper Capital : ${trader.state['capital']:.4f}")
            print(f"  Closed Trades : {len(trader.state['closed_trades'])}")
            if trader.state["closed_trades"]:
                total_pnl = sum(t.get("pnl", 0) for t in trader.state["closed_trades"])
                print(f"  Total PnL     : ${total_pnl:.4f}")

            trader._save()

        except Exception as e:
            print(f"  ERROR: {e}")
            log.error("Bybit paper mode failed: %s", e)
        return


    # --- Default multi-asset daily paper mode ---
    print("=" * 60)
    print("  MACRO QUANT ENGINE — MULTI-ASSET DAILY PAPER TRADER")
    print("=" * 60)
    assets: list[AssetConfig] = ASSET_UNIVERSE
    if args.assets:
        tickers = set(args.assets.split(","))
        assets = [a for a in ASSET_UNIVERSE if a.ticker in tickers]
    trader = PaperTrader()
    result = trader.evaluate_live(assets=assets)
    print(f"  {'Paper Capital':<28}: ${result['capital']:,.2f}")
    print(f"  {'Day Realized PnL':<28}: ${result['day_realized_pnl']:,.2f}")
    print(f"  {'Circuit Breaker':<28}: {'ACTIVE' if result['circuit_breaker_active'] else 'OK'}")
    print(f"  {'Open Positions':<28}: {len(result['open_positions'])} / 3")
    for ticker, pos in result["open_positions"].items():
        print(f"    [{pos['name']}] LONG @ ${pos['entry_price']:.2f} | TP: ${pos['take_profit']:.2f} | SL: ${pos['stop_loss']:.2f}")
    print("-" * 60)
    if result["actions"]:
        for a in result["actions"]:
            print(f"   -> {a}")
    else:
        print("  Status: No signals. Monitoring...")
    print("-" * 60)


def run_live(args):
    env_name = os.getenv("BYBIT_ENV", "demo").upper()
    print("=" * 60)
    print(f"  MACRO QUANT ENGINE — BYBIT {env_name} TRADING")
    print("=" * 60)

    client = BybitClient()
    try:
        balance = client.get_wallet_balance()
        print(f"  Bybit Available Balance : ${balance:,.2f} USDT")
    except Exception as e:
        print(f"  ERROR connecting to Bybit API: {e}")
        return

    current_price = BybitData.fetch_latest_gold_price()
    if not current_price:
        print("  ERROR: Could not fetch live Bybit XAUUSDT price.")
        return

    positions = client.get_positions(symbol="XAUUSDT")

    gold_asset = next(a for a in ASSET_UNIVERSE if a.ticker == "GC=F")
    df = MacroData.fetch_asset(gold_asset, start="2022-01-01")
    features = MacroStrategy.compute_indicators(df)
    last = features.iloc[-1]
    sig = MacroStrategy.generate_signal(last)
    atr = float(last["atr"])

    print(f"  Live XAUUSDT Price      : ${current_price:.2f}")
    print(f"  Signal (GC=F daily)     : {'BUY' if sig == 1 else 'NEUTRAL'}")
    print(f"  Macro Z-Score           : {round(float(last['z_score']), 2)}")
    print(f"  DXY Correlation         : {round(float(last['corr_dxy']), 2)}")
    print(f"  ATR (14-bar daily)      : ${atr:.2f}")
    print(f"  EMA Trend (50d)         : ${round(float(last['ema_trend']), 2)}")
    print("-" * 60)

    if positions:
        pos = positions[0]
        side = pos.get("side")
        size = float(pos.get("size", 0))
        entry_price = float(pos.get("avgPrice", 0))
        unrealized_pnl = float(pos.get("unrealisedPnl", 0))
        sl = float(pos.get("stopLoss") or 0)
        tp = float(pos.get("takeProfit") or 0)
        print(f"  ACTIVE BYBIT POSITION   : {side} {size:.3f} oz @ ${entry_price:.2f}")
        print(f"  Take Profit             : ${tp:.2f}")
        print(f"  Stop Loss               : ${sl:.2f}")
        print(f"  Unrealized PnL          : ${unrealized_pnl:.2f} USDT")
    else:
        print("  Active Positions        : None (Flat)")
        if sig == 1 and atr > 0:
            trading_capital = args.capital if args.capital > 0 else balance
            risk_dollars = trading_capital * args.risk_pct
            sl_dist = atr * args.sl_atr
            tp_dist = atr * args.tp_atr
            size = max(0.001, math.floor((risk_dollars / sl_dist) * 1000) / 1000)
            sl = round(current_price - sl_dist, 2)
            tp = round(current_price + tp_dist, 2)

            print(f"\n  --- TRIGGERING BYBIT {env_name} ORDER ---")
            print(f"  Order Type  : Market Buy")
            print(f"  Quantity    : {size:.3f} oz (${size * current_price:.2f} notional)")
            print(f"  Take Profit : ${tp:.2f} (+${tp_dist:.2f})")
            print(f"  Stop Loss   : ${sl:.2f} (-${sl_dist:.2f})")
            print(f"  Risk Budget : ${risk_dollars:.2f} ({args.risk_pct*100:.1f}% of ${trading_capital:,.2f})")

            res = client.place_order(symbol="XAUUSDT", side="Buy", qty=size, take_profit=tp, stop_loss=sl)
            if res.get("retCode") == 0:
                order_id = res.get("result", {}).get("orderId", "N/A")
                print(f"  SUCCESS! Order placed on Bybit. OrderId: {order_id}")
                log.info("Live Bybit order placed: ID=%s Size=%.3f TP=%.2f SL=%.2f", order_id, size, tp, sl)
            else:
                print(f"  FAILED to place order: {res.get('retMsg')}")
                log.error("Live Bybit order failed: %s", res)
        else:
            print("  Status: No signal — flat, monitoring...")
    print("-" * 60)


def run_intraday_backtest(args):
    symbol = getattr(args, "symbol", "BTCUSDT")
    print("=" * 60)
    print(f"  INTRADAY SESSION BREAKOUT — BYBIT {symbol} 15M BACKTEST")
    print("  (Strategy: 15m Asian Range Breakout + 50-EMA Trend Filter)")
    print("=" * 60)
    print(f"\n  Fetching Bybit 15m klines for {symbol} (~150 days / ~17,000 bars)...")
    try:
        df_15m = BybitData.fetch_15m(symbol=symbol, days=150)
        print(f"  Loaded {len(df_15m)} 15m bars from {df_15m.index[0]} to {df_15m.index[-1]}")
        features_15m = SessionBreakoutStrategy.compute_indicators(df_15m)
        bt = IntradayBacktester(
            initial_capital=args.capital,
            risk_pct=args.risk_pct,
            sl_atr_mult=args.sl_atr,
            rr_ratio=args.tp_atr if args.tp_atr != 4.0 else 2.5,
            trailing_be_mult=1.0,
            spread=1.0 if "BTC" in symbol else 0.25,
            slippage=0.5 if "BTC" in symbol else 0.15,
        )
        eq_df, trades = bt.run(features_15m)
        metrics = Analytics.calculate_metrics(eq_df, trades, initial_capital=args.capital)
        print(f"\n[{symbol} 15M] Asian Range Session Breakout")
        _print_metrics(metrics)

        if args.monte_carlo and trades:
            print("\n  Running Monte Carlo (5,000 runs)...")
            mc = Analytics.monte_carlo_simulation(trades, runs=5000, initial_capital=args.capital)
            for k, v in mc.items():
                print(f"  {k.replace('_', ' ').title():<28}: {v}")
            print("-" * 60)
    except Exception as e:
        print(f"  ERROR: {e}")
        log.error("Intraday backtest failed: %s", e)


def run_intraday_live(args):
    symbol = getattr(args, "symbol", "BTCUSDT")
    env_name = os.getenv("BYBIT_ENV", "demo").upper()
    print("=" * 60)
    print(f"  INTRADAY SESSION BREAKOUT — BYBIT {env_name} TRADING ({symbol})")
    print("  (Strategy: 15m Asian Range Breakout + 50-EMA Trend Filter)")
    print("=" * 60)

    client = BybitClient()
    try:
        balance = client.get_wallet_balance()
        client.set_leverage(symbol=symbol, leverage=10)
        print(f"  Bybit Available Balance : ${balance:,.2f} USDT (10x Leverage Configured)")
    except Exception as e:
        print(f"  ERROR connecting to Bybit API: {e}")
        return

    try:
        df_15m = BybitData.fetch_15m(symbol=symbol, days=10)
        features_15m = SessionBreakoutStrategy.compute_indicators(df_15m)
        last_bar = features_15m.iloc[-1]
        now_utc = datetime.now(timezone.utc)
        current_price = BybitData.fetch_latest_price(symbol=symbol) or float(last_bar["close"])
        atr = float(last_bar["atr"])
        ema = float(last_bar["ema50"])
    except Exception as e:
        print(f"  ERROR fetching market data for {symbol}: {e}")
        return

    asian_high, asian_low = SessionBreakoutStrategy.get_asian_range_for_today(df_15m, now_utc.date())

    print(f"  Current UTC Time        : {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Live {symbol} Price     : ${current_price:,.2f}")
    print(f"  15m ATR (14-period)     : ${atr:.2f}")
    print(f"  50-EMA (15m Filter)     : ${ema:,.2f}")
    if asian_high is not None and asian_low is not None:
        range_size = asian_high - asian_low
        print(f"  Asian Range (00-07 UTC) : ${asian_low:,.2f} — ${asian_high:,.2f} (Spread: ${range_size:.2f})")
    else:
        print(f"  Asian Range (00-07 UTC) : Forming / Incomplete (Current Hour: {now_utc.hour} UTC)")
    print("-" * 60)

    positions = client.get_positions(symbol=symbol)
    hour = now_utc.hour

    if positions:
        pos = positions[0]
        side = pos.get("side")
        size = float(pos.get("size", 0))
        entry_price = float(pos.get("avgPrice", 0))
        unrealized_pnl = float(pos.get("unrealisedPnl", 0))
        sl = float(pos.get("stopLoss") or 0)
        tp = float(pos.get("takeProfit") or 0)
        unit = "BTC" if "BTC" in symbol else "oz"
        print(f"  ACTIVE POSITION         : {side} {size:.3f} {unit} @ ${entry_price:,.2f}")
        print(f"  Take Profit             : ${tp:,.2f}")
        print(f"  Stop Loss               : ${sl:,.2f}")
        print(f"  Unrealized PnL          : ${unrealized_pnl:.2f} USDT")

        # Hard EOD exit rule: Close at 21:00 UTC or later
        if hour >= 21:
            print(f"\n  [EOD RULE] End-of-Day reached (>= 21:00 UTC). Closing {symbol} position...")
            res = client.close_position(symbol=symbol, side=side, qty=size)
            if res.get("retCode") == 0:
                print(f"  SUCCESS! Position closed at market price: ${current_price:,.2f}")
                log.info("EOD close executed for %s %s size=%.3f @ %.2f", symbol, side, size, current_price)
            else:
                print(f"  FAILED to close position: {res.get('retMsg')}")
            return

        # Trailing Break-Even Rule
        if atr > 0:
            if side == "Buy" and (current_price - entry_price) >= atr and sl < entry_price:
                print(f"\n  [TRAILING RULE] Profit is +${current_price - entry_price:.2f} (>= 1.0x ATR). Moving SL to Break-Even (${entry_price:,.2f})...")
                res = client.set_trading_stop(symbol=symbol, stop_loss=entry_price)
                if res.get("retCode") == 0:
                    print("  SUCCESS! Stop Loss moved to Break-Even.")
                    log.info("Moved SL to Break-Even for %s %s @ %.2f", symbol, side, entry_price)
            elif side == "Sell" and (entry_price - current_price) >= atr and (sl == 0 or sl > entry_price):
                print(f"\n  [TRAILING RULE] Profit is +${entry_price - current_price:.2f} (>= 1.0x ATR). Moving SL to Break-Even (${entry_price:,.2f})...")
                res = client.set_trading_stop(symbol=symbol, stop_loss=entry_price)
                if res.get("retCode") == 0:
                    print("  SUCCESS! Stop Loss moved to Break-Even.")
                    log.info("Moved SL to Break-Even for %s %s @ %.2f", symbol, side, entry_price)

    else:
        print("  Active Positions        : None (Flat)")

        if not (7 <= hour <= 16):
            print(f"  Status: Outside active session trading window (07:00 - 16:00 UTC). Monitoring...")
            print("-" * 60)
            return

        if asian_high is None or asian_low is None:
            print("  Status: Asian session data incomplete for today. Waiting for range...")
            print("-" * 60)
            return

        range_size = asian_high - asian_low
        if range_size < atr * 0.5:
            print(f"  Status: Asian Range (${range_size:.2f}) too tight (< 0.5x ATR). Skipping breakout.")
            print("-" * 60)
            return

        sig = 0
        if current_price > asian_high and current_price > ema:
            sig = 1
        elif current_price < asian_low and current_price < ema:
            sig = -1

        if sig != 0 and atr > 0:
            trading_capital = args.capital if args.capital > 0 else balance
            risk_dollars = trading_capital * args.risk_pct
            min_sl = 50.0 if "BTC" in symbol else 3.0
            sl_dist = max(atr * (args.sl_atr if args.sl_atr != 2.0 else 1.5), min_sl)
            rr = 2.5 if sig == 1 else 2.0
            tp_dist = sl_dist * rr
            size = max(0.001, math.floor((risk_dollars / sl_dist) * 1000) / 1000)

            side_name = "Buy" if sig == 1 else "Sell"
            sl = round(current_price - sl_dist if sig == 1 else current_price + sl_dist, 2)
            tp = round(current_price + tp_dist if sig == 1 else current_price - tp_dist, 2)
            unit = "BTC" if "BTC" in symbol else "oz"

            print(f"\n  --- TRIGGERING INTRADAY BREAKOUT ({side_name.upper()}) ---")
            print(f"  Symbol      : {symbol}")
            print(f"  Setup       : {'London High Breakout' if sig==1 else 'London Low Breakdown'}")
            print(f"  Order Type  : Market {side_name}")
            print(f"  Quantity    : {size:.3f} {unit} (${size * current_price:,.2f} notional)")
            print(f"  Take Profit : ${tp:,.2f} ({'+' if sig==1 else '-'}${tp_dist:.2f}) [{rr}:1 RR]")
            print(f"  Stop Loss   : ${sl:,.2f} ({'-' if sig==1 else '+'}${sl_dist:.2f}) [1.5x ATR]")
            print(f"  Risk Budget : ${risk_dollars:.2f} ({args.risk_pct*100:.1f}% of ${trading_capital:,.2f})")

            res = client.place_order(symbol=symbol, side=side_name, qty=size, take_profit=tp, stop_loss=sl)
            if res.get("retCode") == 0:
                order_id = res.get("result", {}).get("orderId", "N/A")
                print(f"  SUCCESS! Order placed on Bybit. OrderId: {order_id}")
                log.info("Intraday order placed: %s %s ID=%s Size=%.3f TP=%.2f SL=%.2f", symbol, side_name, order_id, size, tp, sl)
            else:
                print(f"  FAILED to place order: {res.get('retMsg')}")
                log.error("Intraday order failed for %s: %s", symbol, res)
        else:
            print(f"  Status: Inside Asian Range (${asian_low:,.2f} - ${asian_high:,.2f}). Waiting for breakout...")
    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(description="Macro Multi-Asset & Intraday Quant Engine")
    parser.add_argument("--strategy",    choices=["intraday", "macro"], default="intraday",
                        help="Strategy to run. 'intraday' = 15m Session Breakout (1-2 trades/day). 'macro' = Daily Swing.")
    parser.add_argument("--symbol",      default="BTCUSDT", help="Symbol to trade for intraday engine (default: BTCUSDT, also supports XAUUSDT)")
    parser.add_argument("--mode",        choices=["backtest", "paper", "demo", "live"], default="backtest")
    parser.add_argument("--source",      choices=["yfinance", "bybit"], default="bybit",
                        help="Data source. 'bybit' = Bybit klines. 'yfinance' = daily multi-asset.")
    parser.add_argument("--start",       default="2023-01-01")
    parser.add_argument("--end",         default=None)
    parser.add_argument("--assets",      default=None, help="Comma-separated tickers (macro mode only)")
    parser.add_argument("--capital",     type=float, default=100.0,
                        help="Starting capital (default $100 for paper account, or balance for live/demo)")
    parser.add_argument("--risk-pct",    type=float, default=0.015)
    parser.add_argument("--sl-atr",      type=float, default=1.5, help="Stop loss ATR multiplier (default 1.5 for intraday, 2.0 for macro)")
    parser.add_argument("--tp-atr",      type=float, default=2.0, help="Take profit RR multiplier (default 2.0 for intraday, 4.0 for macro)")
    parser.add_argument("--monte-carlo", action="store_true")
    parser.add_argument("--wfa",         action="store_true")
    parser.add_argument("--loop",        action="store_true", help="Run continuously in automated background loop")
    parser.add_argument("--interval-hours", type=float, default=0.0, help="Hours between automated scans in loop mode")
    parser.add_argument("--interval-mins",  type=float, default=15.0, help="Minutes between scans (default: 15.0 for intraday)")

    args = parser.parse_args()

    # Dispatch based on strategy and mode
    if args.strategy == "intraday":
        dispatch = {
            "backtest": run_intraday_backtest,
            "paper": run_intraday_live,
            "demo": run_intraday_live,
            "live": run_intraday_live,
        }
    else:
        dispatch = {
            "backtest": run_backtest,
            "paper": run_paper,
            "demo": run_live,
            "live": run_live,
        }

    if args.loop:
        interval_sec = int(args.interval_hours * 3600) if args.interval_hours > 0 else int(args.interval_mins * 60)
        interval_label = f"{args.interval_hours} hrs" if args.interval_hours > 0 else f"{args.interval_mins} mins"
        print("=" * 60)
        print(f"  [AUTOMATED RUNNER] Strategy: {args.strategy.upper()} | Mode: {args.mode.upper()} | Interval: {interval_label}")
        print("  Press Ctrl+C at any time to halt.")
        print("=" * 60)

        while True:
            try:
                dispatch[args.mode](args)
            except KeyboardInterrupt:
                print("\n[AUTOMATED RUNNER] Halted by user.")
                break
            except Exception as e:
                log.error("Loop iteration failed: %s", e)
                print(f"\n[LOOP ERROR]: {e}")

            next_run_ts = time.time() + interval_sec
            next_time_str = datetime.fromtimestamp(next_run_ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[AUTOMATED RUNNER] Sleeping. Next check scheduled at: {next_time_str}")
            try:
                time.sleep(interval_sec)
            except KeyboardInterrupt:
                print("\n[AUTOMATED RUNNER] Halted by user.")
                break
    else:
        dispatch[args.mode](args)


if __name__ == "__main__":
    main()


