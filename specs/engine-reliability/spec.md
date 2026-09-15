# Feature Spec: Engine Reliability & Multi-Asset Expansion

## Problem Statement
The current macro quant engine (`macro_engine.py`) works for a single asset (XAUUSD/GC=F) on daily bars but has four critical reliability gaps that prevent safe personal deployment:
1. CSV cache never expires — paper trader reads stale historical data indefinitely.
2. No circuit breaker — engine keeps firing trades during a losing streak, amplifying drawdowns.
3. No error handling — API failures and network drops crash the process silently.
4. Single-asset only — only Gold is scanned; Silver, Oil, EURUSD, and BTC are idle.

## Users
- **Primary**: The system operator (you), running the engine on a local machine or VPS for personal automated trading.

## Functional Requirements

### FR-1: Cache TTL
- Cache file (`macro_data_cache.csv`) must be considered stale if its mtime is older than 23 hours.
- On stale detection, re-fetch all assets and overwrite the cache.
- Cache filenames must be per-asset+interval so they don't collide across assets.

### FR-2: Circuit Breaker
- Track rolling daily PnL in the paper ledger.
- If realized losses in the current UTC trading day exceed a configurable threshold (default: 3% of starting daily capital), halt all new position entries for the remainder of that day.
- Log the halt event with timestamp and daily loss amount.
- Auto-reset at UTC midnight.

### FR-3: Error Handling & Logging
- All network calls (yfinance downloads, future broker API calls) must be wrapped in try/except with exponential backoff (max 3 retries).
- All errors must be appended to a rotating log file (`engine.log`) with ISO 8601 timestamps.
- On unrecoverable error, print a clear human-readable message and exit with code 1 (no silent crash).

### FR-4: Multi-Asset Daily Scanner
- Extend the engine to evaluate 5 assets in parallel at each daily bar:
  `GC=F` (Gold), `SI=F` (Silver), `CL=F` (Crude Oil), `EURUSD=X` (EUR/USD), `BTC-USD` (Bitcoin).
- Each asset uses the same DXY + US10Y macro driver series.
- Portfolio heat cap: max 3 concurrent open positions across all assets.
- Correlation lock: cannot hold simultaneous positions in both `GC=F` and `SI=F` (both are precious metals with ~0.85+ correlation).

### FR-5: .env Credential Isolation
- All future API keys (Bybit, broker) must be loaded from a `.env` file via `python-dotenv`.
- `.env` must be in `.gitignore`.
- Engine must print a clear startup warning if `.env` is missing but not crash (keys may not be needed for paper mode).

## Non-Functional Requirements
- **NFR-1 Ponytail**: No new dependencies beyond what's already installed unless strictly necessary. `python-dotenv` is already present.
- **NFR-2**: Cache file per asset, named `cache_{ticker}_{interval}.csv` (e.g., `cache_GCF_1d.csv`).
- **NFR-3**: All changes must be backward-compatible — `python main.py --mode backtest` still works with zero flags.

## Out of Scope
- SaaS, subscriptions, Discord/Telegram webhooks, web dashboard, Stripe billing.
- 4H/1H intraday timeframes (requires Bybit REST API, deferred to Step 4).
- Bybit live order execution (deferred to Step 4).
