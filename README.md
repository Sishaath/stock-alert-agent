# Stock Market Alert Agent

A Python agent that monitors Indian stock markets and sends push notifications to your phone via [ntfy.sh](https://ntfy.sh). Designed for Zerodha users.

## What It Monitors

| Alert | Trigger | Schedule |
|-------|---------|----------|
| **Holdings drop** | Any holding falls 20%+ below your avg buy price | Every 5 min (market hours) |
| **Watchlist drop** | Any watchlist stock falls 20%+ from its 52-week high | Every 5 min (market hours) |
| **Volume spike** | Any stock has 3x+ today's volume vs 30-day average | Every 5 min (market hours) |
| **FII/DII flow** | Net FII or DII cash flow exceeds ₹3,000 Cr | Once daily at 9:00 AM IST |
| **SEBI / NSE filings** | New corporate announcement for any tracked stock | Every 30 min |

Market hour checks run **9:15 AM – 3:30 PM IST, Monday–Friday only**.  
Duplicate alerts are suppressed for **4 hours** per stock per alert type.

---

## Prerequisites

- **Python 3.11+**
- A **Zerodha Kite** account with API access (₹2,000/month subscription at [kite.trade](https://kite.trade))
- The **ntfy app** on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396347) | [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) subscribed to `my_stock_alerts`

---

## Local Setup

### 1. Clone / download the project

```bash
git clone <your-repo-url>
cd stock_alert_agent
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate      # macOS / Linux
# venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### 3. Fill in your credentials

Edit `.env`:

```env
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
KITE_ACCESS_TOKEN=           # Leave blank for now — see Step 4
NTFY_TOPIC=my_stock_alerts
```

Get your API key and secret from [developers.kite.trade](https://developers.kite.trade) → **Your Apps**.

### 4. Generate your first access token

The Kite access token **expires every day at 6:00 AM IST**. Run this each morning before market open:

```bash
python generate_token.py
```

Follow the prompts — it will open a browser login and save the token to your `.env` automatically.

### 5. Edit your watchlist

Open `watchlist.json` and add/remove stocks as needed:

```json
["RELIANCE", "TCS", "INFY", "HDFCBANK", "WIPRO"]
```

You can edit this file at any time — the agent picks up changes on the next check.

### 6. Run the agent

```bash
python main.py
```

You'll receive a startup notification on your phone confirming everything works.

---

## Subscribe to Alerts on Your Phone

1. Install the **ntfy** app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) | [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Tap **+** → enter topic: `my_stock_alerts`
3. Done — notifications will appear instantly

You can also view alerts in a browser at: `https://ntfy.sh/my_stock_alerts`

---

## Deploy to Railway (Always-On, Free)

Railway runs your agent 24/7 so you don't need your laptop open.

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/your-username/stock-alert-agent.git
git push -u origin main
```

> **Important:** `.env` is in `.gitignore` — your credentials will NOT be pushed to GitHub.

### Step 2 — Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `stock-alert-agent` repository
4. Railway detects the `Procfile` and starts a **worker** service automatically

### Step 3 — Add environment variables in Railway

In your Railway project → **Variables** tab → add these one by one:

| Variable | Value |
|----------|-------|
| `KITE_API_KEY` | Your Kite API key |
| `KITE_API_SECRET` | Your Kite API secret |
| `KITE_ACCESS_TOKEN` | Today's access token (from `generate_token.py`) |
| `NTFY_TOPIC` | `my_stock_alerts` |

### Step 4 — Deploy

Railway deploys automatically when you push to GitHub.  
Check **Deployments → Logs** to confirm the agent started and you received a startup notification.

### Step 5 — Renew the access token every morning

Since Kite tokens expire daily:

```bash
# Run locally each morning before 9:15 AM IST
python generate_token.py

# Copy the printed token, then update Railway:
railway variables set KITE_ACCESS_TOKEN=<new_token>

# Railway redeploys automatically after the variable update
```

Or update it via the Railway dashboard: **Variables → KITE_ACCESS_TOKEN → Edit**.

---

## Project Structure

```
stock_alert_agent/
├── main.py              # Entry point — scheduler and task orchestration
├── kite_client.py       # Zerodha API wrapper (holdings, quotes, history)
├── alerts.py            # All alert logic and duplicate-prevention
├── scrapers.py          # NSE (FII/DII, announcements) and SEBI scrapers
├── notifier.py          # ntfy.sh push notification sender
├── watchlist_manager.py # Read/write watchlist.json
├── config.py            # All thresholds and settings (edit here to tune)
├── generate_token.py    # Run daily to refresh Kite access token
├── watchlist.json       # Your stock watchlist — edit freely
├── alerts_log.json      # Auto-managed duplicate-alert tracking
├── .env                 # Your secrets — never commit this
├── requirements.txt     # Python dependencies
├── Procfile             # Railway deployment config
└── .gitignore           # Keeps .env out of git
```

---

## Tuning Thresholds

All alert thresholds are in `config.py`:

```python
HOLDINGS_DROP_THRESHOLD  = 20    # % drop from avg buy price
WATCHLIST_DROP_THRESHOLD = 20    # % drop from 52-week high
VOLUME_SPIKE_MULTIPLIER  = 3     # x times the 30-day avg volume
FII_DII_THRESHOLD_CRORES = 3000  # ₹ crore net FII or DII flow
ALERT_COOLDOWN_HOURS     = 4     # Suppress repeat alerts for 4 hours
```

---

## Troubleshooting

**No alerts received at all**
- Check that you subscribed to the correct ntfy topic
- Run `python main.py` locally and watch the logs

**"Kite authentication failed" alert**
- Your access token expired — run `python generate_token.py` and update `KITE_ACCESS_TOKEN`

**FII/DII data returns empty**
- NSE sometimes changes their API format. Check the logs for the raw response.
- The agent will retry on the next scheduled run.

**SEBI scraper not finding announcements**
- NSE may return 403 if cookies expire mid-session. The scraper retries once automatically.
- Check `Deployments → Logs` on Railway for details.

**Agent crashes on Railway**
- Check the logs. The most common cause is an expired Kite token.
- Railway auto-restarts crashed workers, so a fresh token + redeploy fixes it.
