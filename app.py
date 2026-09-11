"""
=====================================================================
 AI SIGNALS BOT — MERGED SERVER + MULTI-BOT ENGINE   (PART 1/4)
=====================================================================
Server aur bot engine ab EK hi file mein hain.
5 alag threads (Bot 1 -> Bot 5), har ek apni strategy follow karta hai.

NAYI CHEEZEIN (is part mein):
  1. Multi-timeframe numerical trend filter (1h + 4h EMA50/EMA200) -
     sirf 15m chart image par blind reliance khatam.
  2. Har bot ka apna strategy config (trend-follow / reversal /
     breakout / scalp / conservative-swing).
  3. Pakistan time (PKT) ke mutabiq dynamic SL/TP margin - sham
     5-6 baje ke baad wider stops (high-volatility session).
  4. Signal ab bot_id + bot_name ke sath store hota hai, taake
     dashboard har bot ko alag dikha sake.
  5. Background "position monitor" thread jo open signals ko live
     price ke against track karta hai, TP/SL hit hone par close karta
     hai aur per-bot win/loss stats banata hai (comparison table +
     winner bot ke liye zaroori).

Run: python app.py
Dashboard: http://127.0.0.1:5055/
=====================================================================
"""

import os, time, requests, json, base64, io, re, math, threading, hmac, hashlib
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# API KEYS — loaded from environment variables (.env locally, or
# Render/host "Environment" settings in production). NEVER hardcode
# real keys in this file — this file may end up in a public repo.
# =====================================================================
RAW_GEMINI_KEYS = os.getenv("GEMINI_KEYS", "")
RAW_GROQ_KEYS = os.getenv("GROQ_KEYS", "")
RAW_MISTRAL_KEYS = os.getenv("MISTRAL_KEYS", "")

# --- AI on/off master switch ---
# AI_ENABLED=true  -> bots use Gemini/Groq/Mistral vision models (as before)
# AI_ENABLED=false -> bots use pure technical-indicator rules, no AI calls at all
AI_ENABLED = os.getenv("AI_ENABLED", "false").strip().lower() == "true"

# --- Discord webhook (optional) — if set, every new signal is posted there ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# --- Public Binance REST mirrors — some cloud regions (incl. Render's US
# datacenters) get rate-limited/blocked on api.binance.com alone, so we
# try each mirror in order until one responds. ---
BINANCE_MIRRORS = [
    "https://data-api.binance.vision/api/v3",
    "https://api1.binance.com/api/v3",
    "https://api.binance.com/api/v3",
    "https://api.binance.me/api/v3",
]
BINANCE_TIMEOUT = 6

GEMINI_KEYS = [k.strip() for k in RAW_GEMINI_KEYS.split(",") if k.strip()]
GROQ_KEYS = [k.strip() for k in RAW_GROQ_KEYS.split(",") if k.strip()]
MISTRAL_KEYS = [k.strip() for k in RAW_MISTRAL_KEYS.split(",") if k.strip()]

_key_locks = {"gemini": threading.Lock(), "groq": threading.Lock(), "mistral": threading.Lock()}
_key_idx = {"gemini": 0, "groq": 0, "mistral": 0}


def _next_key(name, pool):
    if not pool:
        return None
    with _key_locks[name]:
        key = pool[_key_idx[name] % len(pool)]
        _key_idx[name] += 1
    return key


# NOTE: MATICUSDT was replaced with POLUSDT - Binance delisted MATICUSDT after
# Polygon's MATIC -> POL token migration/rebrand. Keeping the old symbol here
# would make every bot silently fail on that entry (no klines, no live price),
# and any signal generated for it before the rebrand could never close (no
# live price to hit TP/SL against) - see the monitor age-expiry fix in
# bot_engine.py for the safety net on that second half of the problem.
TOP_50_COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "SUIUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT", "LTCUSDT", "BCHUSDT", "POLUSDT",
    "UNIUSDT", "ICPUSDT", "FETUSDT", "RENDERUSDT", "INJUSDT", "TIAUSDT", "STXUSDT", "FILUSDT"
]

SERVER_PORT = 5055
ENGINE_START_TIME = datetime.now()

# =====================================================================
# NEW: "TAKE TRADE (DEMO)" — one-click execution on Binance FUTURES
# TESTNET (fake money, never real funds). Lowest leverage (1x) and the
# smallest quantity Binance's own rules allow, every time.
#
# ⚠️ SETUP REQUIRED — this does nothing until you fill these in:
#   1. Go to https://testnet.binancefuture.com , log in with GitHub.
#   2. Generate a Testnet API key + secret there (NOT your real Binance keys).
#   3. Paste them below. These only work against the testnet base URL —
#      they cannot touch your real Binance account even by mistake.
# =====================================================================
BINANCE_TESTNET_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "")
BINANCE_TESTNET_API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", "")
BINANCE_TESTNET_BASE = "https://testnet.binancefuture.com"
DEMO_LEVERAGE = 1                 # lowest possible leverage
_symbol_filters_cache = {}        # symbol -> {stepSize, minQty, minNotional, pricePrecision, qtyPrecision}


def _testnet_signed_request(method, path, params=None):
    if not BINANCE_TESTNET_API_KEY or not BINANCE_TESTNET_API_SECRET:
        raise RuntimeError(
            "Binance Testnet API key/secret not configured. Add BINANCE_TESTNET_API_KEY and "
            "BINANCE_TESTNET_API_SECRET near the top of app.py (see setup comment above them)."
        )
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    query = urlencode(params)
    signature = hmac.new(BINANCE_TESTNET_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_TESTNET_BASE}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_TESTNET_API_KEY}
    res = requests.request(method, url, headers=headers, timeout=10)
    data = res.json()
    if res.status_code != 200:
        raise RuntimeError(f"Binance Testnet error {res.status_code}: {data}")
    return data


def get_symbol_filters(symbol):
    """Cached lookup of step size / min quantity / min notional so every order
    is legal on the first try instead of guessing and getting rejected."""
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]
    res = requests.get(f"{BINANCE_TESTNET_BASE}/fapi/v1/exchangeInfo", timeout=10)
    info = res.json()
    for s in info.get("symbols", []):
        if s["symbol"] != symbol:
            continue
        step_size, min_qty, min_notional = 0.001, 0.001, 5.0
        tick_size = 0.01
        qty_precision = s.get("quantityPrecision", 3)
        price_precision = s.get("pricePrecision", 2)
        for f in s.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("notional", f.get("minNotional", 5.0)))
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
        result = {
            "stepSize": step_size, "minQty": min_qty, "minNotional": min_notional,
            "qtyPrecision": qty_precision, "pricePrecision": price_precision,
            "tickSize": tick_size,
        }
        _symbol_filters_cache[symbol] = result
        return result
    raise RuntimeError(f"Symbol {symbol} not found on Binance Futures Testnet.")


def round_step(value, step, precision):
    if step <= 0:
        return round(value, precision)
    steps = math.floor(value / step)
    return round(steps * step, precision)


