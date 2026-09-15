# Research: Engine Reliability & Multi-Asset Expansion

## Decision Log

### D1: Cache Invalidation Strategy
- **Decision**: `os.path.getmtime()` comparison against `time.time() - 82800` (23h TTL). One line.
- **Rationale**: Standard library, zero dependencies, deterministic.
- **Alternatives**: `hashlib` content hash (overkill — we don't need change detection, just freshness), SQLite metadata table (overkill).

### D2: Exponential Backoff for yfinance Network Calls
- **Decision**: Manual `time.sleep(2**attempt)` inside a `for attempt in range(3)` loop.
- **Rationale**: `tenacity` / `backoff` libraries are not installed; three-line manual retry is sufficient for 3 retries.
- **Alternatives**: `requests.adapters.HTTPAdapter` retry (can't reach yfinance internals), `tenacity` (new dependency, YAGNI).

### D3: Logging
- **Decision**: `logging.basicConfig` with `RotatingFileHandler` from stdlib. No third-party logger.
- **Rationale**: Already in stdlib, handles rotation, ISO 8601 timestamps built-in via `%(asctime)s`.
- **Alternatives**: `loguru` (not installed), `structlog` (not installed, overkill).

### D4: Circuit Breaker State
- **Decision**: Stored directly in `paper_ledger.json` under a `circuit_breaker` key — no separate file.
- **Rationale**: Already persisting state there; one source of truth.
- **Alternatives**: Separate `circuit_breaker.json` (unnecessary file), in-memory only (lost on crash).

### D5: Multi-Asset Parallelism
- **Decision**: Sequential per-asset loop — no `threading` or `asyncio`.
- **Rationale**: Daily bar evaluation takes <1 second per asset. 5 assets = ~5 seconds total. Concurrency adds complexity for no user-observable benefit at this scale.
- **Alternatives**: `ThreadPoolExecutor` (adds complexity, YAGNI at 5 assets/daily bars).
- `# ponytail: sequential loop, upgrade to ThreadPoolExecutor if >20 assets or intraday latency matters`

### D6: Correlation Lock Implementation
- **Decision**: Hardcoded `CORRELATED_PAIRS = [{"GC=F", "SI=F"}]` set. Check at signal time.
- **Rationale**: The correlation between Gold and Silver is structurally stable (>0.85 over decades). No need to compute it dynamically at runtime.
- **Alternatives**: Rolling dynamic correlation computation (expensive, adds noise, unnecessary).

### D7: .env Loading
- **Decision**: `python-dotenv` `load_dotenv()` called once at the top of `main.py`. `python-dotenv` is already installed.
- **Rationale**: Standard pattern, zero new dependencies.
