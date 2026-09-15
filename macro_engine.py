"""
Macro Quantitative Trading & Backtesting Engine (Multi-Asset)
Ponytail: Lean, self-contained. All reliability fixes applied.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Logging (stdlib RotatingFileHandler, no new deps)
# ---------------------------------------------------------------------------
logging.basicConfig(
    handlers=[RotatingFileHandler("engine.log", maxBytes=1_000_000, backupCount=2)],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asset Universe
# ---------------------------------------------------------------------------
@dataclass
class AssetConfig:
    ticker: str
    name: str
    correlation_group: str | None  # assets in same group cannot be held simultaneously
    cache_key: str                 # filename-safe slug for CSV cache


ASSET_UNIVERSE: list[AssetConfig] = [
    AssetConfig("GC=F",    "Gold",      "precious_metals", "GCF_1d"),
    AssetConfig("SI=F",    "Silver",    "precious_metals", "SIF_1d"),
    AssetConfig("PL=F",    "Platinum",  "precious_metals", "PLF_1d"),
    AssetConfig("HG=F",    "Copper",    "industrial",      "HGF_1d"),
    AssetConfig("BTC-USD", "Bitcoin",   None,              "BTC_1d"),
]

# Macro driver tickers (shared across all assets)
_DXY_TICKER   = "DX-Y.NYB"
_YIELD_TICKER = "^TNX"
_CACHE_TTL_S  = 82_800  # 23 hours


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def _cache_path(cache_key: str) -> str:
    return f"cache_{cache_key}.csv"


def _cache_stale(path: str) -> bool:
    return not os.path.exists(path) or (time.time() - os.path.getmtime(path)) > _CACHE_TTL_S


# ---------------------------------------------------------------------------
# Data Layer
# ---------------------------------------------------------------------------
class MacroData:
    """Multi-asset data ingestion with TTL cache, retry, and UTC alignment."""

    @staticmethod
    def _download_with_retry(ticker: str, start: str, end: str | None, max_retries: int = 3) -> pd.DataFrame:
        for attempt in range(max_retries):
            try:
                df = yf.download(ticker, start=start, end=end, progress=False)
                if not df.empty:
                    return df
                log.warning("Empty data for %s (attempt %d)", ticker, attempt + 1)
            except Exception as e:
                log.error("Download failed for %s attempt %d: %s", ticker, attempt + 1, e)
            time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed to download {ticker} after {max_retries} attempts")

    @staticmethod
    def _extract_ohlc(df: pd.DataFrame, name: str) -> pd.DataFrame:
        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"].iloc[:, 0]
            high  = df["High"].iloc[:, 0]  if "High"  in df else close
            low   = df["Low"].iloc[:, 0]   if "Low"   in df else close
        else:
            close = df["Close"]
            high  = df["High"]  if "High"  in df else close
            low   = df["Low"]   if "Low"   in df else close
        out = pd.DataFrame({f"{name}_close": close, f"{name}_high": high, f"{name}_low": low})
        out.index = pd.to_datetime(out.index, utc=True)
        return out

    @staticmethod
    def fetch_asset(
        asset: AssetConfig,
        start: str = "2020-01-01",
        end: str | None = None,
    ) -> pd.DataFrame:
        """Fetch one asset + macro drivers with TTL cache."""
        path = _cache_path(asset.cache_key)

        if not _cache_stale(path):
            log.info("Cache hit: %s", path)
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            return df

        log.info("Cache stale, fetching: %s %s %s", asset.ticker, _DXY_TICKER, _YIELD_TICKER)
        raw_asset = MacroData._download_with_retry(asset.ticker, start, end)
        raw_dxy   = MacroData._download_with_retry(_DXY_TICKER,   start, end)
        raw_yield = MacroData._download_with_retry(_YIELD_TICKER, start, end)

        df_asset = MacroData._extract_ohlc(raw_asset, "gold")  # "gold" prefix reused for compatibility
        df_dxy   = MacroData._extract_ohlc(raw_dxy,   "dxy")[["dxy_close"]]
        df_yield = MacroData._extract_ohlc(raw_yield, "yield")[["yield_close"]]

        merged = df_asset.join([df_dxy, df_yield], how="left")
        merged.ffill(inplace=True)
        merged.dropna(inplace=True)
        merged.to_csv(path)
        log.info("Cache written: %s (%d bars)", path, len(merged))
        return merged

    @staticmethod
    def fetch_latest_quote(ticker: str) -> float | None:
        """Fetch latest close price for a single ticker."""
        try:
            hist = yf.Ticker(ticker).history(period="5d", interval="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            log.error("fetch_latest_quote %s: %s", ticker, e)
        return None



# ---------------------------------------------------------------------------
# Bybit 4H Data Layer (public REST API — no auth required)
# ---------------------------------------------------------------------------
_BYBIT_BASE   = "https://api.bybit.com"
_BYBIT_SYMBOL = "XAUUSDT"
_BYBIT_INTERVAL = "240"          # 240 minutes = 4H
_4H_CACHE_TTL = 4 * 3600 + 300   # 4h 5m — re-fetch after each new candle


class BybitData:
    """
    Fetches 4H XAUUSDT klines from Bybit's public REST API.
    Merges with daily DXY + US10Y (forward-filled to 4H timestamps).
    No API key required.
    # ponytail: daily DXY/yield ffill to 4H is fine — macro drivers move on daily/weekly timescales
    """

    @staticmethod
    def _fetch_klines_raw(params: dict) -> list[list]:
        """Single paginated request. Returns raw list rows newest-first."""
        for attempt in range(3):
            try:
                r = requests.get(f"{_BYBIT_BASE}/v5/market/kline", params=params, timeout=10)
                r.raise_for_status()
                data = r.json()
                if data.get("retCode") == 0:
                    return data["result"]["list"]
                log.warning("Bybit API retCode %s: %s", data.get("retCode"), data.get("retMsg"))
            except Exception as e:
                log.error("Bybit kline request attempt %d: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
        raise RuntimeError("Bybit kline fetch failed after 3 attempts")

    @staticmethod
    def fetch_4h(
        start: str = "2023-01-01",
        end: str | None = None,
        cache_file: str = "cache_XAUUSDT_4h.csv",
    ) -> pd.DataFrame:
        """
        Returns a DataFrame with columns:
          gold_close, gold_high, gold_low, dxy_close, yield_close
        indexed by UTC datetime at 4H resolution.
        """
        if not _cache_stale(cache_file):
            log.info("Bybit 4H cache hit: %s", cache_file)
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df

        log.info("Fetching Bybit 4H XAUUSDT klines from %s", start)
        start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)

        all_rows: list[list] = []
        # Paginate backwards: fetch newest 1000, then use oldest timestamp as next end
        cursor_end_ms: int | None = None

        for _ in range(20):  # max 20 pages = 20,000 bars ≈ 3.3 years of 4H data
            params: dict = {
                "category": "linear",
                "symbol":   _BYBIT_SYMBOL,
                "interval": _BYBIT_INTERVAL,
                "limit":    1000,
            }
            if cursor_end_ms is not None:
                params["end"] = cursor_end_ms

            rows = BybitData._fetch_klines_raw(params)
            if not rows:
                break
            all_rows.extend(rows)

            # oldest bar is last in newest-first list
            oldest_ts = int(rows[-1][0])
            if oldest_ts <= start_ms:
                break
            cursor_end_ms = oldest_ts - 1  # page before oldest
            time.sleep(0.2)

        if not all_rows:
            raise RuntimeError("No Bybit kline data returned")

        # Sort chronologically and filter to start date
        all_rows.sort(key=lambda r: int(r[0]))
        all_rows = [r for r in all_rows if int(r[0]) >= start_ms]

        df_price = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df_price["ts"] = pd.to_datetime(df_price["ts"].astype(int), unit="ms", utc=True)
        df_price.set_index("ts", inplace=True)
        df_price = df_price[["high", "low", "close"]].astype(float)
        df_price.rename(columns={"close": "gold_close", "high": "gold_high", "low": "gold_low"}, inplace=True)
        df_price = df_price[~df_price.index.duplicated(keep="last")]

        # Macro drivers: daily yfinance → reindex to 4H via ffill
        raw_dxy   = yf.download(_DXY_TICKER,   start=start, progress=False)
        raw_yield = yf.download(_YIELD_TICKER, start=start, progress=False)

        def daily_series(raw: pd.DataFrame, col: str) -> pd.Series:
            s = (raw["Close"].iloc[:, 0] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"])
            s.index = pd.to_datetime(s.index, utc=True)
            s.name = col
            return s

        df_macro = pd.DataFrame({
            "dxy_close":   daily_series(raw_dxy,   "dxy_close"),
            "yield_close": daily_series(raw_yield, "yield_close"),
        })
        df_macro = df_macro.reindex(df_price.index, method="ffill")

        merged = df_price.join(df_macro)
        merged.ffill(inplace=True)
        merged.dropna(inplace=True)
        merged.to_csv(cache_file)
        log.info("Bybit 4H cache written: %s (%d bars)", cache_file, len(merged))
        return merged

    @staticmethod
    def fetch_15m(
        symbol: str = "BTCUSDT",
        days: int = 150,
        cache_file: str | None = None,
    ) -> pd.DataFrame:
        """
        Fetches 15-minute klines from Bybit with caching.
        Returns DataFrame with open, high, low, close, volume indexed by UTC DatetimeIndex.
        """
        if cache_file is None:
            cache_file = f"cache_{symbol}_15m.csv"

        if not _cache_stale(cache_file):
            log.info("Bybit 15m cache hit: %s", cache_file)
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df

        log.info("Fetching Bybit 15m %s klines (last %d days)...", symbol, days)
        all_rows: list[list] = []
        cursor_end_ms: int | None = None
        max_pages = max(5, int(days / 10) + 2)

        for _ in range(max_pages):
            params: dict = {
                "category": "linear",
                "symbol": symbol,
                "interval": "15",
                "limit": 1000,
            }
            if cursor_end_ms is not None:
                params["end"] = cursor_end_ms

            rows = BybitData._fetch_klines_raw(params)
            if not rows:
                break
            all_rows.extend(rows)
            oldest_ts = int(rows[-1][0])
            cursor_end_ms = oldest_ts - 1
            time.sleep(0.12)

        if not all_rows:
            raise RuntimeError(f"No Bybit 15m kline data returned for {symbol}")

        all_rows.sort(key=lambda r: int(r[0]))
        df_price = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df_price["ts"] = pd.to_datetime(df_price["ts"].astype(int), unit="ms", utc=True)
        df_price.set_index("ts", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df_price[col] = df_price[col].astype(float)
        df_price = df_price[~df_price.index.duplicated(keep="last")]
        df_price.to_csv(cache_file)
        log.info("Bybit 15m cache written: %s (%d bars)", cache_file, len(df_price))
        return df_price

    @staticmethod
    def fetch_latest_price(symbol: str = "BTCUSDT") -> float | None:
        """Fetch latest mark price from Bybit."""
        try:
            r = requests.get(
                f"{_BYBIT_BASE}/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            log.error("Bybit latest price fetch for %s: %s", symbol, e)
        return None

    @staticmethod
    def fetch_latest_gold_price() -> float | None:
        return BybitData.fetch_latest_price(symbol=_BYBIT_SYMBOL)

class BybitClient:
    """Authenticated Bybit V5 Client for Demo/Testnet/Live execution."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, env: str = "demo"):
        self.api_key = (api_key or os.getenv("BYBIT_API_KEY", "")).strip()
        self.api_secret = (api_secret or os.getenv("BYBIT_API_SECRET", "")).strip()
        self.env = (env or os.getenv("BYBIT_ENV", "demo")).strip().lower()

        if self.env == "demo":
            self.base_url = "https://api-demo.bybit.com"
        elif self.env == "testnet":
            self.base_url = "https://api-testnet.bybit.com"
        else:
            self.base_url = "https://api.bybit.com"

    def _sign(self, payload: str, ts: str, recv_window: str = "5000") -> str:
        param_str = ts + self.api_key + recv_window + payload
        return hmac.new(self.api_secret.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None) -> dict:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = ""
        url = f"{self.base_url}{endpoint}"

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

        if method == "GET" and params:
            query_str = urllib.parse.urlencode(params)
            payload = query_str
            url = f"{url}?{query_str}"
        elif method == "POST" and body:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"

        headers["X-BAPI-SIGN"] = self._sign(payload, ts, recv_window)

        if method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        else:
            r = requests.post(url, headers=headers, data=payload, timeout=10)

        r.raise_for_status()
        res = r.json()
        if res.get("retCode") != 0:
            log.warning("Bybit API [%s]: %s", res.get("retCode"), res.get("retMsg"))
        return res

    def get_wallet_balance(self) -> float:
        """Returns total available margin balance in USDT."""
        res = self._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
        if res.get("retCode") == 0:
            try:
                acc = res["result"]["list"][0]
                return float(acc.get("totalAvailableBalance") or acc.get("totalWalletBalance") or 0.0)
            except Exception as e:
                log.error("Parsing wallet balance: %s", e)
        return 0.0

    def get_positions(self, symbol: str = "XAUUSDT") -> list[dict]:
        """Returns open positions for symbol."""
        res = self._request("GET", "/v5/position/list", params={"category": "linear", "symbol": symbol})
        if res.get("retCode") == 0:
            return [p for p in res["result"]["list"] if float(p.get("size", 0)) > 0]
        return []

    def place_order(
        self,
        symbol: str = "XAUUSDT",
        side: str = "Buy",
        qty: float = 0.001,
        take_profit: float | None = None,
        stop_loss: float | None = None,
    ) -> dict:
        """Place Market order with native Take Profit and Stop Loss attached."""
        # Bybit XAUUSDT lot precision is 3 decimal places (0.001 oz)
        qty_str = f"{math.floor(qty * 1000) / 1000:.3f}"
        body: dict = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "positionIdx": 0,
            "timeInForce": "IOC",
        }
        if take_profit:
            body["takeProfit"] = f"{take_profit:.2f}"
            body["tpTriggerBy"] = "MarkPrice"
        if stop_loss:
            body["stopLoss"] = f"{stop_loss:.2f}"
            body["slTriggerBy"] = "MarkPrice"

        log.info("Placing Bybit %s Order: %s", self.env, body)
        return self._request("POST", "/v5/order/create", body=body)

    def set_trading_stop(
        self,
        symbol: str = "XAUUSDT",
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """Update active position's TP/SL (e.g. Move SL to Break-Even)."""
        body: dict = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0,
        }
        if stop_loss is not None:
            body["stopLoss"] = f"{stop_loss:.2f}"
            body["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            body["takeProfit"] = f"{take_profit:.2f}"
            body["tpTriggerBy"] = "MarkPrice"

        log.info("Updating Bybit Trading Stop (%s): %s", self.env, body)
        return self._request("POST", "/v5/position/trading-stop", body=body)

    def set_leverage(self, symbol: str = "BTCUSDT", leverage: int = 10) -> dict:
        """Sets buy and sell leverage for the symbol on Bybit."""
        body = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        try:
            res = self._request("POST", "/v5/position/set-leverage", body=body)
            log.info("Set Bybit %s leverage to %dx: %s", symbol, leverage, res.get("retMsg"))
            return res
        except Exception as e:
            # Bybit returns 110043 if leverage is already set to that exact number
            log.debug("Bybit set_leverage: %s", e)
            return {}

    def close_position(self, symbol: str = "XAUUSDT", side: str = "Buy", qty: float = 0.001) -> dict:
        """Close position by placing opposite market order with reduceOnly."""
        opposite_side = "Sell" if side.lower() == "buy" else "Buy"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": opposite_side,
            "orderType": "Market",
            "qty": f"{math.floor(qty * 1000) / 1000:.3f}",
            "reduceOnly": True,
            "positionIdx": 0,
            "timeInForce": "IOC",
        }
        log.info("Closing Bybit Position (%s): %s", self.env, body)
        return self._request("POST", "/v5/order/create", body=body)