def round_price_to_tick(price, tick_size, precision):
    """Round to the nearest multiple of tickSize (not just N decimal places) —
    Binance rejects prices with error -4014 if they aren't an exact multiple."""
    if tick_size <= 0:
        return round(price, precision)
    ticks = round(price / tick_size)
    return round(ticks * tick_size, precision)


def compute_smallest_quantity(symbol, price):
    """Smallest quantity that satisfies BOTH the exchange's minQty/stepSize
    AND minNotional (price * qty) — i.e. the smallest legal order size."""
    f = get_symbol_filters(symbol)
    qty_from_notional = f["minNotional"] / price
    raw_qty = max(f["minQty"], qty_from_notional)
    qty = round_step(raw_qty, f["stepSize"], f["qtyPrecision"])
    # rounding down can occasionally land just under the minimum — bump up one step if so
    if qty * price < f["minNotional"] or qty < f["minQty"]:
        qty = round(qty + f["stepSize"], f["qtyPrecision"])
    return qty, f["pricePrecision"]


def get_mark_price(symbol):
    res = requests.get(f"{BINANCE_TESTNET_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    return float(res.json()["price"])


def place_demo_trade(symbol, direction, ai_entry, ai_tp, ai_sl):
    """
    Updated place_demo_trade function for Binance API compatibility.
    Replaces deprecated STOP_MARKET / TAKE_PROFIT_MARKET with LIMIT / STOP orders.
    """
    direction = direction.upper()
    side = "BUY" if direction == "LONG" else "SELL"
    opposite_side = "SELL" if direction == "LONG" else "BUY"

    # 1) Set leverage
    try:
        _testnet_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": DEMO_LEVERAGE})
    except Exception as e:
        print(f"⚠️ [DEMO TRADE] Leverage set failed: {e}")

    # 2) Get current market price & quantity
    market_price = get_mark_price(symbol)
    qty, price_precision = compute_smallest_quantity(symbol, market_price)
    tick_size = get_symbol_filters(symbol)["tickSize"]

    # 3) Calculate TP and SL
    reward_dist = abs(ai_tp - market_price)
    original_risk_dist = abs(market_price - ai_sl)
    capped_risk_dist = min(original_risk_dist, reward_dist) if reward_dist > 0 else original_risk_dist
    
    if direction == "LONG":
        final_sl = round_price_to_tick(market_price - capped_risk_dist, tick_size, price_precision)
    else:
        final_sl = round_price_to_tick(market_price + capped_risk_dist, tick_size, price_precision)
        
    final_tp = round_price_to_tick(float(ai_tp), tick_size, price_precision)

    # 4) Execute MARKET Entry Order
    entry_order = _testnet_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol, 
        "side": side, 
        "type": "MARKET", 
        "quantity": qty
    })

    # 5) Place Take-Profit as a CLOSE-POSITION algo order so it shows
    #    attached to the position's TP/SL field (not just a stray open order).
    tp_order = _testnet_signed_request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": opposite_side,
        "type": "TAKE_PROFIT_MARKET",
        "triggerPrice": final_tp,
        "closePosition": "true",
        "timeInForce": "GTC"
    })

    # 6) Place Stop-Loss via the Algo Order endpoint.
    #    Binance migrated conditional order types (STOP, STOP_MARKET,
    #    TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET) off the
    #    regular /fapi/v1/order endpoint to /fapi/v1/algoOrder on 2025-12-09.
    #    The old endpoint now rejects them with -4120. Plain LIMIT/MARKET
    #    orders (like our TP above) were NOT migrated and still work as-is.
    sl_order = _testnet_signed_request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": opposite_side,
        "type": "STOP",
        "triggerPrice": final_sl,
        "price": final_sl,
        "quantity": qty,
        "reduceOnly": "true",
        "timeInForce": "GTC"
    })

    return {
        "status": "success",
        "symbol": symbol,
        "direction": direction,
        "executed_entry": market_price,
        "quantity": qty,
        "leverage": DEMO_LEVERAGE,
        "tp": final_tp,
        "sl": final_sl,
        "sl_adjusted": final_sl != round_price_to_tick(float(ai_sl), tick_size, price_precision),
        "entry_order_id": entry_order.get("orderId"),
        "tp_order_id": tp_order.get("algoId"),
        "sl_order_id": sl_order.get("algoId"),
    }



# =====================================================================
# BOT STRATEGY CONFIGS  (Bot 1 -> Bot 5)
# =====================================================================
BOT_CONFIGS = [
    {
        "id": "bot1",
        "name": "Trend Rider",
        "tagline": "Higher-timeframe trend ke sath hi trade karta hai",
        "description": (
            "Sirf tab signal deta hai jab 1h aur 4h EMA50/EMA200 dono ek hi direction "
            "confirm karein (main trend). Counter-trend setups skip kar deta hai — "
            "goal: kam signals, zyada reliable direction."
        ),
        "trend_mode": "strict_align",       # signal ko higher-TF trend ke against nahi jaane deta
        "min_score": 78,
        "base_rr": 1.8,                     # base risk:reward multiplier for TP distance vs SL
        "prompt_style": (
            "STRATEGY = TREND FOLLOWING. Only take the trade if it agrees with the higher "
            "timeframe trend given below. Reject counter-trend setups even if the 15m chart "
            "looks tempting."
        ),
    },
    {
        "id": "bot2",
        "name": "Reversal Hunter",
        "tagline": "Overbought/oversold extremes par reversal dhoondta hai",
        "description": (
            "RSI extremes (>70 / <30) aur Bollinger Band touches par counter-trend reversal "
            "trades leta hai. Tighter SL rakhta hai kyunke reversal setups fail-fast hote hain."
        ),
        "trend_mode": "counter_allowed",    # higher-TF trend ke against jaane ki ijazat hai
        "min_score": 72,
        "base_rr": 1.4,
        "prompt_style": (
            "STRATEGY = MEAN REVERSION / REVERSAL. Look specifically for RSI extremes (overbought "
            "above 70, oversold below 30) combined with a Bollinger Band touch or rejection. "
            "Counter-trend entries against the higher timeframe trend are ALLOWED if the "
            "reversal signal is strong, but say so explicitly in the reason."
        ),
    },
    {
        "id": "bot3",
        "name": "Breakout Sniper",
        "tagline": "Volatility expansion / squeeze breakouts",
        "description": (
            "ATR expansion aur Bollinger Band squeeze-breakout patterns par focus karta hai. "
            "Wider TP rakhta hai kyunke breakouts bade moves dete hain."
        ),
        "trend_mode": "soft_align",         # trend ke against ho sakta hai but score kam milta hai
        "min_score": 75,
        "base_rr": 2.2,
        "prompt_style": (
            "STRATEGY = VOLATILITY BREAKOUT. Look for Bollinger Band squeeze followed by "
            "expansion, or a sudden ATR expansion versus recent average, suggesting a breakout "
            "move is starting. Prefer wider take-profit targets since breakouts tend to run."
        ),
    },
    {
        "id": "bot4",
        "name": "Scalper Precision",
        "tagline": "Tight, fast, high-frequency 15m scalps",
        "description": (
            "Chhote, tez moves par focus — tight SL/TP. Higher timeframe trend ko sirf ek "
            "soft filter ki tarah use karta hai, mostly 15m momentum par decide karta hai."
        ),
        "trend_mode": "soft_align",
        "min_score": 70,
        "base_rr": 1.2,
        "prompt_style": (
            "STRATEGY = SHORT-TERM SCALP. Focus mainly on 15m momentum, MACD histogram "
            "direction and short bursts of volume/price action. Keep TP and SL tight and "
            "close to current price — this is a fast in-and-out trade, not a swing."
        ),
    },
    {
        "id": "bot5",
        "name": "Conservative Swing",
        "tagline": "Sirf best-of-best setups, strict multi-timeframe alignment",
        "description": (
            "Sabse zyada selective bot — high score threshold, strict 1h+4h alignment, "
            "wider SL/TP for a proper swing trade. Kam signals, lekin sabse high-conviction."
        ),
        "trend_mode": "strict_align",
        "min_score": 85,
        "base_rr": 2.6,
        "prompt_style": (
            "STRATEGY = CONSERVATIVE SWING. Be very selective — only signal if multiple "
            "confluences line up (trend + momentum + indicator agreement). It is completely "
            "fine to return NONE most of the time. When you do signal, use wider SL/TP suited "
            "for a multi-hour swing, not a scalp."
        ),
    },
]
BOT_BY_ID = {b["id"]: b for b in BOT_CONFIGS}

