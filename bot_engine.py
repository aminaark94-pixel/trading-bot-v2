"""
=====================================================================
 BOT ENGINE (HEADLESS) - 24/7 AUTOPILOT MODE FOR GITHUB ACTIONS
=====================================================================
Yeh file app.py ke merged 5-bot engine ko HEADLESS mode mein chalati hai:

  - GitHub Actions har 15 minute par `python bot_engine.py` run karta hai
  - Har run = 1 scan cycle: saare symbols x 5 strategy bots (parallel threads)
  - Results signals/*.json files mein persist hote hain (repo mein commit)
  - index.html (cloud dashboard) in files ko GitHub se directly parhta hai
  - Aapke Windows PC ki koi zaroorat nahi - poora system cloud par hai

DATA MODES:
  AI_ENABLED=false (default) -> pure technical-indicator strategies
                                (free, fast, zero AI calls)
  AI_ENABLED=true            -> Gemini/Groq/Mistral vision AI decide karta hai
                                (multi-key rotation built-in, keys env se aati hain)

KEY SAFETY:
  API keys kabhi code mein nahi hoti. Sirf environment variables se
  aati hain - GitHub par "Secrets and variables -> Actions" mein
  encrypted secrets ke tor par set karo (logs mein masked rehti hain).

CONTROLS:
  config/bot_enabled.txt   -> "1" = scanning ON, "0" = paused (monitoring chalti rahegi)
  config/settings.json     -> symbols list, e.g. ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
  SIGNAL_MAX_AGE_HOURS     -> is age se purani open signals auto-close (default 72)
  MAX_OPEN_PER_BOT         -> per-bot open signal cap (default 3)
  SCAN_BUDGET_SECONDS      -> max scan time before force-persist (default 900)

Run:  python bot_engine.py
=====================================================================
"""

import os
import json
import time
import threading
from datetime import datetime, timezone

import app as engine  # merged server + 5-bot engine (strategies, indicators, AI, Discord)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "settings.json")
BOT_ENABLED_PATH = os.path.join(BASE_DIR, "config", "bot_enabled.txt")

OPEN_PATH = os.path.join(BASE_DIR, "signals", "open.json")
CLOSED_PATH = os.path.join(BASE_DIR, "signals", "closed.json")
STATS_PATH = os.path.join(BASE_DIR, "signals", "stats.json")
GROUPED_PATH = os.path.join(BASE_DIR, "signals", "signals_by_bot.json")
BOTS_META_PATH = os.path.join(BASE_DIR, "signals", "bots.json")
STATUS_PATH = os.path.join(BASE_DIR, "signals", "status.json")

SIGNAL_MAX_AGE_HOURS = float(os.getenv("SIGNAL_MAX_AGE_HOURS", "72") or 72)
MAX_OPEN_PER_BOT = int(float(os.getenv("MAX_OPEN_PER_BOT", "3") or 3))
SCAN_BUDGET_SECONDS = int(float(os.getenv("SCAN_BUDGET_SECONDS", "900") or 900))
PAUSE_BETWEEN_SCANS = float(os.getenv("PAUSE_BETWEEN_SCANS", "2") or 2)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not parse {path}: {e}")
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def is_bot_active():
    """config/bot_enabled.txt: '1' = ON. File missing = ON. Kuch bhi aur = paused."""
    if not os.path.exists(BOT_ENABLED_PATH):
        return True
    try:
        with open(BOT_ENABLED_PATH, "r", encoding="utf-8") as f:
            return f.read().strip() == "1"
    except Exception as e:
        print(f"[WARN] Could not read master switch: {e}. Defaulting to active.")
        return True


def get_symbols():
    """config/settings.json ki symbols list use karo ('BTC/USDT' -> 'BTCUSDT').
    Khali/missing ho to app.py ke TOP_50_COINS fallback."""
    settings = load_json(CONFIG_PATH, {}) or {}
    out = []
    for s in settings.get("symbols") or []:
        sym = str(s).replace("/", "").replace(":", "").upper().strip()
        if len(sym) >= 5 and sym.endswith("USDT"):
            out.append(sym)
    if not out:
        out = list(engine.TOP_50_COINS)
    return out