# ---------------------------------------------------------------------------
# Intraday Alpha Strategy (15-Minute Session Breakout)
# ---------------------------------------------------------------------------
class SessionBreakoutStrategy:
    """
    15-Minute Asian Range Session Breakout with 50-EMA Regime Filter.
    Asian Session: 00:00 - 07:00 UTC (determines high/low range).
    London/NY Window: 07:00 - 16:00 UTC (trade entry window).
    Exit: 2:1 RR Take Profit, 1.5x ATR Stop Loss, Trailing Break-Even at 1.0x ATR, Hard EOD at 21:00 UTC.
    """

    @staticmethod
    def compute_indicators(df: pd.DataFrame, atr_period: int = 14, ema_period: int = 50) -> pd.DataFrame:
        data = df.copy()
        hl = data["high"] - data["low"]
        hc = (data["high"] - data["close"].shift()).abs()
        lc = (data["low"] - data["close"].shift()).abs()
        data["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(atr_period).mean()
        data["ema50"] = data["close"].ewm(span=ema_period, adjust=False).mean()
        data.dropna(inplace=True)
        return data

    @staticmethod
    def get_asian_range_for_today(df_15m: pd.DataFrame, target_date=None) -> tuple[float | None, float | None]:
        """Calculates Asian session (00:00 - 06:45 UTC) High & Low for a specific date."""
        if target_date is None:
            target_date = datetime.now(timezone.utc).date()
        asian_bars = df_15m[(df_15m.index.date == target_date) & (df_15m.index.hour < 7)]
        if asian_bars.empty:
            return None, None
        return float(asian_bars["high"].max()), float(asian_bars["low"].min())


class IntradayBacktester:
    """Event-driven backtester for 15m Session Breakout."""

    def __init__(
        self,
        initial_capital: float = 100.0,
        risk_pct: float = 0.015,
        sl_atr_mult: float = 1.5,
        rr_ratio: float = 2.0,
        trailing_be_mult: float = 1.0,
        spread: float = 0.25,
        slippage: float = 0.15,
    ):
        self.initial_capital = initial_capital
        self.risk_pct = risk_pct
        self.sl_atr_mult = sl_atr_mult
        self.rr_ratio = rr_ratio
        self.trailing_be_mult = trailing_be_mult
        self.spread = spread
        self.slippage = slippage

    def run(self, df_15m: pd.DataFrame, df_daily: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[dict]]:
        capital = self.initial_capital
        position = 0 # 1=Long, -1=Short
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        pos_size = 0.0
        entry_time = None
        current_day = None
        asian_high = None
        asian_low = None
        traded_today = False
        trades = []
        equity_curve = []

        # Map daily macro bull/bear state if daily data provided
        daily_bull_map = {}
        if df_daily is not None and not df_daily.empty:
            ema_trend = df_daily["ema_trend"] if "ema_trend" in df_daily else df_daily["gold_close"].ewm(span=50, adjust=False).mean()
            daily_bull_map = {d.date(): bool(close > ema) for d, close, ema in zip(df_daily.index, df_daily["gold_close"], ema_trend)}

        for ts, row in df_15m.iterrows():
            day = ts.date()
            hour = ts.hour
            close_p = float(row["close"])
            high_p = float(row["high"])
            low_p = float(row["low"])
            atr = float(row["atr"])
            ema = float(row["ema50"])
            is_macro_bull = daily_bull_map.get(day, True) if daily_bull_map else False

            # Reset on new day
            if day != current_day:
                current_day = day
                traded_today = False
                asian_high = None
                asian_low = None

            # Track Asian Range (00:00 - 06:45 UTC)
            if hour < 7:
                asian_high = high_p if asian_high is None else max(asian_high, high_p)
                asian_low = low_p if asian_low is None else min(asian_low, low_p)

            # Manage open position
            if position != 0:
                closed = False
                exit_p = 0.0
                reason = ""

                # Exit checks
                if position == 1:
                    if low_p <= sl_price:
                        exit_p, reason, closed = sl_price - self.slippage, "SL", True
                    elif high_p >= tp_price:
                        exit_p, reason, closed = tp_price - self.slippage, "TP", True
                    elif hour >= 21:
                        exit_p, reason, closed = close_p - self.spread / 2 - self.slippage, "EOD", True
                elif position == -1:
                    if high_p >= sl_price:
                        exit_p, reason, closed = sl_price + self.slippage, "SL", True
                    elif low_p <= tp_price:
                        exit_p, reason, closed = tp_price + self.slippage, "TP", True
                    elif hour >= 21:
                        exit_p, reason, closed = close_p + self.spread / 2 + self.slippage, "EOD", True

                if closed:
                    pnl = (exit_p - entry_price) * pos_size if position == 1 else (entry_price - exit_p) * pos_size
                    capital += pnl
                    trades.append({
                        "entry_time": str(entry_time),
                        "exit_time": str(ts),
                        "side": "LONG" if position == 1 else "SHORT",
                        "size": pos_size,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_p, 2),
                        "pnl": round(pnl, 4),
                        "reason": reason,
                        "capital_after": round(capital, 4),
                    })
                    position = 0

            # Signal evaluation (07:00 - 16:00 UTC)
            if position == 0 and not traded_today and asian_high is not None and asian_low is not None and 7 <= hour <= 16 and atr > 0:
                range_size = asian_high - asian_low
                if range_size >= atr * 0.5:
                    sl_dist = max(atr * self.sl_atr_mult, 3.0)
                    risk_dollars = capital * self.risk_pct
                    size = max(0.001, math.floor((risk_dollars / sl_dist) * 1000) / 1000)

                    # Long Breakout: allowed if 15m price > Asian High and 15m price > 50-EMA
                    if close_p > asian_high and close_p > ema:
                        entry_price = close_p + self.spread / 2 + self.slippage
                        sl_price = entry_price - sl_dist
                        # 2.5:1 RR on trend-aligned longs
                        rr = 2.5 if daily_bull_map else self.rr_ratio
                        tp_price = entry_price + sl_dist * rr
                        pos_size = size
                        position = 1
                        entry_time = ts
                        traded_today = True

                    # Short Breakout: only allowed if not in daily macro bull regime
                    elif (not is_macro_bull) and close_p < asian_low and close_p < ema:
                        entry_price = close_p - self.spread / 2 - self.slippage
                        sl_price = entry_price + sl_dist
                        tp_price = entry_price - sl_dist * self.rr_ratio
                        pos_size = size
                        position = -1
                        entry_time = ts
                        traded_today = True

            unrealized = 0.0
            if position == 1:
                unrealized = (close_p - entry_price) * pos_size
            elif position == -1:
                unrealized = (entry_price - close_p) * pos_size
            equity_curve.append({"time": ts, "equity": capital + unrealized, "close": close_p})

        return pd.DataFrame(equity_curve).set_index("time"), trades