# =====================================================================
# SHARED STATE  (thread-safe)
# =====================================================================
_state_lock = threading.Lock()
signals_store = {}     # key: f"{bot_id}:{symbol}" -> signal dict (OPEN signals)
closed_store = []      # list of closed signal dicts (TP/SL hit)
bot_stats = {b["id"]: {"wins": 0, "losses": 0, "total": 0} for b in BOT_CONFIGS}

app = Flask(__name__, template_folder='.')
CORS(app)  # Blockage fix karne ke liye full CORS enable kar diya hai


# =====================================================================
# PKT TIME / DYNAMIC RISK SESSION
# =====================================================================
PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time, no DST


def karachi_now_str():
    """Every persisted timestamp in this app goes through this function so
    they're ALWAYS true Karachi wall-clock time, regardless of what
    timezone the underlying machine/container/GitHub Actions runner is
    set to. Previously this used naive datetime.now() (whatever the host's
    local timezone happened to be), which is why card timestamps could
    look 'wrong' depending on where app.py/bot_engine.py actually ran."""
    return datetime.now(PKT).strftime('%Y-%m-%d %H:%M:%S')


def get_session_risk_multiplier():
    """
    5-6 PM PKT ke baad market volatility badalti hai (US session open hone
    lagta hai) -> is waqt ke baad SL/TP dono ko thoda wider rakhte hain
    taake normal noise se pehle stop na lage, aur profit-taking bhi realistic ho.
    """
    now_pkt = datetime.now(PKT)
    hour = now_pkt.hour
    if 17 <= hour < 23:          # 5 PM - 11 PM PKT: high volatility window
        return 1.35, "HIGH_VOLATILITY_EVENING"
    elif 0 <= hour < 6:           # late night: thinner liquidity, be a bit wider too
        return 1.15, "LOW_LIQUIDITY_NIGHT"
    else:
        return 1.0, "NORMAL_SESSION"


# =====================================================================
# INDICATOR MATH — plain Python (unchanged from original bot.py)
# =====================================================================
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2 / (period + 1)
    prev = sma
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [(f - s) if f is not None and s is not None else None for f, s in zip(ema_fast, ema_slow)]
    valid = [v for v in macd_line if v is not None]
    if len(valid) < signal:
        return None, None, None
    signal_series = _ema_series(valid, signal)
    macd_now = valid[-1]
    signal_now = signal_series[-1]
    if signal_now is None:
        return round(macd_now, 5), None, None
    return round(macd_now, 5), round(signal_now, 5), round(macd_now - signal_now, 5)


def calc_bollinger(closes, period=20, num_std=2):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return round(mid + num_std * std, 6), round(mid, 6), round(mid - num_std * std, 6)


def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 6)


def _rolling_bollinger(closes, period=20, num_std=2):
    n = len(closes)
    upper, mid, lower = [math.nan] * n, [math.nan] * n, [math.nan] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(var)
        upper[i] = m + num_std * std
        mid[i] = m
        lower[i] = m - num_std * std
    return upper, mid, lower


