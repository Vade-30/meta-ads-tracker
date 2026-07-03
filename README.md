# Meta Ads Gmail Monitor

A zero-cost, serverless Python automation tool that monitors your Gmail for
[Extend](https://www.paywithextend.com/) virtual card purchase notifications
(Meta/Facebook Ads charges) and sends **persistent Telegram alerts** if a
charge looks suspicious — repeating until you acknowledge with `/ack`.

Runs on GitHub Actions (cron every 10 min). **No server required. $0/month.**

---

## Security Model

> **This repo is public — and that's intentional and safe.**

| What lives where | Location |
|---|---|
| Source code (this repo) | GitHub — public, zero secrets |
| Gmail & Telegram credentials | GitHub Actions Secrets (encrypted) |
| Charge history database | GitHub Actions Cache (never committed) |

The `data/` directory and `credentials.json` are in `.gitignore`. No financial
data or credentials ever touch a commit.

---

## How It Works

Every 10 minutes, GitHub Actions:

1. Restores the SQLite charge history from cache
2. Authenticates to Gmail via OAuth refresh token (env vars only)
3. Searches for Extend purchase notifications newer than the last check
4. Parses each email: extracts merchant, amount, timestamp, card name
5. Skips non-Facebook charges; stores Facebook charges in SQLite
6. Evaluates three detection rules (see below)
7. If any rule fires → sends a Telegram alert and loops until you `/ack` or 30 min elapses
8. Saves the updated charge history back to cache

---

## Detection Rules

### Rule 1 — Fixed Amount
Flags any single charge **over $900.00**.

### Rule 2a — Overall Rolling Frequency (EWMA)
Maintains an [Exponentially Weighted Moving Average](https://en.wikipedia.org/wiki/Exponential_smoothing)
of your charges-per-hour across the last 14 days.  Flags the current hour if
its count exceeds **2× the EWMA baseline**.

As you scale ad campaigns (more charges per hour), the EWMA adapts upward
automatically — only genuine sudden spikes trip the alert.

### Rule 2b — Per-Hour-of-Day Frequency
Separately tracks the average charge count for each specific hour of the day
(e.g., what's normal for 2 PM specifically) across the last 14 days of history.
Flags if the current hour exceeds **2× its historical same-hour average**.

**Cold-start**: if fewer than 5 days of same-hour data exist, Rule 2b is
skipped and only Rule 2a is used.  This is logged clearly.

All three rules are evaluated for every new charge; **all triggered reasons**
are included in the Telegram alert.

### Adjusting Thresholds

All thresholds are named constants at the top of [`rules.py`](rules.py):

```python
AMOUNT_THRESHOLD = 900.00          # Rule 1 — flag if charge exceeds this
SPIKE_MULTIPLIER = 2.0              # Rules 2a/2b — flag if count > N× baseline
EWMA_ALPHA = 0.30                  # EWMA smoothing (higher = more reactive)
EWMA_HISTORY_DAYS = 14             # Days of history for EWMA
HOUR_BASELINE_HISTORY_DAYS = 14    # Days of same-hour history
MIN_DAYS_FOR_HOUR_BASELINE = 5     # Min days before Rule 2b activates
```

---

## Setup

### Step 1 — Google Cloud & Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create
   a new project (or use an existing one).
2. Enable the **Gmail API**:
   - APIs & Services → Enable APIs & Services → search "Gmail API" → Enable
3. Configure the **OAuth consent screen**:
   - APIs & Services → OAuth consent screen
   - User Type: **External**
   - Fill in App name (e.g. "Meta Ads Monitor"), your email as support email
   - Scopes: add `https://www.googleapis.com/auth/gmail.readonly`
   - Test users: add **your Gmail address**
   - Publishing status: leave as **Testing** (no review needed for personal use)
4. Create **OAuth credentials**:
   - APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: **Desktop app**
   - Download the JSON → save as **`credentials.json`** in the repo root
   - (It's in `.gitignore` — safe to keep locally, never commit)

### Step 2 — Generate Your Gmail Refresh Token

Run this locally (one time only):

```bash
pip install google-auth-oauthlib
python get_refresh_token.py
```

A browser window opens.  Sign in with your Gmail account and grant access.
The script prints three values — **copy them immediately**:

```
GMAIL_CLIENT_ID      →  123456789-xxx.apps.googleusercontent.com
GMAIL_CLIENT_SECRET  →  GOCSPX-xxxxxxxxxxxx
GMAIL_REFRESH_TOKEN  →  1//0xxxxxxxxxxxxxxxxxx
```

### Step 3 — Create Your Telegram Bot

1. Open Telegram, search for **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot` → follow prompts → get your **bot token**
   (format: `123456789:AAxxxxxxxxxxxxxxxxxxxxxx`)
3. Start a chat with your new bot (send it any message)
4. Get your **chat ID** — easiest way:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Look for `"chat": {"id": 123456789}` in the response.
   Your chat ID is that number (may be negative for group chats).

### Step 4 — Add GitHub Secrets

In your GitHub repo:
**Settings → Secrets and variables → Actions → New repository secret**

Add all five secrets:

| Secret Name | Value |
|---|---|
| `GMAIL_CLIENT_ID` | From Step 2 |
| `GMAIL_CLIENT_SECRET` | From Step 2 |
| `GMAIL_REFRESH_TOKEN` | From Step 2 |
| `TELEGRAM_BOT_TOKEN` | From Step 3 |
| `TELEGRAM_CHAT_ID` | From Step 3 |

### Step 5 — Push & Verify

```bash
git init
git add .
git commit -m "Initial setup"
git remote add origin https://github.com/YOUR_USERNAME/meta-ads-tracker.git
git push -u origin main
```

Go to your repo → **Actions** tab → you should see the workflow running.

> **Tip:** GitHub Actions cron triggers may be delayed by up to 15 minutes on
> free-tier runners during high-demand periods.

---

## Testing the Telegram Alert Loop

You can trigger a full end-to-end test of the alert system **without waiting
for a real suspicious charge** using the `--test-alert` flag.

### Option A — Run Locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python check_gmail.py --test-alert
```

### Option B — Trigger from GitHub Actions

Go to **Actions → Meta Ads Gmail Monitor → Run workflow**, then temporarily
edit the run step in the workflow to add `--test-alert`:

```yaml
run: python check_gmail.py --test-alert
```

(Revert after testing.)

### What to Expect

- Telegram receives an alert message immediately with `[TEST]` tags
- It repeats every 10 seconds for the first 2 minutes
- Send `/ack` to your bot to stop the loop → confirmation message received
- If you don't `/ack`, it escalates to every 30s, then 60s, then stops at 30 min

---

## Files

```
meta-ads-tracker/
├── check_gmail.py          # Main detection script (run by GitHub Actions)
├── alert.py                # Telegram alert loop
├── rules.py                # Detection rule logic (tune thresholds here)
├── db.py                   # SQLite helpers
├── get_refresh_token.py    # Local-only OAuth setup
├── requirements.txt        # Python dependencies
├── .gitignore              # Excludes credentials.json, data/, *.db
└── .github/
    └── workflows/
        └── check-gmail.yml # GitHub Actions workflow
```

---

## Troubleshooting

**No alerts arriving even with `--test-alert`**
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct
- Make sure you've started a chat with the bot (sent it at least one message)
- Check GitHub Actions logs for Telegram error messages

**`invalid_grant` or auth errors**
- The refresh token has expired or been revoked
- Re-run `get_refresh_token.py` locally and update the GitHub Secret

**"cold start" logged for Rule 2b**
- Normal for the first 5 days — only Rule 2a (EWMA) is active until enough
  per-hour history accumulates

**Workflow not running on schedule**
- GitHub may delay cron triggers — check the Actions tab
- Ensure the repo has had at least one successful push to `main`

**Want to reset charge history**
- Go to GitHub → Actions → Caches → delete the `charge-history-v1` cache
- The next run starts with a fresh empty database

---

## 12-Hour Summary

In addition to real-time fraud alerts, the tool sends a **routine 12-hour
status report** via Telegram twice a day.

### What it includes
```
📊 12-Hour Summary
Charges: 14
Total: $12,600.00
Average amount: $900.00/charge
Average frequency: 1 charge every 51 min
Busiest hour: 2:00 PM–3:00 PM UTC (3 charges)
Period: 12:00 AM – 12:00 PM UTC
```

### When it sends
Twice a day at **12:00 AM UTC** and **12:00 PM UTC**, controlled by
`.github/workflows/summary.yml`.

> **Note:** The times are in UTC. If you want the summary to arrive at
> your local midnight/noon instead, adjust the cron expressions in the
> workflow file. For UTC+8 (Manila), use `0 16 * * *` (midnight local)
> and `0 4 * * *` (noon local).

### How it differs from fraud alerts
| | Fraud Alert | 12-Hour Summary |
|---|---|---|
| Trigger | Suspicious charge detected | Fixed schedule (2×/day) |
| Urgency | High — repeats every 10–60s | Low — single message only |
| Requires /ack | Yes | No |
| Purpose | Immediate action needed | Informational status check |

### How to customize

**Change the schedule** — edit the `cron:` lines in
`.github/workflows/summary.yml`:
```yaml
- cron: '0 0 * * *'   # 12:00 AM UTC  ← change this
- cron: '0 12 * * *'  # 12:00 PM UTC  ← and this
```

**Change the summary period** (default 12 hours) — edit this constant
at the top of `summary.py`:
```python
SUMMARY_PERIOD_HOURS: int = 12   # change to 6, 24, etc.
```

### No new secrets required
The summary uses the same `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
secrets already configured — nothing new to add.

### Independence from fraud alerts
The summary workflow uses a **separate concurrency group** (`gmail-summary`)
from the fraud-detection workflow (`gmail-check`).  This means a long-running
30-minute alert loop never blocks or delays the summary from sending.

---

## Contributing / Personal Use Notes

This is a personal-use tool.  The repo is public only to get unlimited free
GitHub Actions minutes — not for distribution.  PRs are welcome but there
is no support commitment.
