# Implementation Plan: Engine Reliability & Multi-Asset Expansion

## Goal
Make `macro_engine.py` production-safe for personal automated daily use by fixing 4 critical reliability gaps and expanding from 1 to 5 tradable assets.

## Constitution Check
- **Minimal dependencies**: All changes use stdlib + already-installed packages. ✅
- **Single module**: All logic stays in `macro_engine.py` + `main.py`. ✅
- **Backward compatible**: Existing `python main.py --mode backtest` unchanged. ✅

## Technical Context
- **Runtime**: Python 3.11, Windows (local) or Linux (future VPS)
- **Installed packages**: `numpy`, `pandas`, `yfinance`, `matplotlib`, `python-dotenv`
- **Key files**: [`macro_engine.py`](file:///c:/Users/sjcab/Documents/antigravity/hopeful-carson/macro_engine.py), [`main.py`](file:///c:/Users/sjcab/Documents/antigravity/hopeful-carson/main.py), [`test_engine.py`](file:///c:/Users/sjcab/Documents/antigravity/hopeful-carson/test_engine.py)

---

## Phase 0: Research ✅ Complete
All unknowns resolved in [`research.md`](file:///c:/Users/sjcab/Documents/antigravity/hopeful-carson/specs/engine-reliability/research.md).

---

## Phase 1: Implementation Tasks

### Task 1 — Cache TTL + Per-Asset Named Cache Files
**File**: `macro_engine.py` → `MacroData`
- Replace hardcoded `"macro_data_cache.csv"` with per-asset `cache_{ticker_slug}_{interval}.csv`
- Add `_cache_stale(path)` helper: `return not os.path.exists(path) or (time.time() - os.path.getmtime(path)) > 82800`
- Wrap `yf.download()` calls in 3-retry exponential backoff

### Task 2 — Structured Logging
**File**: `macro_engine.py` (module top)
- Add `logging.basicConfig(handlers=[RotatingFileHandler("engine.log", maxBytes=1_000_000, backupCount=2)], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")`
- Replace all `print()` error paths with `logging.error()` / `logging.info()`
- Wrap all yfinance calls in `try/except Exception as e: logging.error(...)`

### Task 3 — Circuit Breaker
**File**: `macro_engine.py` → `PaperTrader`
- Extend `paper_ledger.json` schema with `circuit_breaker` dict (see data-model.md)
- `_check_circuit_breaker()`: returns `True` (halt) if `day_realized_pnl / day_start_capital < -halt_threshold_pct`
- `_reset_circuit_breaker_if_new_day()`: compare `day_utc` to `datetime.now(UTC).date().isoformat()`
- Block new entries in `evaluate_live()` when breaker is active; log the halt

### Task 4 — Asset Universe + Portfolio Heat + Correlation Lock
**File**: `macro_engine.py`
- Add `AssetConfig` dataclass (ticker, name, correlation_group, cache_key)
- Define `ASSET_UNIVERSE` list (5 assets)
- Define `CORRELATED_GROUPS` set-of-sets for lock logic
- Extend `PaperTrader` ledger: replace single `open_position` dict with `open_positions` dict keyed by ticker
- Add `_portfolio_heat_ok(ticker)`: checks `len(open_positions) < max_positions` AND no same-group asset already open
- `evaluate_live()` loops over `ASSET_UNIVERSE`, calls `_portfolio_heat_ok()` before any new entry

### Task 5 — .env Credential Isolation
**File**: `main.py`
- Add `load_dotenv()` call at top
- Add startup log: `logging.info("Engine starting. .env loaded: %s", os.path.exists('.env'))`
- Create `.env.example` with placeholder keys
- Create/update `.gitignore` to exclude `.env` and `*.csv` cache files

### Task 6 — Backtest Multi-Asset Support
**File**: `main.py` → `run_backtest()`
- Loop over `ASSET_UNIVERSE`, run `MacroData.fetch_historical()` + `MacroStrategy.compute_indicators()` + `Backtester.run()` per asset
- Print per-asset tearsheet + combined portfolio summary (sum of all trade PnL)

---

## Phase 2: Verification

### Automated
```
python test_engine.py
```
Must verify:
- Cache TTL: stale file triggers re-fetch, fresh file skips fetch
- Circuit breaker: halts entry after -3% day loss, resets next UTC day
- Portfolio heat: 4th concurrent signal blocked
- Correlation lock: Silver blocked when Gold position is open

### Manual End-to-End
```bash
# Multi-asset backtest (5 assets, 2022 → present)
python main.py --mode backtest --start 2022-01-01 --monte-carlo

# Paper trader multi-asset scan
python main.py --mode paper
```