def _rolling_ema(values, period):
    n = len(values)
    out = [math.nan] * n
    if n < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2 / (period + 1)
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rolling_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = _rolling_ema(closes, fast)
    ema_slow = _rolling_ema(closes, slow)
    macd_line = [(f - s) if not math.isnan(f) and not math.isnan(s) else math.nan for f, s in zip(ema_fast, ema_slow)]
    first_valid = next((i for i, v in enumerate(macd_line) if not math.isnan(v)), None)
    signal_line = [math.nan] * len(macd_line)
    if first_valid is not None:
        seg_ema = _rolling_ema(macd_line[first_valid:], signal)
        for off, val in enumerate(seg_ema):
            signal_line[first_valid + off] = val
    hist = [(m - s) if not math.isnan(m) and not math.isnan(s) else math.nan for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def _rolling_rsi(closes, period=14):
    n = len(closes)
    out = [math.nan] * n
    if n < period + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


# =====================================================================
# NEW: MULTI-TIMEFRAME TREND FILTER (1h + 4h EMA50 / EMA200)
# =====================================================================
def fetch_klines(symbol, interval, limit=210):
    for base_url in BINANCE_MIRRORS:
        try:
            r = requests.get(
                f"{base_url}/klines?symbol={symbol}&interval={interval}&limit={limit}",
                timeout=BINANCE_TIMEOUT
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


# =====================================================================
# SHARED KLINE CACHE — all 5 bots scan the same coin list, so without
# this they'd each independently re-fetch identical 15m/1h/4h candles
# for every symbol (5x the network calls and CPU work for no reason).
# This cache lets whichever bot asks first do the real fetch; the
# other 4 reuse that result for a short TTL window.
# =====================================================================
_kline_cache = {}
_kline_cache_lock = threading.Lock()
KLINE_CACHE_TTL = {"15m": 45, "1h": 180, "4h": 300}  # seconds


def cached_fetch_klines(symbol, interval, limit=210):
    key = (symbol, interval, limit)
    ttl = KLINE_CACHE_TTL.get(interval, 60)
    now = time.time()

    with _kline_cache_lock:
        entry = _kline_cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]

    data = fetch_klines(symbol, interval, limit)

    with _kline_cache_lock:
        _kline_cache[key] = (now, data)
    return data


def get_higher_tf_trend(symbol):
    """
    Returns dict: {
      "bias": "UP" | "DOWN" | "NEUTRAL",
      "summary": human-readable text for the AI prompt
    }
    Based on EMA50 vs EMA200 alignment on 1h AND 4h closes.
    """
    tf_bias = {}
    for tf in ("1h", "4h"):
        kl = cached_fetch_klines(symbol, tf, limit=210)
        if not kl or len(kl) < 200:
            tf_bias[tf] = "NEUTRAL"
            continue
        closes = [float(k[4]) for k in kl]
        ema50 = _ema_series(closes, 50)
        ema200 = _ema_series(closes, 200)
        e50, e200 = ema50[-1], ema200[-1]
        if e50 is None or e200 is None:
            tf_bias[tf] = "NEUTRAL"
        elif e50 > e200 * 1.001:
            tf_bias[tf] = "UP"
        elif e50 < e200 * 0.999:
            tf_bias[tf] = "DOWN"
        else:
            tf_bias[tf] = "NEUTRAL"

    b1h, b4h = tf_bias.get("1h", "NEUTRAL"), tf_bias.get("4h", "NEUTRAL")
    if b1h == b4h and b1h in ("UP", "DOWN"):
        overall = b1h
    elif "NEUTRAL" in (b1h, b4h) and b1h != "DOWN" and b4h != "DOWN" and (b1h == "UP" or b4h == "UP"):
        overall = "UP"
    elif "NEUTRAL" in (b1h, b4h) and b1h != "UP" and b4h != "UP" and (b1h == "DOWN" or b4h == "DOWN"):
        overall = "DOWN"
    else:
        overall = "NEUTRAL"  # 1h and 4h disagree directly -> no clear higher-TF trend

    summary = (
        f"Higher-Timeframe Trend Filter -> 1h bias: {b1h} | 4h bias: {b4h} | "
        f"Combined trend: {overall} (based on EMA50 vs EMA200)."
    )
    return {"bias": overall, "summary": summary}


def trend_allows_signal(bot_cfg, direction, trend_bias):
    """Decide if this bot's trend_mode permits a given direction against the higher-TF trend."""
    mode = bot_cfg["trend_mode"]
    if trend_bias == "NEUTRAL":
        return True  # no clear higher-TF trend -> don't block anyone
    aligned = (direction == "LONG" and trend_bias == "UP") or (direction == "SHORT" and trend_bias == "DOWN")
    if mode == "strict_align":
        return aligned
    if mode == "soft_align":
        return True  # allowed either way, AI is just told about it in the prompt
    if mode == "counter_allowed":
        return True
    return True


# =====================================================================
# ORDER BOOK CONTEXT (unchanged)
# =====================================================================
def get_orderbook_summary(symbol):
    data = None
    for base_url in BINANCE_MIRRORS:
        try:
            r = requests.get(f"{base_url}/depth?symbol={symbol}&limit=20", timeout=BINANCE_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                break
        except Exception:
            continue
    if data is None:
        return "Order Book: unavailable."
    try:
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid_vol = sum(float(q) for _, q in bids)
        ask_vol = sum(float(q) for _, q in asks)
        total = bid_vol + ask_vol
        buy_pct = round((bid_vol / total) * 100, 1) if total > 0 else 50.0
        summary = f"Order Book (top 20 levels): {buy_pct}% buy-side pressure."
        if bids:
            top_bid = max(bids, key=lambda x: float(x[1]))
            summary += f" Strongest buy wall at {top_bid[0]} (qty {top_bid[1]})."
        if asks:
            top_ask = max(asks, key=lambda x: float(x[1]))
            summary += f" Strongest sell wall at {top_ask[0]} (qty {top_ask[1]})."
        return summary
    except Exception as e:
        return f"Order book unavailable ({e})."


# =====================================================================
# CHART IMAGE (unchanged logic, still 15m visual — used as ONE input,
# not the only input anymore)
# =====================================================================
def generate_chart_image(symbol, klines):
    try:
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        n = len(closes)

        bb_upper, bb_mid, bb_lower = _rolling_bollinger(closes)
        macd_line, signal_line, hist = _rolling_macd(closes)
        rsi_line = _rolling_rsi(closes)

        fig, (ax_price, ax_macd, ax_rsi) = plt.subplots(
            3, 1, figsize=(9, 7), dpi=100, sharex=True,
            gridspec_kw={"height_ratios": [3, 1, 1]}
        )
        fig.patch.set_facecolor("#0d0d1a")

        ax_price.set_facecolor("#0d0d1a")
        for i in range(n):
            color = "#00e676" if closes[i] >= opens[i] else "#ff1744"
            ax_price.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.8)
            body_bottom = min(opens[i], closes[i])
            body_height = abs(closes[i] - opens[i]) or (highs[i] - lows[i]) * 0.01
            ax_price.add_patch(plt.Rectangle((i - 0.3, body_bottom), 0.6, body_height, color=color))
        ax_price.plot(range(n), bb_upper, color="#ffd600", linewidth=1, label="BB Upper")
        ax_price.plot(range(n), bb_mid, color="#8e82b8", linewidth=1, linestyle="--", label="BB Mid")
        ax_price.plot(range(n), bb_lower, color="#ffd600", linewidth=1, label="BB Lower")
        ax_price.set_title(f"{symbol} — 15m (Bollinger overlay)", color="white", fontsize=10)
        ax_price.tick_params(colors="white", labelsize=6)
        ax_price.legend(fontsize=6, facecolor="#1a1530", labelcolor="white", loc="upper left")

        ax_macd.set_facecolor("#0d0d1a")
        ax_macd.plot(range(n), macd_line, color="#00f0ff", linewidth=1, label="MACD")
        ax_macd.plot(range(n), signal_line, color="#ff9100", linewidth=1, label="Signal")
        bar_colors = ["#00e676" if (not math.isnan(h) and h >= 0) else "#ff1744" for h in hist]
        ax_macd.bar(range(n), [0 if math.isnan(h) else h for h in hist], color=bar_colors, width=0.6)
        ax_macd.set_title("MACD (12,26,9)", color="white", fontsize=8)
        ax_macd.tick_params(colors="white", labelsize=6)
        ax_macd.legend(fontsize=6, facecolor="#1a1530", labelcolor="white", loc="upper left")

        ax_rsi.set_facecolor("#0d0d1a")
        ax_rsi.plot(range(n), rsi_line, color="#00f0ff", linewidth=1)
        ax_rsi.axhline(70, color="#ff1744", linewidth=0.6, linestyle="--")
        ax_rsi.axhline(30, color="#00e676", linewidth=0.6, linestyle="--")
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_title("RSI (14)", color="white", fontsize=8)
        ax_rsi.tick_params(colors="white", labelsize=6)

        for ax in (ax_price, ax_macd, ax_rsi):
            for spine in ax.spines.values():
                spine.set_color("#333")

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception as e:
        print(f"⚠️ [CHART ERROR] {symbol}: {e}")
        return None


def build_context_text(symbol, closes, highs, lows, trend_info, session_note):
    rsi = calc_rsi(closes)
    macd, macd_signal, macd_hist = calc_macd(closes)
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes)
    atr = calc_atr(highs, lows, closes)
    ob_summary = get_orderbook_summary(symbol)

    return (
        f"Current Price: {closes[-1]}\n"
        f"RSI(14) [15m]: {rsi}\n"
        f"MACD [15m]: {macd} | Signal: {macd_signal} | Histogram: {macd_hist}\n"
        f"Bollinger Bands(20,2) [15m]: Upper {bb_upper} | Mid {bb_mid} | Lower {bb_lower}\n"
        f"ATR(14) [15m volatility]: {atr}\n"
        f"{ob_summary}\n"
        f"{trend_info['summary']}\n"
        f"Session Note: {session_note}"
    )