def seed_state():
    """Pichli run ki state (signals/*.json) ko engine memory mein wapas load karo."""
    open_list = load_json(OPEN_PATH, []) or []
    kept = dropped = 0
    for sig in open_list:
        if isinstance(sig, dict) and sig.get("key") and sig.get("direction"):
            engine.signals_store[sig["key"]] = sig
            kept += 1
        else:
            dropped += 1
    if dropped:
        print(f"[SEED] {dropped} legacy/invalid open signal(s) dropped (purana schema)")

    closed_list = load_json(CLOSED_PATH, []) or []
    engine.closed_store = [c for c in closed_list if isinstance(c, dict) and c.get("key")]

    stats = load_json(STATS_PATH, {}) or {}
    rows = stats.get("bots")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            bid = row.get("bot_id")
            if bid in engine.bot_stats:
                engine.bot_stats[bid] = {
                    "wins": int(row.get("wins") or 0),
                    "losses": int(row.get("losses") or 0),
                    "total": int(row.get("total") or 0),
                }
    print(f"[SEED] State restored: {kept} open, {len(engine.closed_store)} closed")


def open_count(bot_id):
    with engine._state_lock:
        return sum(1 for s in engine.signals_store.values() if s.get("bot_id") == bot_id)


def scan_symbol(bot_cfg, symbol):
    """app.py ke run_bot_engine loop-body ka one-shot version (same gates, same logic)."""
    bid = bot_cfg["id"].upper()
    try:
        kl_15m = engine.cached_fetch_klines(symbol, "15m", limit=100)
        if not kl_15m:
            print(f"[BINANCE ERROR] [{bid}] {symbol}")
            return

        closes = [float(k[4]) for k in kl_15m]
        highs = [float(k[2]) for k in kl_15m]
        lows = [float(k[3]) for k in kl_15m]

        trend_info = engine.get_higher_tf_trend(symbol)
        risk_mult, session_note = engine.get_session_risk_multiplier()

        if engine.AI_ENABLED:
            context_text = engine.build_context_text(symbol, closes, highs, lows, trend_info, session_note)
            image_b64 = engine.generate_chart_image(symbol, kl_15m)
            if not image_b64:
                print(f"[{bid}][{symbol}] chart generation failed - skipped")
                return
            ai_out = engine.analyze_with_gemini(symbol, image_b64, context_text, bot_cfg)
            if not ai_out:
                ai_out = engine.analyze_with_groq(symbol, image_b64, context_text, bot_cfg)
            if not ai_out:
                ai_out = engine.analyze_with_mistral(symbol, image_b64, context_text, bot_cfg)
            if not ai_out:
                print(f"[{bid}][{symbol}] all AI engines failed")
                return
        else:
            evaluator = engine.TECHNICAL_EVALUATORS.get(bot_cfg["id"])
            ai_out = evaluator(symbol, closes, highs, lows, trend_info, bot_cfg) if evaluator else None
            if not ai_out:
                return  # no setup - normal

        sig = ai_out.get("signal", "NONE")
        provider = ai_out.get("provider", "AI")
        score = ai_out.get("score", 80)

        if sig not in ("LONG", "SHORT"):
            return
        if score < bot_cfg["min_score"]:
            print(f"[{bid}][{symbol}] score {score} < {bot_cfg['min_score']} - skipped")
            return
        if not engine.trend_allows_signal(bot_cfg, sig, trend_info["bias"]):
            print(f"[{bid}][{symbol}] {sig} rejected - against {trend_info['bias']} trend (strict bot)")
            return

        entry = float(ai_out.get("entry") or 0)
        tp = float(ai_out.get("tp") or 0)
        sl = float(ai_out.get("sl") or 0)
        if entry <= 0 or tp <= 0 or sl <= 0:
            print(f"[{bid}][{symbol}] invalid entry/tp/sl - skipped")
            return

        if risk_mult != 1.0:
            tp_dist = abs(tp - entry) * risk_mult
            sl_dist = abs(entry - sl) * risk_mult
            if sig == "LONG":
                tp = round(entry + tp_dist, 6)
                sl = round(entry - sl_dist, 6)
            else:
                tp = round(entry - tp_dist, 6)
                sl = round(entry + sl_dist, 6)

        engine.submit_signal(bot_cfg, symbol, sig, entry, tp, sl, score, provider, ai_out.get("reason", ""))
        print(f"SIGNAL [{bid}] {bot_cfg['name']} | {symbol} {sig} | Entry {entry} | TP {tp} | SL {sl} | {provider} score {score} | {session_note}")
    except Exception as e:
        print(f"[CRITICAL] [{bid}] {symbol}: {e}")


