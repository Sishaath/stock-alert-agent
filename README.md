# Stock Market Alert Agent

A Python agent that monitors your Indian stock portfolio and sends push notifications to your phone via [ntfy.sh](https://ntfy.sh). Designed for Zerodha users. Runs 24/7 on [Railway](https://railway.app).

## Features

| Feature | Details |
|---------|---------|
| **Price drop alerts** | Holdings fall 20%+ below avg buy price · Watchlist stocks fall 20%+ from 52-week high |
| **Volume spike alerts** | Any stock has 3x+ today's volume vs 30-day average |
| **FII/DII flow alerts** | Net FII or DII cash flow exceeds ₹3,000 Cr |
| **SEBI / NSE filings** | New corporate announcements for any tracked stock |
| **Live dashboard** | Web dashboard at `/holdings` with P&L, charts, watchlist management |
| **Fully automated login** | Kite token auto-renewed daily at 6 AM IST via stored credentials + TOTP |
| **Auto portfolio sync** | Holdings synced from Zerodha on every startup and daily at 9:15 AM — buys/sells always reflected automatically |

Market hour checks run **9:15 AM – 3:30 PM IST, Monday–Friday only**.  
Duplicate alerts are suppressed for **4 hours** per stock per alert type.

---

## How Holdings Stay in Sync

Holdings are sourced directly from `kite.holdings()` (Zerodha's API) on every service startup and daily at market open. You **never need to manually add or remove stocks** — buy or sell on Zerodha and the dashboard reflects it automatically. Zerodha postbacks provide additional real-time updates during the day.

---

## Prerequisites

- **Python 3.11+**
- A **Zerodha Kite** account with API access (₹2,000/month at [kite.trade](https://kite.trade))
- The **ntfy app** on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396347) | [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) subscribed to `my_stock_alerts`

---

## Local Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd stock_alert_agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a `.env` file

```env
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_USER_ID=your_zerodha_user_id
KITE_PASSWORD=your_zerodha_password
KITE_TOTP_SECRET=your_totp_secret_key
NTFY_TOPIC=my_stock_alerts
DATA_DIR=./data
```

Get your API credentials from [developers.kite.trade](https://developers.kite.trade) → **Your Apps**.  
`KITE_TOTP_SECRET` is the base32 secret shown when you set up TOTP 2FA on your Zerodha account (the raw key, not the QR code).

> **Important:** `.env` is in `.gitignore` — your credentials will NOT be pushed to GitHub.

### 3. Run the agent

```bash
python main.py
```

On first run, the agent logs in automatically, syncs your portfolio, and sends a startup notification to your phone.

---

## Deploy to Railway (Always-On)

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/your-username/stock-alert-agent.git
git push -u origin main
```

### Step 2 — Create a Railway project

1. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
2. Select your `stock-alert-agent` repository
3. Railway detects the `Procfile` and starts a **web** service

### Step 3 — Add a persistent volume

In Railway → your service → **Volumes** tab:
- Mount path: `/data`

This keeps `holdings.json`, the token, and caches across restarts.

### Step 4 — Add environment variables

In Railway → **Variables** tab:

| Variable | Value |
|----------|-------|
| `KITE_API_KEY` | Your Kite API key |
| `KITE_API_SECRET` | Your Kite API secret |
| `KITE_USER_ID` | Your Zerodha user ID |
| `KITE_PASSWORD` | Your Zerodha password |
| `KITE_TOTP_SECRET` | Your TOTP base32 secret |
| `NTFY_TOPIC` | `my_stock_alerts` |
| `DATA_DIR` | `/data` |

### Step 5 — Set Zerodha postback URL

In [developers.kite.trade](https://developers.kite.trade) → your app → **Postback URL**:

```
https://<your-railway-url>/postback
```

This enables real-time trade updates in addition to the daily portfolio sync.

### Step 6 — Deploy

Railway deploys automatically on every `git push`. Check **Deployments → Logs** to confirm startup.

---

## Dashboard

The live dashboard is available at `https://<your-railway-url>/holdings`.

- Portfolio P&L with live prices
- Holdings and watchlist management (add/remove stocks)
- Intraday charts per stock
- Alert log and SEBI filing history
- Kite token status and manual re-auth

---

## Kite Token — Fully Automated

The Kite access token expires daily at **6:00 AM IST**. The agent handles this automatically:

- **6:05 AM IST** — logs in using `KITE_USER_ID`, `KITE_PASSWORD`, and a TOTP code auto-generated from `KITE_TOTP_SECRET`
- **On every restart** — checks if the saved token is from today; re-logs in if not
- You receive a push notification when the token is renewed

No manual action is ever needed.

**Fallback:** If auto-login fails, visit `https://<your-railway-url>/kite/renew` to authorize manually.

---

## Project Structure

```
stock_alert_agent/
├── main.py              # Entry point — scheduler and task orchestration
├── kite_client.py       # Zerodha API wrapper — login, quotes, portfolio sync
├── alerts.py            # All alert logic and duplicate-prevention
├── holdings_manager.py  # Read/write holdings.json, sync from Kite API
├── watchlist_manager.py # Read/write watchlist.json
├── postback_server.py   # Flask server — trade postbacks + live dashboard
├── scrapers.py          # NSE (FII/DII, announcements) and SEBI scrapers
├── notifier.py          # ntfy.sh push notification sender
├── market_data.py       # Market data helpers
├── config.py            # Thresholds and settings
├── generate_token.py    # One-time manual token helper (not needed in normal use)
├── requirements.txt     # Python dependencies
├── Procfile             # Railway deployment config
└── .gitignore
```

---

## Tuning Thresholds

All alert thresholds are in `config.py` and can also be adjusted live from the dashboard Settings tab:

| Setting | Default | Description |
|---------|---------|-------------|
| `HOLDINGS_DROP_THRESHOLD` | 20% | Drop from avg buy price to trigger holding alert |
| `WATCHLIST_DROP_THRESHOLD` | 20% | Drop from 52-week high to trigger watchlist alert |
| `VOLUME_SPIKE_MULTIPLIER` | 3x | Today's volume vs 30-day average |
| `FII_DII_THRESHOLD_CRORES` | ₹3,000 Cr | Net FII or DII flow threshold |
| `ALERT_COOLDOWN_HOURS` | 4 hours | Suppress repeat alerts per stock per type |

---

## Troubleshooting

**Stock I bought isn't showing in the dashboard**
- Holdings sync from Zerodha on every startup — redeploy or restart the service and it will appear.
- Same-day buys (T1) are included immediately.

**"Kite token expired" notifications at market open**
- This was a known issue caused by Railway restarts loading a stale token. Fixed — the agent now validates the token age on startup and re-logs in automatically.

**No alerts received**
- Check you subscribed to the correct ntfy topic (`my_stock_alerts`)
- Check Railway logs for errors

**FII/DII data empty**
- NSE occasionally changes their API. The agent retries on the next scheduled run. Check logs for details.

**Auto-login failed notification**
- Visit `https://<your-railway-url>/kite/renew` to re-authenticate manually
- Verify `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET` are correct in Railway Variables (no extra spaces)
