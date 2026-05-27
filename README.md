# X Stock Bot

Monitors an X (Twitter) account, interprets stock mentions using Claude AI,
pulls live market data, and sends a Telegram alert on every relevant post —
**no $100/month X API required**.

## How it works

```
Apify (scrapes X) → Claude (interprets signal) → yfinance (live data) → Telegram
```

1. Every N minutes the bot triggers an Apify actor that scrapes the target account
2. Each new tweet is sent to Claude (claude-sonnet-4-6) which extracts:
   - Is this about a specific stock?
   - Which tickers are mentioned?
   - Signal: BUY / SELL / HOLD / WATCH
   - Confidence: low / medium / high
3. Live price, P/E, 52-week range, and market cap are fetched via `yfinance` (free)
4. A formatted Telegram message is sent with the full assessment

---

## Credentials you need

| Credential | Where to get it | Cost |
|---|---|---|
| **Apify API token** | [console.apify.com](https://console.apify.com/account/integrations) | Free tier: ~$5 credit/mo |
| **Anthropic API key** | [console.anthropic.com](https://console.anthropic.com) | ~$0.001–0.003/tweet |
| **Telegram Bot Token** | Message `@BotFather` → `/newbot` | Free |
| **Telegram Chat ID** | Message `@userinfobot` | Free |

---

## Setup

### 1. Install dependencies

```bash
cd x-stock-bot
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Open .env and fill in your credentials
```

Key `.env` settings:

```bash
APIFY_API_TOKEN=apify_api_xxxx         # from Apify console
X_USERNAME=financeguru                 # account to watch, no @
ANTHROPIC_API_KEY=sk-ant-xxxx
TELEGRAM_BOT_TOKEN=123456789:ABCdef
TELEGRAM_CHAT_ID=-1001234567890        # your chat/group ID
POLL_INTERVAL_SECONDS=900              # see cost table below
MIN_CONFIDENCE=medium                  # skip low-confidence alerts
```

### 3. Run

```bash
python main.py
```

---

## Apify cost & polling interval guide

Each Apify actor run costs roughly **$0.005–$0.015** depending on how long it takes.

| Polling interval | Runs/month | Est. Apify cost |
|---|---|---|
| Every 15 min (900s) | ~2,880 | ~$14–43/mo — **needs paid plan** |
| Every 30 min (1800s) | ~1,440 | ~$7–22/mo |
| Every 60 min (3600s) | ~720 | ~$4–11/mo → **free tier works** |
| Every 2 hours (7200s) | ~360 | ~$2–5/mo → **free tier works** |

**Recommendation:** Start at 3600s on the free tier to verify everything works,
then upgrade Apify and reduce the interval if you want faster alerts.

---

## Running as a background service on macOS

### Option A — keep it simple (nohup)

```bash
nohup python main.py >> bot.log 2>&1 &
echo $! > bot.pid   # save PID so you can kill it later
```

### Option B — launchd (survives reboots)

Save this as `~/Library/LaunchAgents/com.adrien.xstockbot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>com.adrien.xstockbot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/adriennichol/x-stock-bot/.venv/bin/python</string>
    <string>/Users/adriennichol/x-stock-bot/main.py</string>
  </array>
  <key>WorkingDirectory</key> <string>/Users/adriennichol/x-stock-bot</string>
  <key>RunAtLoad</key>      <true/>
  <key>KeepAlive</key>      <true/>
  <key>StandardOutPath</key>  <string>/Users/adriennichol/x-stock-bot/bot.log</string>
  <key>StandardErrorPath</key><string>/Users/adriennichol/x-stock-bot/bot.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.adrien.xstockbot.plist
```

---

## Sample Telegram alert

```
📣 @financeguru just posted

_Just loaded up on $NVDA calls. AI infrastructure play is real.
Q2 earnings will prove it. High conviction 🚀_

🤖 AI Assessment
🟢 Signal: BUY   Confidence: ▓▓▓ (high)
💬 Author is expressing strong bullish conviction on NVIDIA ahead of
   Q2 earnings, citing AI infrastructure as the core thesis.

📈 Live Market Data
NVDA (NVIDIA Corporation)  $1,247.60 ▲2.14%
  Mkt cap $3,078.4B · P/E 74.2 · 52w $462–$1,390

🔗 View tweet
```

---

## File overview

| File | What it does |
|---|---|
| `main.py` | Polling loop — ties everything together |
| `config.py` | Reads `.env` into a typed config object |
| `x_monitor.py` | Calls Apify to scrape tweets |
| `stock_analyzer.py` | Claude interprets tweet + yfinance fetches data |
| `telegram_bot.py` | Formats and delivers the Telegram message |

---

## Troubleshooting

**No tweets coming through?**
- Check `bot.log` for Apify errors
- Confirm the Apify actor `apidojo/tweet-scraper` is still available; if not,
  set `APIFY_ACTOR_ID=quacker/twitter-scraper` in `.env` as a fallback

**Telegram not delivering?**
- Make sure you've started a conversation with your bot first (send it `/start`)
- For group chats, add the bot to the group and use the group's negative chat ID

**Too many alerts?**
- Raise `MIN_CONFIDENCE=high` in `.env` to only get high-conviction signals
- Confirm `exclude retweets` logic is working (tweets starting with "RT @" are skipped)