def scan_bot(bot_cfg, symbols):
    print(f"[{bot_cfg['id'].upper()}] {bot_cfg['name']} - scan started ({bot_cfg['tagline']})")
    for symbol in symbols:
        if open_count(bot_cfg["id"]) >= MAX_OPEN_PER_BOT:
            print(f"[{bot_cfg['id'].upper()}] cap {MAX_OPEN_PER_BOT} open signals reached - scan stopped")
            return
        key = f"{bot_cfg['id']}:{symbol}"
        with engine._state_lock:
            already = key in engine.signals_store
        if already:
            continue
        scan_symbol(bot_cfg, symbol)
        time.sleep(PAUSE_BETWEEN_SCANS)
    print(f"[{bot_cfg['id'].upper()}] scan cycle done")


def _parse_signal_timestamp(ts):
    """Signal timestamps are now saved as Karachi-tz ISO strings (see app.py
    karachi_now_str()), but older persisted signals may still be naive
    '%Y-%m-%d %H:%M:%S' strings assumed-Karachi. Handle both so nothing new
    breaks on old data."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=engine.PKT)
        return dt
    except Exception:
        pass
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=engine.PKT)
    except Exception:
        return None


def monitor_once():
    """Open signals ko live price se check karo: TP/SL hit -> closed store + stats update.
    IMPORTANT: age-based expiry runs even when the live price can't be fetched
    (e.g. a delisted/renamed symbol like the old MATICUSDT) - otherwise a dead
    symbol's signal would stay 'open' forever since it can never hit TP/SL."""
    now_dt = datetime.now(engine.PKT)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    with engine._state_lock:
        keys = list(engine.signals_store.keys())

    for key in keys:
        with engine._state_lock:
            sig = engine.signals_store.get(key)
        if not sig:
            continue

        price = engine.get_live_price(sig["symbol"])

        hit = None
        if price is not None:
            if sig["direction"] == "LONG":
                if price >= sig["tp"]:
                    hit = "TP_HIT"
                elif price <= sig["sl"]:
                    hit = "SL_HIT"
            else:
                if price <= sig["tp"]:
                    hit = "TP_HIT"
                elif price >= sig["sl"]:
                    hit = "SL_HIT"
        else:
            print(f"[MONITOR] {sig['symbol']} price unavailable (delisted/renamed on Binance?) - age-check only")

        if hit is None:
            sig_dt = _parse_signal_timestamp(sig.get("timestamp", ""))
            if sig_dt is not None:
                age_h = (now_dt - sig_dt).total_seconds() / 3600.0
                if age_h >= SIGNAL_MAX_AGE_HOURS:
                    hit = "EXPIRED"

        if hit:
            closed = None
            with engine._state_lock:
                closed = engine.signals_store.pop(key, None)
                if closed:
                    closed["result"] = hit
                    closed["close_price"] = price if price is not None else closed.get("entry")
                    closed["closed_at"] = now_str
                    engine.closed_store.append(closed)
                    if hit in ("TP_HIT", "SL_HIT"):
                        stats = engine.bot_stats.setdefault(closed["bot_id"], {"wins": 0, "losses": 0, "total": 0})
                        stats["total"] += 1
                        if hit == "TP_HIT":
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1
            if closed:
                print(f"CLOSED [{closed['bot_id']}] {closed['symbol']} -> {hit} @ {closed.get('close_price')}")
                if hit in ("TP_HIT", "SL_HIT"):
                    bot_cfg = engine.BOT_BY_ID.get(closed["bot_id"], {"id": closed["bot_id"], "name": closed.get("bot_name", closed["bot_id"])})
                    try:
                        engine.post_discord_signal(bot_cfg, closed["symbol"], closed["direction"], closed.get("entry", 0), closed.get("tp", 0), closed.get("sl", 0), closed.get("score", 80), "MONITOR", f"Position closed: {hit} @ {closed.get('close_price')}")
                    except Exception as e:
                        print(f"[DISCORD CLOSE ALERT FAILED] {e}")
                elif hit == "EXPIRED":
                    print(f"[EXPIRED] {closed['symbol']} - no live price for {SIGNAL_MAX_AGE_HOURS}h+, force-closed without counting as win/loss")