def build_prompt(symbol, context_text, bot_cfg):
    return (
        f"You are Bot \"{bot_cfg['name']}\" analyzing {symbol}. {bot_cfg['prompt_style']}\n\n"
        f"A 15-minute candlestick chart image is attached (newest candle on the right, Bollinger "
        f"Bands overlaid on price, MACD and RSI panels below it). Do NOT rely on the image alone — "
        f"treat it as one input among several.\n\n"
        f"Calculated indicator values and multi-timeframe context (use these exact numbers, don't "
        f"re-estimate them from the image):\n{context_text}\n\n"
        "Using the chart image, the indicator values, the higher-timeframe trend filter, and the "
        "order book context together, decide if there is a high-quality LONG or SHORT setup for "
        "THIS bot's strategy, or NONE if nothing compelling. Pick TP/SL levels that are realistic "
        "given current volatility (ATR) — not so tight that normal noise would hit them, not so "
        "wide that price is unlikely to reach TP.\n\n"
        "Return ONLY JSON, no markdown, no extra commentary, in this exact format:\n"
        '{"signal": "LONG", "score": 80, "entry": 100.0, "tp": 105.0, "sl": 95.0, '
        '"reason": "short 1-2 sentence explanation referencing the chart/indicators/trend/order book"}\n'
        "signal MUST be LONG, SHORT, or NONE."
    )


def extract_json_block(text):
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


# =====================================================================
# AI ENGINES — Gemini (vision) -> Groq (vision) -> Mistral (vision)
# =====================================================================
def analyze_with_gemini(symbol, image_b64, context_text, bot_cfg):
    if not GEMINI_KEYS:
        return None
    prompt = build_prompt(symbol, context_text, bot_cfg)
    for attempt in range(len(GEMINI_KEYS)):
        key = _next_key("gemini", GEMINI_KEYS)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={key}"
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": image_b64}}
                ]
            }],
            "generationConfig": {"response_mime_type": "application/json"}
        }
        try:
            res = requests.post(url, json=payload, timeout=20)
            if res.status_code == 200:
                raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = extract_json_block(raw_text)
                if data:
                    data["provider"] = "Gemini"
                    return data
            else:
                print(f"⚠️ [{bot_cfg['id'].upper()}][GEMINI KEY {attempt + 1} LIMIT/ERR] {res.status_code}")
        except Exception as e:
            print(f"⚠️ [{bot_cfg['id'].upper()}][GEMINI KEY {attempt + 1} FAIL] {symbol}: {e}")
    return None


def analyze_with_groq(symbol, image_b64, context_text, bot_cfg):
    if not GROQ_KEYS:
        return None
    prompt = build_prompt(symbol, context_text, bot_cfg)
    for attempt in range(len(GROQ_KEYS)):
        key = _next_key("groq", GROQ_KEYS)
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "qwen/qwen3.6-27b",
                    "reasoning_effort": "none",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                        ]
                    }],
                    "response_format": {"type": "json_object"}
                },
                timeout=20
            )
            if res.status_code == 200:
                raw_text = res.json()["choices"][0]["message"]["content"]
                data = extract_json_block(raw_text)
                if data:
                    data["provider"] = "Groq"
                    return data
            else:
                print(f"⚠️ [{bot_cfg['id'].upper()}][GROQ KEY {attempt + 1} ERROR] {res.status_code}")
        except Exception as e:
            print(f"⚠️ [{bot_cfg['id'].upper()}][GROQ KEY {attempt + 1} FAIL] {symbol}: {e}")
    return None


def analyze_with_mistral(symbol, image_b64, context_text, bot_cfg):
    if not MISTRAL_KEYS:
        return None
    prompt = build_prompt(symbol, context_text, bot_cfg)
    for attempt in range(len(MISTRAL_KEYS)):
        key = _next_key("mistral", MISTRAL_KEYS)
        try:
            res = requests.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "pixtral-12b-2409",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": f"data:image/png;base64,{image_b64}"}
                        ]
                    }],
                    "response_format": {"type": "json_object"}
                },
                timeout=20
            )
            if res.status_code == 200:
                raw_text = res.json()["choices"][0]["message"]["content"]
                data = extract_json_block(raw_text)
                if data:
                    data["provider"] = "Mistral"
                    return data
            else:
                print(f"⚠️ [{bot_cfg['id'].upper()}][MISTRAL KEY {attempt + 1} ERROR] {res.status_code}")
        except Exception as e:
            print(f"⚠️ [{bot_cfg['id'].upper()}][MISTRAL KEY {attempt + 1} FAIL] {symbol}: {e}")
    return None


# =====================================================================
# DISCORD WEBHOOK — posts a message whenever a new signal is submitted.
# No bot token/persistent connection needed, just a simple HTTP POST.
# =====================================================================
def post_discord_signal(bot_cfg, symbol, direction, entry, tp, sl, score, provider, reason):
    if not DISCORD_WEBHOOK_URL:
        return
    color = 0x00e676 if direction == "LONG" else 0xff1744
    embed = {
        "title": f"🚨 {bot_cfg['name']} — {direction} {symbol}",
        "color": color,
        "fields": [
            {"name": "Entry", "value": str(entry), "inline": True},
            {"name": "Take Profit", "value": str(tp), "inline": True},
            {"name": "Stop Loss", "value": str(sl), "inline": True},
            {"name": "Score", "value": str(score), "inline": True},
            {"name": "Source", "value": provider, "inline": True},
            {"name": "Reason", "value": (reason or "-")[:1000], "inline": False},
        ],
        "footer": {"text": f"Bot: {bot_cfg['id']}"},
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=8)
    except Exception as e:
        print(f"⚠️ [DISCORD WEBHOOK FAILED] {e}")


# =====================================================================
# TECHNICAL (NON-AI) STRATEGY EVALUATORS — used when AI_ENABLED=false.
# Each function mirrors that bot's original "prompt_style" persona but
# as plain indicator rules instead of an AI call. Returns a dict shaped
# like the AI output ({signal, score, entry, tp, sl, reason, provider})
# or None if no setup. TP/SL distances are ATR-based, scaled by the
# bot's base_rr — the same session risk-multiplier widening in
# run_bot_engine() applies afterwards either way.
# =====================================================================
def _tech_common(closes, highs, lows):
    rsi = calc_rsi(closes)
    macd, macd_signal, macd_hist = calc_macd(closes)
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes)
    atr = calc_atr(highs, lows, closes)
    ema9 = _ema_series(closes, 9)[-1]
    ema21 = _ema_series(closes, 21)[-1]
    return rsi, macd, macd_signal, macd_hist, bb_upper, bb_mid, bb_lower, atr, ema9, ema21


