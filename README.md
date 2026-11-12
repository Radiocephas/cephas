# Deriv Telegram Signal Bot

Reads signals like the one below from a Telegram group and trades them automatically on your
Deriv account (Rise/Fall), with martingale recovery and risk limits.

```
Entry confirmed ✅
🌎 Market: Volatility 75 (1s) Index
⏳ Duration: 5min
📊 Direction: 🟢 RISE
⏰ Entry Time (UTC): 11:30
👉 On loss, use martingale on the next candle, max 3 times
```

For this signal the bot will:

| Time (UTC) | Action |
|---|---|
| 11:30:00 | Buy **Rise** on `1HZ75V`, 5 min, stake = `BASE_STAKE` |
| 11:35:00 | If lost → Recovery 1, stake × `MARTINGALE_MULTIPLIER` |
| 11:40:00 | If lost → Recovery 2, stake × multiplier² |
| 11:45:00 | If lost → Recovery 3 (last), stake × multiplier³ |

The sequence stops as soon as a trade wins, or when a limit (`MAX_STAKE`, `DAILY_LOSS_LIMIT`,
`DAILY_PROFIT_TARGET`, `/pause`) is hit.

## How it works

* **Listener** – a Telethon *user* session logs in as you, so it can read any group you are a
  member of. You don't need to be an admin, and you don't need to add a bot to the group.
* **Control bot** (optional) – a bot from @BotFather sends you every entry, result and error, and
  takes commands: `/status`, `/balance`, `/pause`, `/resume`, `/stake 2`. You can also forward
  or paste a signal to it to trade the signal manually.
* **Deriv** – trades go straight to Deriv's official WebSocket API using your API token. No
  third-party site gets your token.

## Setup

1. Python 3.10+:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```
2. **Telegram API keys**: go to https://my.telegram.org → *API development tools* and copy
   `api_id` and `api_hash` into `TG_API_ID` / `TG_API_HASH`.
3. **Deriv token**: go to https://app.deriv.com/account/api-token and create a token with
   the **Read** and **Trade** scopes. **Start with your demo account** (login ID `VRTC…`).
   Put the token in `DERIV_TOKEN`.
4. *(Optional)* Create a bot with @BotFather, put its token in `BOT_TOKEN`, then send `/start`
   to your bot once so it is allowed to message you.
5. Find the signal group's ID:
   ```bash
   python -m signalbot.main --list-chats
   ```
   The first run asks for your phone number and the login code Telegram sends you. Put the
   group's ID (e.g. `-1001234567890`) in `SIGNAL_CHATS`.
6. Run it. Keep `DRY_RUN=true` first and watch a few signals come through, then set
   `DRY_RUN=false`:
   ```bash
   python -m signalbot.main
   ```

The bot must be running when the signal arrives. Use a VPS or another machine that is always on,
for example with `tmux`, `systemd`, or `nohup python -m signalbot.main &`. Your clock must be in
sync (NTP), because entries are timed to the second.

## Tests

```bash
pip install pytest && python -m pytest -q
```

## ⚠️ Risk warning

Martingale raises the stake after every loss. With multiplier 2.2 and 3 recoveries, one losing
sequence costs `1 + 2.2 + 4.84 + 10.65 ≈ 18.7 ×` your base stake, but a winning sequence earns
only about `0.9 ×` your base stake. A few bad sequences can erase many good ones. Signal groups
rarely publish their real losing record. Test on demo for a good while, set `DAILY_LOSS_LIMIT`,
and trade only money you can afford to lose. Never give your Deriv token or password to a signal
group or a third-party "auto-trader" site.