def persist_state():
    """Engine memory ko signals/*.json files mein dump karo (repo commit ke liye)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with engine._state_lock:
        open_list = sorted(engine.signals_store.values(), key=lambda s: s.get("timestamp", ""), reverse=True)
        closed_list = list(engine.closed_store[-400:])
        rows = []
        for b in engine.BOT_CONFIGS:
            s = engine.bot_stats.get(b["id"], {"wins": 0, "losses": 0, "total": 0})
            win_rate = round((s["wins"] / s["total"]) * 100, 1) if s["total"] > 0 else 0.0
            rows.append({"bot_id": b["id"], "bot_name": b["name"], "wins": s["wins"], "losses": s["losses"], "total": s["total"], "win_rate": win_rate})

    winner = None
    if rows:
        best = max(rows, key=lambda r: (r["win_rate"], r["total"]))
        if best["total"] > 0:
            winner = best

    save_json(OPEN_PATH, open_list)
    save_json(CLOSED_PATH, closed_list)
    save_json(STATS_PATH, {"bots": rows, "winner": winner, "last_run": now_iso, "ai_enabled": bool(engine.AI_ENABLED), "engine": "github-actions"})

    grouped = {b["id"]: [] for b in engine.BOT_CONFIGS}
    for sig in open_list:
        grouped.setdefault(sig.get("bot_id", "external"), []).append(sig)
    save_json(GROUPED_PATH, grouped)
    save_json(BOTS_META_PATH, engine.BOT_CONFIGS)

    prev = load_json(STATUS_PATH, {}) or {}
    started_iso = prev.get("started_at") or now_iso
    try:
        uptime_seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(started_iso)).total_seconds()))
    except Exception:
        uptime_seconds = 0
    save_json(STATUS_PATH, {
        "started_at": started_iso,
        "last_run": now_iso,
        "uptime_seconds": uptime_seconds,
        "open_signals": len(open_list),
        "closed_trades": len(closed_list),
        "ai_enabled": bool(engine.AI_ENABLED),
    })
    print(f"PERSISTED: open={len(open_list)} closed={len(closed_list)} -> signals/*.json")


def main():
    t0 = time.time()
    symbols_planned = get_symbols()
    print("=" * 60)
    print("BOT ENGINE (HEADLESS) - 24/7 AUTOPILOT CYCLE")
    print(f"AI Mode: {'ON (Gemini -> Groq -> Mistral)' if engine.AI_ENABLED else 'OFF (technical strategies only)'}")
    print(f"Symbols: {len(symbols_planned)} | Bots: {len(engine.BOT_CONFIGS)} | Max open/bot: {MAX_OPEN_PER_BOT}")
    print("=" * 60)

    seed_state()
    monitor_once()

    if is_bot_active():
        print(f"Master switch = 1 - scanning {len(symbols_planned)} symbols with {len(engine.BOT_CONFIGS)} bots...")
        threads = []
        for cfg in engine.BOT_CONFIGS:
            th = threading.Thread(target=scan_bot, args=(cfg, symbols_planned), daemon=True)
            th.start()
            threads.append(th)
            time.sleep(1.5)

        deadline = t0 + SCAN_BUDGET_SECONDS
        for th in threads:
            th.join(timeout=max(5.0, deadline - time.time()))
        still_running = sum(1 for th in threads if th.is_alive())
        if still_running:
            print(f"Scan budget hit with {still_running} bot thread(s) still running - persisting what we have.")
    else:
        print("Master switch = 0 (config/bot_enabled.txt) - scanning PAUSED, monitoring only.")

    time.sleep(2)
    monitor_once()
    persist_state()

    with engine._state_lock:
        open_n = len(engine.signals_store)
        closed_n = len(engine.closed_store)
    print("=" * 60)
    print(f"CYCLE COMPLETE in {round(time.time() - t0, 1)}s - Open: {open_n} | Closed: {closed_n}")
    print("=" * 60)


if __name__ == "__main__":
    main()