def _tp_sl_from_atr(entry, atr, base_rr, direction):
    if not atr or atr <= 0:
        atr = entry * 0.004  # fallback ~0.4% if ATR unavailable
    risk_dist = atr * 1.5
    reward_dist = risk_dist * base_rr
    if direction == "LONG":
        return round(entry + reward_dist, 6), round(entry - risk_dist, 6)
    else:
        return round(entry - reward_dist, 6), round(entry + risk_dist, 6)


def eval_tech_bot1_trend_rider(symbol, closes, highs, lows, trend_info, bot_cfg):
    """Trend Rider: only trades WITH the higher-TF trend, EMA9/21 confirms."""
    rsi, macd, macd_sig, macd_hist, bb_u, bb_m, bb_l, atr, ema9, ema21 = _tech_common(closes, highs, lows)
    if None in (rsi, macd, macd_sig, ema9, ema21):
        return None
    entry = closes[-1]
    bias = trend_info["bias"]
    if bias == "UP" and ema9 > ema21 and macd > macd_sig and rsi < 75:
        direction = "LONG"
    elif bias == "DOWN" and ema9 < ema21 and macd < macd_sig and rsi > 25:
        direction = "SHORT"
    else:
        return None
    tp, sl = _tp_sl_from_atr(entry, atr, bot_cfg["base_rr"], direction)
    score = 80 if (rsi < 65 or rsi > 35) else 75
    return {"signal": direction, "score": score, "entry": entry, "tp": tp, "sl": sl,
            "reason": f"Trend {bias} confirmed by EMA9/21 + MACD alignment (RSI {rsi}).", "provider": "Technical"}


def eval_tech_bot2_reversal_hunter(symbol, closes, highs, lows, trend_info, bot_cfg):
    """Reversal Hunter: RSI extreme + Bollinger Band touch, counter-trend OK."""
    rsi, macd, macd_sig, macd_hist, bb_u, bb_m, bb_l, atr, ema9, ema21 = _tech_common(closes, highs, lows)
    if None in (rsi, bb_u, bb_l):
        return None
    entry = closes[-1]
    if rsi <= 30 and entry <= bb_l:
        direction = "LONG"
    elif rsi >= 70 and entry >= bb_u:
        direction = "SHORT"
    else:
        return None
    tp, sl = _tp_sl_from_atr(entry, atr, bot_cfg["base_rr"], direction)
    score = 85 if (rsi <= 25 or rsi >= 75) else 74
    return {"signal": direction, "score": score, "entry": entry, "tp": tp, "sl": sl,
            "reason": f"RSI {rsi} extreme + price at Bollinger Band edge.", "provider": "Technical"}


def eval_tech_bot3_breakout_sniper(symbol, closes, highs, lows, trend_info, bot_cfg):
    """Breakout Sniper: price breaks recent range with ATR/volatility expansion."""
    rsi, macd, macd_sig, macd_hist, bb_u, bb_m, bb_l, atr, ema9, ema21 = _tech_common(closes, highs, lows)
    if atr is None or len(closes) < 25:
        return None
    entry = closes[-1]
    lookback = 20
    recent_high = max(highs[-lookback - 1:-1])
    recent_low = min(lows[-lookback - 1:-1])
    prev_atr = calc_atr(highs[:-1], lows[:-1], closes[:-1]) or atr
    atr_expanding = atr > prev_atr * 1.15
    if entry > recent_high and atr_expanding:
        direction = "LONG"
    elif entry < recent_low and atr_expanding:
        direction = "SHORT"
    else:
        return None
    tp, sl = _tp_sl_from_atr(entry, atr, bot_cfg["base_rr"], direction)
    return {"signal": direction, "score": 78, "entry": entry, "tp": tp, "sl": sl,
            "reason": "Price broke recent range high/low with ATR volatility expansion.", "provider": "Technical"}


def eval_tech_bot4_scalper(symbol, closes, highs, lows, trend_info, bot_cfg):
    """Scalper Precision: fast MACD histogram crossover, tight TP/SL."""
    rsi, macd, macd_sig, macd_hist, bb_u, bb_m, bb_l, atr, ema9, ema21 = _tech_common(closes, highs, lows)
    if None in (macd, macd_sig) or len(closes) < 30:
        return None
    prev_macd, prev_sig, _ = calc_macd(closes[:-1])
    if prev_macd is None or prev_sig is None:
        return None
    entry = closes[-1]
    if prev_macd <= prev_sig and macd > macd_sig:
        direction = "LONG"
    elif prev_macd >= prev_sig and macd < macd_sig:
        direction = "SHORT"
    else:
        return None
    tp, sl = _tp_sl_from_atr(entry, atr, bot_cfg["base_rr"], direction)
    return {"signal": direction, "score": 72, "entry": entry, "tp": tp, "sl": sl,
            "reason": "MACD line just crossed its signal line (fast momentum flip).", "provider": "Technical"}


def eval_tech_bot5_conservative_swing(symbol, closes, highs, lows, trend_info, bot_cfg):
    """Conservative Swing: needs trend + MACD + RSI all agreeing, high bar."""
    rsi, macd, macd_sig, macd_hist, bb_u, bb_m, bb_l, atr, ema9, ema21 = _tech_common(closes, highs, lows)
    if None in (rsi, macd, macd_sig, ema9, ema21):
        return None
    entry = closes[-1]
    bias = trend_info["bias"]
    confluences_long = [bias == "UP", ema9 > ema21, macd > macd_sig, 40 < rsi < 65]
    confluences_short = [bias == "DOWN", ema9 < ema21, macd < macd_sig, 35 < rsi < 60]
    if all(confluences_long):
        direction = "LONG"
    elif all(confluences_short):
        direction = "SHORT"
    else:
        return None
    tp, sl = _tp_sl_from_atr(entry, atr, bot_cfg["base_rr"], direction)
    return {"signal": direction, "score": 87, "entry": entry, "tp": tp, "sl": sl,
            "reason": "Trend + EMA + MACD + RSI all confluent — high-conviction swing setup.", "provider": "Technical"}


TECHNICAL_EVALUATORS = {
    "bot1": eval_tech_bot1_trend_rider,
    "bot2": eval_tech_bot2_reversal_hunter,
    "bot3": eval_tech_bot3_breakout_sniper,
    "bot4": eval_tech_bot4_scalper,
    "bot5": eval_tech_bot5_conservative_swing,
}


