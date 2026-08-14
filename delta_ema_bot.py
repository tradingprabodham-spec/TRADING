#!/usr/bin/env python3
"""
delta_ema_bot.py — 9/200 & 21/200 EMA crossover auto-trader for BTCUSD + XAUTUSD on Delta Exchange.
Runs BOTH symbols simultaneously, independently, in the same process.

Strategy (as specified, matches the dashboard exactly):
  - On 9/200 EMA crossover  -> open 1 contract (buy on bullish cross, sell/short on bearish cross)
  - On 21/200 EMA crossover -> open an ADDITIONAL 1 contract, same rules
  - Each leg: target/stoploss are 1:1 risk-reward, but sized per-symbol:
      BTCUSD  -> target/stoploss = entry +/- $1000 (plain USD points, unchanged)
      XAUTUSD -> target/stoploss = entry +/- (Rs.1000 / USD_INR_RATE)  (gold moves far less than $1000)
  - Market orders for entry and exit, 1 whole contract per leg (Delta usually requires integer sizes)
  - Runs continuously (designed for a VPS / cloud box, not your laptop)
  - Persists open-position state to disk so a restart doesn't lose track of trades
  - Optional Telegram notifications on open/close (arrive on your phone, not just this machine)

SAFETY:
  - Defaults to Delta's TESTNET (fake funds). Change DELTA_ENV to "live" only when you're ready.
  - DRY_RUN=true logs every signal/exit WITHOUT placing any order — use this first, even on testnet.
  - Never hardcode your API key/secret in this file. Use environment variables (see config.example.env).
"""

import os
import sys
import time
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (all from environment variables — see config.example.env)
# ---------------------------------------------------------------------------
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")
DELTA_ENV        = os.environ.get("DELTA_ENV", "testnet").lower()          # "testnet" or "live"
TIMEFRAME        = os.environ.get("TIMEFRAME", "1d")                       # "1d" or "15m"

TRADE_BTC        = os.environ.get("TRADE_BTC", "true").lower() == "true"
TRADE_XAUT       = os.environ.get("TRADE_XAUT", "true").lower() == "true"

BTC_TARGET_USD   = float(os.environ.get("BTC_TARGET_USD", "1000"))         # plain $ points, unchanged from original
XAUT_TARGET_INR  = float(os.environ.get("XAUT_TARGET_INR", "1000"))        # Rs target, converted below
USD_INR_RATE     = float(os.environ.get("USD_INR_RATE", "83"))             # update to today's rate for accuracy
XAUT_TARGET_USD  = XAUT_TARGET_INR / USD_INR_RATE

