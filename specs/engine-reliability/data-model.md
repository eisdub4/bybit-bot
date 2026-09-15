# Data Model: Engine Reliability & Multi-Asset Expansion

## Entities

### AssetConfig
Defines a tradable asset's identity and its macro driver mapping.

| Field | Type | Notes |
| :--- | :--- | :--- |
| `ticker` | `str` | yfinance ticker (e.g. `"GC=F"`) |
| `name` | `str` | Human label (e.g. `"Gold"`) |
| `correlation_group` | `str \| None` | Group key for correlation lock (e.g. `"precious_metals"`) |
| `cache_key` | `str` | Filename-safe slug (e.g. `"GCF_1d"`) |

**Default universe (5 assets):**
```python
ASSET_UNIVERSE = [
    AssetConfig("GC=F",     "Gold",       "precious_metals", "GCF_1d"),
    AssetConfig("SI=F",     "Silver",     "precious_metals", "SIF_1d"),
    AssetConfig("CL=F",     "Crude Oil",  None,              "CLF_1d"),
    AssetConfig("EURUSD=X", "EUR/USD",    None,              "EURUSD_1d"),
    AssetConfig("BTC-USD",  "Bitcoin",    None,              "BTC_1d"),
]
```

---

### PaperLedger (persisted in `paper_ledger.json`)
Extended to include circuit breaker state and per-asset positions.

```json
{
  "capital": 100000.0,
  "open_positions": {
    "GC=F": { "side": "LONG", "size": 0.03, "entry_price": 2650.0,
               "stop_loss": 2600.0, "take_profit": 2737.5,
               "entry_time": "2026-09-15T03:00:00Z" }
  },
  "closed_trades": [],
  "circuit_breaker": {
    "active": false,
    "day_utc": "2026-09-15",
    "day_start_capital": 100000.0,
    "day_realized_pnl": 0.0,
    "halt_threshold_pct": 3.0
  }
}
```

**State transitions:**
- `active: false` → `active: true`: When `day_realized_pnl / day_start_capital < -0.03`
- `active: true` → `active: false`: At UTC midnight (new trading day)

---

### PortfolioHeatState (in-memory during run)

| Field | Type | Notes |
| :--- | :--- | :--- |
| `open_count` | `int` | Current number of open positions |
| `max_positions` | `int` | Hard cap (default: 3) |
| `active_groups` | `set[str]` | Correlation groups currently in a position |

---

### CacheFile (filesystem)

- **Path pattern**: `cache_{cache_key}.csv` (e.g. `cache_GCF_1d.csv`)
- **TTL**: 23 hours from `os.path.getmtime()`
- **Format**: Standard CSV with UTC DatetimeIndex and columns:
  `{asset}_close`, `{asset}_high`, `{asset}_low`, `dxy_close`, `yield_close`