# =====================================================================
# SIGNAL SUBMISSION (in-process now, no HTTP hop needed between
# bot threads and the server since they share the same process)
# =====================================================================
def submit_signal(bot_cfg, symbol, direction, entry, tp, sl, score, provider, reason):
    key = f"{bot_cfg['id']}:{symbol}"
    timestamp = karachi_now_str()
    copy_payload = f"Coin: {symbol}\nSide: {direction.upper()}\nTime: {timestamp}\nEntry: {entry}\nTP: {tp}\nSL: {sl}"
    with _state_lock:
        signals_store[key] = {
            "key": key,
            "bot_id": bot_cfg["id"],
            "bot_name": bot_cfg["name"],
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "score": score,
            "provider": provider,
            "reason": reason,
            "status": "waiting_entry",
            "timestamp": timestamp,
            "copy_payload": copy_payload,
        }
    post_discord_signal(bot_cfg, symbol, direction, entry, tp, sl, score, provider, reason)


# =====================================================================
# PER-BOT ENGINE LOOP  (one thread per bot config)
# =====================================================================
def run_bot_engine(bot_cfg):
    print(f"🚀 [{bot_cfg['id'].upper()}] {bot_cfg['name']} engine started — {bot_cfg['tagline']}")
    coin_offset = BOT_CONFIGS.index(bot_cfg)  # stagger start point per bot so they don't hammer API in lockstep

    while True:
        coins = TOP_50_COINS[coin_offset:] + TOP_50_COINS[:coin_offset]
        for symbol in coins:
            time_str = datetime.now(PKT).strftime('%H:%M:%S')
            print(f"[{time_str}] 🔍 [{bot_cfg['id'].upper()}] Scanning {symbol}...")

            try:
                kl_15m = cached_fetch_klines(symbol, "15m", limit=100)
                if not kl_15m:
                    print(f"❌ [{bot_cfg['id'].upper()}][BINANCE ERROR] {symbol}")
                    continue

                closes = [float(k[4]) for k in kl_15m]
                highs = [float(k[2]) for k in kl_15m]
                lows = [float(k[3]) for k in kl_15m]

                # ---- multi-timeframe trend filter ----
                trend_info = get_higher_tf_trend(symbol)

                # ---- dynamic PKT session risk ----
                risk_mult, session_note = get_session_risk_multiplier()

                if AI_ENABLED:
                    context_text = build_context_text(symbol, closes, highs, lows, trend_info, session_note)
                    image_b64 = generate_chart_image(symbol, kl_15m)
                    if not image_b64:
                        print(f"⚠️ [{bot_cfg['id'].upper()}][{symbol}] Chart generation failed, skipping.")
                        continue

                    ai_out = analyze_with_gemini(symbol, image_b64, context_text, bot_cfg)
                    if not ai_out:
                        ai_out = analyze_with_groq(symbol, image_b64, context_text, bot_cfg)
                    if not ai_out:
                        ai_out = analyze_with_mistral(symbol, image_b64, context_text, bot_cfg)

                    if not ai_out:
                        print(f"⚠️ [{bot_cfg['id'].upper()}][{symbol}] All AI engines failed to respond.")
                        time.sleep(2)
                        continue
                else:
                    # AI disabled — pure technical rules, no chart image / AI call needed
                    evaluator = TECHNICAL_EVALUATORS.get(bot_cfg["id"])
                    ai_out = evaluator(symbol, closes, highs, lows, trend_info, bot_cfg) if evaluator else None
                    if not ai_out:
                        time.sleep(1)
                        continue

                sig = ai_out.get('signal', 'NONE')
                provider = ai_out.get('provider', 'AI')
                score = ai_out.get('score', 80)

                if sig not in ('LONG', 'SHORT'):
                    print(f"ℹ️ [{bot_cfg['id'].upper()}][{symbol}] NO TRADE (Provider: {provider})")
                    time.sleep(2)
                    continue

                # bot-specific score threshold
                if score < bot_cfg["min_score"]:
                    print(f"ℹ️ [{bot_cfg['id'].upper()}][{symbol}] Score {score} below threshold "
                          f"{bot_cfg['min_score']} — skipped.")
                    time.sleep(2)
                    continue

                # trend filter gate
                if not trend_allows_signal(bot_cfg, sig, trend_info["bias"]):
                    print(f"🚫 [{bot_cfg['id'].upper()}][{symbol}] {sig} rejected — against higher-TF "
                          f"trend ({trend_info['bias']}) and this bot requires strict alignment.")
                    time.sleep(2)
                    continue

                entry = float(ai_out.get('entry', 0))
                tp = float(ai_out.get('tp', 0))
                sl = float(ai_out.get('sl', 0))

                # apply dynamic PKT risk multiplier to widen SL/TP distance in high-volatility hours
                if risk_mult != 1.0 and entry:
                    tp_dist = abs(tp - entry) * risk_mult
                    sl_dist = abs(entry - sl) * risk_mult
                    if sig == "LONG":
                        tp = round(entry + tp_dist, 6)
                        sl = round(entry - sl_dist, 6)
                    else:
                        tp = round(entry - tp_dist, 6)
                        sl = round(entry + sl_dist, 6)

                reason = ai_out.get('reason', '')
                submit_signal(bot_cfg, symbol, sig, entry, tp, sl, score, provider, reason)

                print("--------------------------------------------------")
                print(f"✅ [{bot_cfg['id'].upper()}] {bot_cfg['name']} SIGNAL | Provider: {provider.upper()}")
                print(f"📌 {symbol} | {sig} | Entry {entry} | TP {tp} | SL {sl} | Score {score}")
                print(f"🕒 Session: {session_note} (risk x{risk_mult}) | Trend: {trend_info['bias']}")
                print(f"💬 {reason}")
                print("--------------------------------------------------\n")

            except Exception as e:
                print(f"❌ [{bot_cfg['id'].upper()}][CRITICAL ERROR] {symbol}: {e}")

            time.sleep(2)

        print(f"\n🔄 [{bot_cfg['id'].upper()}] Completed full scan loop. Restarting in 5 seconds...\n")
        time.sleep(5)


# =====================================================================
# NEW: POSITION MONITOR THREAD — tracks open signals against live
# price, closes them on TP/SL hit, updates per-bot win/loss stats.
# (Needed for the comparison table + "winner bot" feature coming in
#  a later part.)
# =====================================================================
def get_live_price(symbol):
    for base_url in BINANCE_MIRRORS:
        try:
            r = requests.get(f"{base_url}/ticker/price?symbol={symbol}", timeout=BINANCE_TIMEOUT)
            if r.status_code == 200:
                return float(r.json()["price"])
        except Exception:
            continue
    return None