CONTRACT_SIZE    = int(os.environ.get("CONTRACT_SIZE", "1"))               # whole contracts — Delta usually rejects fractions
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() == "true"     # NEVER places real orders when true
PRICE_POLL_SECS  = int(os.environ.get("PRICE_POLL_SECS", "15"))            # how often to check target/stoploss
EMA_POLL_SECS    = int(os.environ.get("EMA_POLL_SECS", "300"))             # how often to re-check for new crossovers
STATE_FILE       = Path(os.environ.get("STATE_FILE", "bot_state.json"))
LOG_FILE         = Path(os.environ.get("LOG_FILE", "bot.log"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")  # optional, but strongly recommended for 24/7 running
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")    # optional

BASE_URL = "https://cdn-ind.testnet.deltaex.org" if DELTA_ENV == "testnet" else "https://api.india.delta.exchange"

# Per-symbol config, built from the above
SYMBOLS = {}
if TRADE_BTC:
    SYMBOLS["BTCUSD"] = {"target_move": BTC_TARGET_USD, "currency": "$", "display_target": f"${BTC_TARGET_USD:.0f}"}
if TRADE_XAUT:
    SYMBOLS["XAUTUSD"] = {"target_move": XAUT_TARGET_USD, "currency": "Rs", "display_target": f"Rs{XAUT_TARGET_INR:,.0f} (approx ${XAUT_TARGET_USD:.2f})"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("delta_ema_bot")


def notify(title: str, body: str):
    """Log always; also push to Telegram if configured (arrives on your phone)."""
    log.info("NOTIFY: %s - %s", title, body)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            log.warning("Telegram notify failed: %s", e)


# ---------------------------------------------------------------------------
# Delta Exchange REST client
# ---------------------------------------------------------------------------
class DeltaClient:
    def __init__(self, api_key, api_secret, base_url):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self._product_cache = {}

    def _sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method + timestamp + path + query + body
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return timestamp, signature

    def _request(self, method, path, query="", body=None, auth=True):
        body_str = json.dumps(body) if body else ""
        url = self.base_url + path + query
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            timestamp, signature = self._sign(method, path, query, body_str)
            headers.update({"api-key": self.api_key, "signature": signature, "timestamp": timestamp})
        resp = self.session.request(method, url, headers=headers, data=body_str or None, timeout=15)
        data = resp.json()
        if not resp.ok or data.get("success") is False:
            err = data.get("error", {})
            raise RuntimeError(f"{method} {path} failed: {err.get('code') or err.get('message') or resp.status_code}")
        return data

    # ---- public endpoints (no signing needed) ----
    def ticker(self, symbol):
        return self._request("GET", f"/v2/tickers/{symbol}", auth=False)["result"]

    def candles(self, symbol, resolution, start_ts, end_ts):
        query = f"?resolution={resolution}&symbol={symbol}&start={start_ts}&end={end_ts}"
        return self._request("GET", "/v2/history/candles", query=query, auth=False)["result"]

    def product(self, symbol):
        if symbol not in self._product_cache:
            self._product_cache[symbol] = self._request("GET", f"/v2/products/{symbol}", auth=False)["result"]
        return self._product_cache[symbol]

    # ---- private (signed) endpoints ----
    def place_market_order(self, symbol, side, size):
        product = self.product(symbol)
        body = {"product_id": product["id"], "size": size, "side": side, "order_type": "market_order"}
        return self._request("POST", "/v2/orders", body=body)["result"]

    def balances(self):
        return self._request("GET", "/v2/wallet/balances")["result"]

    def positions(self):
        return self._request("GET", "/v2/positions/margined")["result"]


# ---------------------------------------------------------------------------
# EMA math
# ---------------------------------------------------------------------------
def ema(closes, period):
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    k = 2 / (period + 1)
    for i in range(period, len(closes)):
        out[i] = closes[i] * k + out[i - 1] * (1 - k)
    return out


def crossed_at(i, fast, slow):
    """Returns 'bull', 'bear', or None for whether fast crossed slow going INTO index i."""
    if i < 1 or None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return None
    now = "bull" if fast[i] > slow[i] else "bear"
    prev = "bull" if fast[i - 1] > slow[i - 1] else "bear"
    return now if now != prev else None


# ---------------------------------------------------------------------------
# State persistence (so a restart doesn't lose open positions) — keyed per symbol
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    for sym in SYMBOLS:
        state.setdefault(sym, {"positions": [], "log": [], "last_candle_time": None})
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Bot core
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self):
        self.client = DeltaClient(DELTA_API_KEY, DELTA_API_SECRET, BASE_URL)
        self.state = load_state()

    def open_position(self, symbol, leg, side):
        cfg = SYMBOLS[symbol]
        sym_state = self.state[symbol]
        entry_price = None
        try:
            if DRY_RUN:
                log.info("[DRY_RUN] Would place %s order on %s, size=%s, leg=%s", side, symbol, CONTRACT_SIZE, leg)
                t = self.client.ticker(symbol)
                entry_price = float(t.get("close") or t.get("mark_price"))
            else:
                order = self.client.place_market_order(symbol, side, CONTRACT_SIZE)
                entry_price = float(order.get("average_fill_price") or self.client.ticker(symbol)["close"])
        except Exception as e:
            notify("Bot: order failed", f"{symbol} {leg} {side} entry failed - {e}")
            return

        move = cfg["target_move"]
        target = entry_price + move if side == "buy" else entry_price - move
        stoploss = entry_price - move if side == "buy" else entry_price + move
        pos = {
            "leg": leg, "side": side, "entry": entry_price, "target": target,
            "stoploss": stoploss, "size": CONTRACT_SIZE,
            "open_time": datetime.now(timezone.utc).isoformat(),
        }
        sym_state["positions"].append(pos)
        save_state(self.state)
        notify(
            f"Bot: {symbol} {leg} trade opened{' [DRY RUN]' if DRY_RUN else ''}",
            f"{side.upper()} {CONTRACT_SIZE} {symbol} @ ${entry_price:.2f} - target/SL +/-{cfg['display_target']} - target ${target:.2f} - SL ${stoploss:.2f}",
        )

    def close_position(self, symbol, pos, reason, exit_price):
        cfg = SYMBOLS[symbol]
        sym_state = self.state[symbol]
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            if not DRY_RUN:
                self.client.place_market_order(symbol, close_side, pos["size"])
        except Exception as e:
            notify("Bot: close order failed", f"{symbol} {pos['leg']} close failed - {e}")
            return

        pnl_usd = (exit_price - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - exit_price)
        pnl_usd *= pos["size"]
        # BTC P&L shown in $, XAUT P&L shown in Rs (converted), matching the dashboard
        pnl_display = pnl_usd if symbol == "BTCUSD" else pnl_usd * USD_INR_RATE

        sym_state["positions"] = [p for p in sym_state["positions"] if p is not pos]
        sym_state["log"].append({**pos, "exit": exit_price, "reason": reason, "pnl": pnl_display,
                                  "close_time": datetime.now(timezone.utc).isoformat()})
        save_state(self.state)
        notify(
            f"Bot: {symbol} {pos['leg']} trade closed ({reason}){' [DRY RUN]' if DRY_RUN else ''}",
            f"{pos['side'].upper()} closed @ ${exit_price:.2f} - P&L {cfg['currency']}{pnl_display:.2f}",
        )

    def check_signals(self, symbol):
        """Fetch latest candles for one symbol, compute EMAs, open new legs on fresh crossovers."""
        sym_state = self.state[symbol]
        end_ts = int(time.time())
        lookback_days = 15 if TIMEFRAME == "15m" else 260
        start_ts = end_ts - lookback_days * 24 * 60 * 60
        try:
            candles = self.client.candles(symbol, TIMEFRAME, start_ts, end_ts)
        except Exception as e:
            log.warning("[%s] Failed to fetch candles: %s", symbol, e)
            return
        candles = sorted(candles, key=lambda c: c["time"])
        if len(candles) < 205:
            log.warning("[%s] Only %d candles available, need 200+.", symbol, len(candles))
            return

        closes = [c["close"] for c in candles]
        e9, e21, e200 = ema(closes, 9), ema(closes, 21), ema(closes, 200)
        i = len(closes) - 1

        # Skip if we've already processed this candle (avoid re-firing on same bar every poll)
        latest_time = candles[-1]["time"]
        if sym_state.get("last_candle_time") == latest_time:
            return
        sym_state["last_candle_time"] = latest_time
        save_state(self.state)

        c9200 = crossed_at(i, e9, e200)
        c21200 = crossed_at(i, e21, e200)

        if c9200 and not any(p["leg"] == "9/200" for p in sym_state["positions"]):
            self.open_position(symbol, "9/200", "buy" if c9200 == "bull" else "sell")
        if c21200 and not any(p["leg"] == "21/200" for p in sym_state["positions"]):
            self.open_position(symbol, "21/200", "buy" if c21200 == "bull" else "sell")

    def check_positions(self, symbol):
        """Poll live price for one symbol, close legs that hit target/stoploss."""
        sym_state = self.state[symbol]
        if not sym_state["positions"]:
            return
        try:
            t = self.client.ticker(symbol)
            price = float(t.get("close") or t.get("mark_price"))
        except Exception as e:
            log.warning("[%s] Failed to fetch live price: %s", symbol, e)
            return

        for pos in list(sym_state["positions"]):
            if pos["side"] == "buy":
                if price >= pos["target"]:
                    self.close_position(symbol, pos, "Target hit", price)
                elif price <= pos["stoploss"]:
                    self.close_position(symbol, pos, "Stoploss hit", price)
            else:
                if price <= pos["target"]:
                    self.close_position(symbol, pos, "Target hit", price)
                elif price >= pos["stoploss"]:
                    self.close_position(symbol, pos, "Stoploss hit", price)

    def run(self):
        if not DELTA_API_KEY or not DELTA_API_SECRET:
            log.error("DELTA_API_KEY / DELTA_API_SECRET not set. See config.example.env. Exiting.")
            sys.exit(1)
        if not SYMBOLS:
            log.error("No symbols enabled (TRADE_BTC and TRADE_XAUT are both false). Exiting.")
            sys.exit(1)

        mode = "DRY RUN (no orders will be placed)" if DRY_RUN else ("TESTNET" if DELTA_ENV == "testnet" else "LIVE - REAL MONEY")
        summary = " | ".join(f"{s}: {SYMBOLS[s]['display_target']}" for s in SYMBOLS)
        notify("Bot started", f"{summary} - {TIMEFRAME} - 1:1 risk-reward - mode: {mode}")

        last_ema_check = 0
        while True:
            try:
                now = time.time()
                if now - last_ema_check >= EMA_POLL_SECS:
                    for symbol in SYMBOLS:
                        self.check_signals(symbol)
                    last_ema_check = now
                for symbol in SYMBOLS:
                    self.check_positions(symbol)
            except Exception as e:
                log.exception("Unhandled error in main loop: %s", e)
            time.sleep(PRICE_POLL_SECS)


if __name__ == "__main__":
    Bot().run()
