# LeetCode Daily Tracker

A lightweight Python daemon that runs on any always-on server.

## What it does

1. Every 30 minutes it calls LeetCode's GraphQL API.
2. Checks if you've made **any** submission today.
3. If you haven't and it's **before noon**, it fetches the code from your last
   accepted submission and resubmits it automatically to keep your streak alive.
4. After noon it only warns — it won't blindly resubmit late in the day.
5. Logs everything to `tracker.log`.

---

## Quick Start

### 1. Clone / copy files to your server

```bash
scp -r ./leetcode-tracker ubuntu@your-server:~/
```

### 2. Create a Python virtual environment

```bash
cd ~/leetcode-tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Get your LeetCode session cookie

1. Open **https://leetcode.com** in your browser and log in.
2. Press **F12** → **Network** tab.
3. Refresh the page. Click any request to leetcode.com.
4. Under **Request Headers**, copy the full value of the **Cookie** header.
5. Also copy just the `csrftoken=...` value from inside that cookie string.

### 4. Create your `.env` file

```bash
cp .env.example .env
nano .env          # fill in your values
```

| Variable | Description |
|---|---|
| `LEETCODE_USERNAME` | Your LeetCode username |
| `LEETCODE_COOKIE` | Full Cookie header string from DevTools |
| `LEETCODE_CSRF` | Just the `csrftoken` value |
| `POLL_INTERVAL_MINUTES` | How often to check (default: 30) |
| `DEADLINE_HOUR` | Auto-resubmit before this hour only (default: 12) |

### 5. Test it manually first

```bash
source venv/bin/activate
python tracker.py
```

Watch `tracker.log` — it should print your recent submission within seconds.

---

## Deploy as a systemd service (runs forever, survives reboots)

```bash
# Edit the service file — change User and WorkingDirectory to match your server
nano leetcode-tracker.service

# Install it
sudo cp leetcode-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable leetcode-tracker
sudo systemctl start leetcode-tracker

# Check it's running
sudo systemctl status leetcode-tracker

# Watch live logs
sudo journalctl -u leetcode-tracker -f
```

---

## Updating your cookie (do this every ~2 weeks when session expires)

LeetCode sessions expire. When the tracker starts logging `401` or `403` errors:

1. Log in to LeetCode in your browser again.
2. Copy the new Cookie header value from DevTools.
3. Update `.env`.
4. Restart: `sudo systemctl restart leetcode-tracker`

---

## state.json

The tracker writes a small `state.json` to remember if it already resubmitted
today. This prevents it from resubmitting multiple times in the same day. It
resets automatically the next day.

---

## Caveats

- LeetCode does not expose an official public API — this uses the same GraphQL
  endpoint their website uses. It may break if LeetCode changes their API.
- The cookie expires periodically (usually 2–4 weeks). You'll need to refresh it.
- The auto-resubmit sends the exact same code as your last accepted submission.
  LeetCode treats it as a new submission, so your streak is maintained.
