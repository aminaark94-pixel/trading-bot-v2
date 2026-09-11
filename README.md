# trading-bot — 24/7 Cloud Trading Signals (Autopilot) 🤖

Ye system ab **fully cloud-based** hai — GitHub Actions har 15 minute mein bot chalata hai, signals repo mein save hoti hain, aur dashboard unhe live parhta hai. **Aapke Windows PC ki koi zaroorat nahi.**

## 🏗 Architecture

```
GitHub Actions (har 15 min)
        │  python bot_engine.py
        ▼
bot_engine.py (headless 5-bot engine)
  ├── live Binance data scan (5 strategies x symbols)
  ├── AI optional (Gemini/Groq/Mistral, multi-key rotation)
  ├── TP/SL monitor (open signals ko live price se check karta hai)
  └── Discord alerts (optional webhook)
        │  auto-commit
        ▼
signals/*.json (open, closed, stats, signals_by_bot, bots, status)
        │  raw.githubusercontent.com
        ▼
index.html (Cloud Dashboard) — Vercel / GitHub Pages / direct open
```

## 🚀 Quick Start (one-time)

1. **Kuch install karne ki zaroorat nahi** — push hote hi Actions apne aap chalna shuru kar dete hain.
2. Manual test: repo → **Actions** tab → **Run Trading Bot** → **Run workflow**.
3. Har successful run ke baad `signals/` folder auto-update hota hai.

### Secrets (sab optional — bina in ke bhi bot chalta hai)

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `AI_ENABLED` | `true` ya `false` (default = technical mode) |
| `GEMINI_KEYS` | `key1,key2,key3` (comma-separated) |
| `GROQ_KEYS` | `key1,key2` |
| `MISTRAL_KEYS` | `key1,key2` |
| `DISCORD_WEBHOOK_URL` | `https://discord.com/api/webhooks/...` |

🔒 **Key Safety**: Keys sirf GitHub **encrypted secrets** mein rehti hain — code mein kahin nahi, logs mein masked, kabhi publicly expose nahi hoti. Locally chalane ke liye `.env` use karo (already `.gitignore`d).

## 🎛 Controls (bina code ke, GitHub web se)

| File | Kaam |
|---|---|
| `config/bot_enabled.txt` | `1` = scanning ON · `0` = paused (monitoring chalti rahegi) |
| `config/settings.json` | `symbols` list edit karo, e.g. `["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]` |

Env tunables (Actions workflow mein): `SIGNAL_MAX_AGE_HOURS` (default 72), `MAX_OPEN_PER_BOT` (default 3), `SCAN_BUDGET_SECONDS` (default 900).

## 🤖 5 Bots — 5 Different Strategies

| Bot | Strategy | Trend Filter |
|---|---|---|
| bot1 Trend Rider | EMA(9/21) alignment + momentum + higher-TF confirmation | 1h & 4h dono match hon |
| bot2 Reversal Hunter | RSI extremes + rejection wicks | contrarian |
| bot3 Breakout Sniper | 20-bar high/low breakout | trend direction |
| bot4 Scalper Precision | micro pullback + short-term momentum shift | fast, koi strict filter nahi |
| bot5 Conservative Swing | multi-factor confluence + session risk | 1h & 4h dono match hon |

Signals tab par har card ka **Signal Rationale** section batata hai ke trade kyun bana.

## 🧠 AI Mode (optional)

- Default: **OFF** — bot pure technical indicators se signals deta hai (free, fast).
- ON karne ke liye: `AI_ENABLED=true` secret + kam az kam ek provider ki keys.
- Multi-key rotation built-in: rate-limit par agla key automatically use hota hai.
- AI off ho ya keys fail hon → bot phir bhi technical mode mein kaam karta rehta hai.

## 📊 Cloud Dashboard

`index.html` kholo — data automatically `signals/*.json` se aata hai:

- **Vercel**: repo ko Vercel se connect karo → auto-deploy → `https://<project>.vercel.app`
- **GitHub Pages**: Settings → Pages → Branch: `main` → `https://<user>.github.io/<repo>/`
- Ya seedha raw file browser mein kholo.

Features: live signal cards + health bars, TP/SL archive + toasts, session report + CSV export, bot comparison + winner.

## 💻 Local Mode (optional, full dashboard + Testnet demo trades)

```bash
pip install -r requirements.txt
python app.py        # http://127.0.0.1:5055
```

## ⚠️ Disclaimer

Ye signals educational/testing purpose ke liye hain — financial advice nahi. Demo trades sirf Binance **Testnet** par hote hain. Real trading mein lage paise ki zimmedari aapki.