def run_position_monitor():
    print("👁️  Position monitor thread started.")
    while True:
        try:
            with _state_lock:
                open_keys = list(signals_store.keys())

            for key in open_keys:
                with _state_lock:
                    sig = signals_store.get(key)
                if not sig:
                    continue

                price = get_live_price(sig["symbol"])
                if price is None:
                    continue

                hit = None
                if sig["direction"] == "LONG":
                    if price >= sig["tp"]:
                        hit = "TP_HIT"
                    elif price <= sig["sl"]:
                        hit = "SL_HIT"
                else:  # SHORT
                    if price <= sig["tp"]:
                        hit = "TP_HIT"
                    elif price >= sig["sl"]:
                        hit = "SL_HIT"

                if hit:
                    with _state_lock:
                        closed = signals_store.pop(key, None)
                        if closed:
                            closed["result"] = hit
                            closed["close_price"] = price
                            closed["closed_at"] = karachi_now_str()
                            closed_store.append(closed)
                            stats = bot_stats.setdefault(closed["bot_id"], {"wins": 0, "losses": 0, "total": 0})
                            stats["total"] += 1
                            if hit == "TP_HIT":
                                stats["wins"] += 1
                            else:
                                stats["losses"] += 1
                    print(f"🏁 [{sig['bot_id'].upper()}] {sig['symbol']} closed -> {hit} @ {price}")

        except Exception as e:
            print(f"❌ [MONITOR ERROR] {e}")

        time.sleep(15)


# =====================================================================
# FLASK ROUTES
# =====================================================================
@app.route('/')
def home():
    import os
    if os.path.exists('dashboard.html'):
        return render_template('dashboard.html')
    return "dashboard.html file missing! It must sit directly next to app.py (repo root), not inside a 'templates' folder.", 404


@app.route('/signal', methods=['POST'])
def receive_signal_external():
    """Kept for backward-compat / external submissions. In-process bots
    use submit_signal() directly (faster, no HTTP round-trip)."""
    try:
        data = request.json
        if not data or 'symbol' not in data:
            return jsonify({"status": "error", "message": "Invalid Payload"}), 400
        bot_id = data.get('bot_id', 'external')
        bot_name = data.get('bot_name', 'External')
        symbol = data['symbol']
        key = f"{bot_id}:{symbol}"
        direction = data.get('direction', 'LONG')
        entry = data.get('entry', 0)
        tp = data.get('tp', 0)
        sl = data.get('sl', 0)
        timestamp = data.get('timestamp', '')
        copy_payload = f"Coin: {symbol}\nSide: {str(direction).upper()}\nTime: {timestamp}\nEntry: {entry}\nTP: {tp}\nSL: {sl}"
        with _state_lock:
            signals_store[key] = {
                "key": key,
                "bot_id": bot_id,
                "bot_name": bot_name,
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "tp": tp,
                "sl": sl,
                "score": data.get('score', 80),
                "provider": data.get('provider', 'AI'),
                "reason": data.get('reason', ''),
                "status": "waiting_entry",
                "timestamp": timestamp,
                "copy_payload": copy_payload,
            }
        return jsonify({"status": "success", "message": "Signal Received"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/tabs', methods=['GET'])
def get_tabs():
    """Flat dict keyed by symbol (backward-compat with old dashboard.js) —
    Part 2 will switch the dashboard to use /bots + /signals_by_bot."""
    with _state_lock:
        flat = {}
        for sig in signals_store.values():
            flat[sig["symbol"]] = sig
        return jsonify(flat)


@app.route('/signals_by_bot', methods=['GET'])
def get_signals_by_bot():
    with _state_lock:
        grouped = {b["id"]: [] for b in BOT_CONFIGS}
        for sig in signals_store.values():
            grouped.setdefault(sig["bot_id"], []).append(sig)
        return jsonify(grouped)


@app.route('/bots', methods=['GET'])
def get_bots():
    return jsonify(BOT_CONFIGS)


@app.route('/stats', methods=['GET'])
def get_stats():
    with _state_lock:
        result = []
        for b in BOT_CONFIGS:
            s = bot_stats.get(b["id"], {"wins": 0, "losses": 0, "total": 0})
            win_rate = round((s["wins"] / s["total"]) * 100, 1) if s["total"] > 0 else 0.0
            result.append({
                "bot_id": b["id"],
                "bot_name": b["name"],
                "wins": s["wins"],
                "losses": s["losses"],
                "total": s["total"],
                "win_rate": win_rate,
            })
        winner = max(result, key=lambda r: (r["win_rate"], r["total"])) if result else None
        return jsonify({"bots": result, "winner": winner})


@app.route('/execute_trade', methods=['POST'])
def execute_trade():
    """One-click 'Take Trade (Demo)' — places a real (testnet, fake-money) order.
    Expects JSON: {symbol, direction, tp, sl}. Entry is always current market price."""
    try:
        data = request.json or {}
        symbol = data.get('symbol')
        direction = data.get('direction')
        tp = data.get('tp')
        sl = data.get('sl')
        if not symbol or not direction or tp is None or sl is None:
            return jsonify({"status": "error", "message": "symbol, direction, tp, sl are all required"}), 400

        result = place_demo_trade(symbol, direction, data.get('entry'), float(tp), float(sl))
        print(f"⚡ [DEMO TRADE EXECUTED] {symbol} {direction} @ {result['executed_entry']} "
              f"qty={result['quantity']} TP={result['tp']} SL={result['sl']}")
        return jsonify(result), 200
    except Exception as e:
        print(f"❌ [DEMO TRADE FAILED] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def get_status():
    uptime_seconds = (datetime.now() - ENGINE_START_TIME).total_seconds()
    return jsonify({
        "started_at": ENGINE_START_TIME.strftime('%Y-%m-%d %H:%M:%S'),
        "uptime_seconds": int(uptime_seconds),
        "open_signals": len(signals_store),
        "closed_trades": len(closed_store),
    })


@app.route('/closed', methods=['GET'])
def get_closed():
    with _state_lock:
        return jsonify(list(reversed(closed_store[-200:])))


# =====================================================================
# ENTRYPOINT — starts Flask + 5 bot threads + 1 monitor thread
# =====================================================================
if __name__ == '__main__':
    port = int(os.getenv("PORT", SERVER_PORT))
    print("==================================================")
    print("🚀 AI SIGNALS BOT — MERGED SERVER + 5-BOT ENGINE")
    print(f"🔗 Dashboard: http://127.0.0.1:{port}/")
    print(f"🧠 AI Mode: {'ON (Gemini -> Groq -> Mistral)' if AI_ENABLED else 'OFF (technical rules only)'}")
    if AI_ENABLED:
        print(f"🔑 Loaded: {len(GEMINI_KEYS)} Gemini | {len(GROQ_KEYS)} Groq | {len(MISTRAL_KEYS)} Mistral Keys")
    print(f"📣 Discord webhook: {'configured' if DISCORD_WEBHOOK_URL else 'not set'}")
    print(f"🤖 Bots: {', '.join(b['name'] for b in BOT_CONFIGS)}")
    print("==================================================\n")

    for cfg in BOT_CONFIGS:
        t = threading.Thread(target=run_bot_engine, args=(cfg,), daemon=True)
        t.start()
        time.sleep(1.5)  # stagger startup so they don't all hit Binance at once

    monitor_t = threading.Thread(target=run_position_monitor, daemon=True)
    monitor_t.start()

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