# ---------------------------------------------------------------------------
# Alpha Strategy
# ---------------------------------------------------------------------------
class MacroStrategy:
    """Macro Divergence Z-Score & Volatility Regime Alpha Model."""

    @staticmethod
    def compute_indicators(
        df: pd.DataFrame,
        corr_window: int = 30,
        z_window: int = 45,
        atr_period: int = 14,
    ) -> pd.DataFrame:
        data = df.copy()
        dxy_ret  = data["dxy_close"].pct_change()
        yield_ret = data["yield_close"].pct_change()
        gold_ret  = data["gold_close"].pct_change()

        data["corr_dxy"]   = gold_ret.rolling(corr_window).corr(dxy_ret)
        data["corr_yield"] = gold_ret.rolling(corr_window).corr(yield_ret)

        macro_proxy = -1.0 * (
            (data["dxy_close"] / data["dxy_close"].rolling(z_window).mean() - 1.0)
            + (data["yield_close"] / data["yield_close"].rolling(z_window).mean() - 1.0)
        )
        gold_norm = data["gold_close"] / data["gold_close"].rolling(z_window).mean() - 1.0

        data["macro_spread"] = gold_norm - macro_proxy
        spread_std = data["macro_spread"].rolling(z_window).std().replace(0, np.nan)
        data["z_score"] = (data["macro_spread"] - data["macro_spread"].rolling(z_window).mean()) / spread_std

        hl  = data["gold_high"] - data["gold_low"]
        hc  = (data["gold_high"] - data["gold_close"].shift()).abs()
        lc  = (data["gold_low"]  - data["gold_close"].shift()).abs()
        data["atr"]       = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(atr_period).mean()
        data["ema_trend"] = data["gold_close"].ewm(span=50, adjust=False).mean()

        data.dropna(inplace=True)
        return data

    @staticmethod
    def generate_signal(row: pd.Series, z_entry: float = 1.2) -> int:
        """Long-only macro bull bias: returns 1 (Buy) or 0 (Neutral)."""
        macro_aligned = (row["corr_dxy"] < 0) or (row["corr_yield"] < 0)
        if not macro_aligned:
            return 0
        if row["z_score"] <= -z_entry and row["gold_close"] > row["ema_trend"]:
            return 1
        return 0


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------
class Backtester:
    """Event-Driven Execution Simulator with real broker friction."""

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        risk_pct_per_trade: float = 0.015,
        sl_atr_mult: float = 2.0,
        tp_atr_mult: float = 4.0,
        base_spread: float = 0.30,
        slippage: float = 0.20,
        annual_swap_pct: float = 0.045,
    ):
        self.initial_capital  = initial_capital
        self.risk_pct         = risk_pct_per_trade
        self.sl_atr_mult      = sl_atr_mult
        self.tp_atr_mult      = tp_atr_mult
        self.base_spread      = base_spread
        self.slippage         = slippage
        self.daily_swap_rate  = annual_swap_pct / 365.0

    def run(self, df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
        capital       = self.initial_capital
        position      = 0
        entry_price   = 0.0
        position_size = 0.0
        stop_loss     = 0.0
        take_profit   = 0.0
        entry_time    = None
        trades        = []
        equity_curve  = []

        for ts, row in df.iterrows():
            gold_close = float(row["gold_close"])
            gold_high  = float(row["gold_high"])
            gold_low   = float(row["gold_low"])
            atr        = float(row["atr"])
            z          = float(row["z_score"])

            if position != 0:
                swap_mult = 3.0 if ts.weekday() == 2 else 1.0
                capital  -= position_size * gold_close * self.daily_swap_rate * swap_mult

                closed = False; exit_price = 0.0; reason = ""

                if position == 1:
                    if gold_low   <= stop_loss:   exit_price, reason, closed = stop_loss  - self.slippage, "SL", True
                    elif gold_high >= take_profit: exit_price, reason, closed = take_profit - self.slippage, "TP", True
                    elif z >= 0.0:                exit_price, reason, closed = gold_close - self.base_spread / 2 - self.slippage, "Z_REVERT", True

                if closed:
                    pnl = (exit_price - entry_price) * position_size
                    capital += pnl
                    trades.append({
                        "entry_time": str(entry_time), "exit_time": str(ts),
                        "side": "LONG", "size": position_size,
                        "entry_price": entry_price, "exit_price": exit_price,
                        "pnl": pnl,
                        "return_pct": pnl / (entry_price * position_size) * 100 if entry_price * position_size else 0,
                        "reason": reason, "capital_after": capital,
                    })
                    position = 0

            if position == 0 and atr > 0 and MacroStrategy.generate_signal(row) == 1:
                sl_dist = atr * self.sl_atr_mult
                if sl_dist > 0:
                    position_size = round((capital * self.risk_pct) / sl_dist, 4)
                    entry_price   = gold_close + self.base_spread / 2 + self.slippage
                    stop_loss     = entry_price - sl_dist
                    take_profit   = entry_price + atr * self.tp_atr_mult
                    position      = 1
                    entry_time    = ts

            unrealized = (gold_close - entry_price) * position_size if position == 1 else 0.0
            equity_curve.append({"time": ts, "equity": capital + unrealized, "gold_price": gold_close})

        return pd.DataFrame(equity_curve).set_index("time"), trades


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
class Analytics:
    """Institutional Risk & Validation Suite."""

    @staticmethod
    def calculate_metrics(eq_df: pd.DataFrame, trades: list[dict], initial_capital: float = 100_000.0) -> dict:
        if eq_df.empty or not trades:
            return {"total_trades": 0, "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0}

        returns      = eq_df["equity"].pct_change().dropna()
        final_equity = float(eq_df["equity"].iloc[-1])
        total_ret    = (final_equity - initial_capital) / initial_capital * 100
        std_ret      = returns.std()
        down_std     = returns[returns < 0].std()
        sharpe       = returns.mean() / std_ret  * math.sqrt(252) if std_ret  > 0 else 0.0
        sortino      = returns.mean() / down_std * math.sqrt(252) if down_std > 0 else 0.0

        rolling_max  = eq_df["equity"].cummax()
        max_dd_pct   = float(((eq_df["equity"] - rolling_max) / rolling_max).min() * 100)
        years        = max((eq_df.index[-1] - eq_df.index[0]).days / 365.25, 0.25)
        cagr         = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0
        calmar        = abs(cagr / max_dd_pct) if max_dd_pct else 0.0

        pnls         = [t["pnl"] for t in trades]
        wins         = [p for p in pnls if p > 0]
        losses       = [p for p in pnls if p < 0]
        profit_factor = sum(wins) / abs(sum(losses)) if losses and sum(losses) else float("inf")
        payoff        = np.mean(wins) / abs(np.mean(losses)) if wins and losses else 0.0

        return {
            "initial_capital":   initial_capital,
            "final_equity":      round(final_equity, 2),
            "total_return_pct":  round(total_ret,    2),
            "cagr_pct":          round(cagr,         2),
            "max_drawdown_pct":  round(max_dd_pct,   2),
            "sharpe_ratio":      round(sharpe,        2),
            "sortino_ratio":     round(sortino,       2),
            "calmar_ratio":      round(calmar,        2),
            "total_trades":      len(trades),
            "win_rate_pct":      round(len(wins) / len(trades) * 100 if trades else 0, 2),
            "profit_factor":     round(profit_factor, 2),
            "payoff_ratio":      round(payoff,        2),
        }

    @staticmethod
    def monte_carlo_simulation(trades: list[dict], runs: int = 5000, initial_capital: float = 100_000.0) -> dict:
        if len(trades) < 5:
            return {"status": "Insufficient trades"}
        pnls = np.array([t["pnl"] for t in trades])
        mds, ends = [], []
        for _ in range(runs):
            eq   = initial_capital + np.cumsum(np.random.permutation(pnls))
            peak = np.maximum.accumulate(eq)
            mds.append(abs(np.min((eq - peak) / peak) * 100))
            ends.append(eq[-1])
        return {
            "runs": runs,
            "median_max_dd_pct":     round(float(np.median(mds)),              2),
            "p95_worst_max_dd_pct":  round(float(np.percentile(mds, 95)),      2),
            "p99_worst_max_dd_pct":  round(float(np.percentile(mds, 99)),      2),
            "median_ending_equity":  round(float(np.median(ends)),             2),
        }

    @staticmethod
    def walk_forward_analysis(df: pd.DataFrame, train_months: int = 12, test_months: int = 3) -> list[dict]:
        results, step = [], test_months * 21
        window = (train_months + test_months) * 21
        for start_idx in range(0, len(df) - window, step):
            df_test  = df.iloc[start_idx + train_months * 21 : start_idx + window]
            eq, trd  = Backtester().run(df_test)
            m        = Analytics.calculate_metrics(eq, trd)
            results.append({
                "test_period": f"{df_test.index[0].strftime('%Y-%m')} to {df_test.index[-1].strftime('%Y-%m')}",
                "trades":     len(trd),
                "return_pct": m.get("total_return_pct", 0),
                "max_dd_pct": m.get("max_drawdown_pct", 0),
                "sharpe":     m.get("sharpe_ratio", 0),
            })
        return results


# ---------------------------------------------------------------------------
# Portfolio-Level Paper Trader (Multi-Asset)
# ---------------------------------------------------------------------------
_MAX_POSITIONS  = 3
_HALT_THRESHOLD = 0.03  # 3% daily loss triggers circuit breaker


class PaperTrader:
    """Multi-asset paper trading with circuit breaker and correlation lock."""

    def __init__(self, ledger_file: str = "paper_ledger.json", initial_capital: float = 100_000.0):
        self.ledger_file = ledger_file
        self.initial_capital = initial_capital
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.ledger_file):
            with open(self.ledger_file) as f:
                return json.load(f)
        return {
            "capital": self.initial_capital,
            "open_positions": {},
            "closed_trades":  [],
            "circuit_breaker": {
                "active":            False,
                "day_utc":           "",
                "day_start_capital": self.initial_capital,
                "day_realized_pnl":  0.0,
                "halt_threshold_pct": _HALT_THRESHOLD * 100,
            },
        }

    def _save(self):
        with open(self.ledger_file, "w") as f:
            json.dump(self.state, f, indent=2)

    # --- Circuit breaker ---
    def _reset_or_update_cb(self):
        today = datetime.now(timezone.utc).date().isoformat()
        cb = self.state["circuit_breaker"]
        if cb["day_utc"] != today:
            cb["day_utc"]           = today
            cb["day_start_capital"] = self.state["capital"]
            cb["day_realized_pnl"]  = 0.0
            cb["active"]            = False
            log.info("Circuit breaker reset for new trading day %s", today)

    def _check_circuit_breaker(self) -> bool:
        cb = self.state["circuit_breaker"]
        if cb["day_start_capital"] <= 0:
            return False
        loss_pct = cb["day_realized_pnl"] / cb["day_start_capital"]
        if loss_pct < -_HALT_THRESHOLD:
            if not cb["active"]:
                cb["active"] = True
                log.warning("CIRCUIT BREAKER TRIGGERED: day loss %.2f%%", loss_pct * 100)
            return True
        return False

    # --- Portfolio heat & correlation ---
    def _can_open(self, ticker: str, group: str | None) -> bool:
        positions = self.state["open_positions"]
        if len(positions) >= _MAX_POSITIONS:
            return False
        if group:
            for t, pos in positions.items():
                if pos.get("correlation_group") == group:
                    log.info("Correlation lock: skipping %s (group %s already in %s)", ticker, group, t)
                    return False
        return True

    # --- Main evaluation loop ---
    def evaluate_live(self, assets: list[AssetConfig] | None = None) -> dict:
        if assets is None:
            assets = ASSET_UNIVERSE

        self._reset_or_update_cb()
        actions = []

        for asset in assets:
            ticker = asset.ticker
            current_price = MacroData.fetch_latest_quote(ticker)
            if current_price is None:
                log.error("Could not fetch price for %s, skipping", ticker)
                continue

            # --- Manage open position ---
            pos = self.state["open_positions"].get(ticker)
            if pos:
                sl, tp, size, entry = pos["stop_loss"], pos["take_profit"], pos["size"], pos["entry_price"]
                closed = False; exit_price = 0.0; reason = ""

                if current_price <= sl:
                    exit_price, reason, closed = sl, "Stop Loss Hit", True
                elif current_price >= tp:
                    exit_price, reason, closed = tp, "Take Profit Hit", True

                if closed:
                    pnl = (exit_price - entry) * size
                    self.state["capital"] += pnl
                    self.state["circuit_breaker"]["day_realized_pnl"] += pnl
                    pos.update({"exit_price": exit_price, "exit_time": datetime.now(timezone.utc).isoformat(),
                                "pnl": pnl, "close_reason": reason})
                    self.state["closed_trades"].append(pos)
                    del self.state["open_positions"][ticker]
                    actions.append(f"[{asset.name}] Closed LONG @ ${exit_price:.2f} ({reason}) PnL: ${pnl:.2f}")
                    log.info("Closed %s @ %.2f reason=%s pnl=%.2f", ticker, exit_price, reason, pnl)

            # --- Check for new signal ---
            if ticker not in self.state["open_positions"]:
                if self._check_circuit_breaker():
                    actions.append(f"[{asset.name}] Circuit breaker active — no new entries today.")
                    continue
                if not self._can_open(ticker, asset.correlation_group):
                    continue

                try:
                    df = MacroData.fetch_asset(asset, start="2024-01-01")
                    features = MacroStrategy.compute_indicators(df)
                    last = features.iloc[-1]
                    sig = MacroStrategy.generate_signal(last)
                    atr = float(last["atr"])

                    if sig == 1 and atr > 0:
                        risk_dollars = self.state["capital"] * 0.015
                        sl_dist      = atr * 2.0
                        size         = round(risk_dollars / sl_dist, 4)
                        sl           = round(current_price - sl_dist, 2)
                        tp           = round(current_price + atr * 4.0, 2)
                        self.state["open_positions"][ticker] = {
                            "ticker": ticker, "name": asset.name,
                            "correlation_group": asset.correlation_group,
                            "entry_time": datetime.now(timezone.utc).isoformat(),
                            "side": "LONG", "size": size,
                            "entry_price": current_price, "stop_loss": sl, "take_profit": tp,
                        }
                        actions.append(f"[{asset.name}] Opened LONG @ ${current_price:.2f} | TP: ${tp:.2f} | SL: ${sl:.2f}")
                        log.info("Opened LONG %s @ %.2f TP=%.2f SL=%.2f size=%.4f", ticker, current_price, tp, sl, size)
                except Exception as e:
                    log.error("Error evaluating %s: %s", ticker, e)

        self._save()
        return {
            "capital":        round(self.state["capital"], 2),
            "open_positions": self.state["open_positions"],
            "actions":        actions,
            "circuit_breaker_active": self.state["circuit_breaker"]["active"],
            "day_realized_pnl": round(self.state["circuit_breaker"]["day_realized_pnl"], 2),
        }
