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
| 11:40:00 | If lost and `MAX_MARTINGALE_STEPS` ≥ 2 → Recovery 2 |
| 11:45:00 | If lost and `MAX_MARTINGALE_STEPS` = 3 → Recovery 3 |

By default it trades **only after the group has proven a winning record** (see *Losing less*
below); until then every signal is paper-tracked. The sequence stops as soon as a trade wins, or when a limit (`MAX_STAKE`, `DAILY_LOSS_LIMIT`,
`DAILY_PROFIT_TARGET`, `/pause`) is hit.

## How it works

* **Listener** – a Telethon *user* session logs in as you, so it can read any group you are a
  member of. You don't need to be an admin, and you don't need to add a bot to the group.
* **Control bot** (optional) – a bot from @BotFather sends you every entry, result and error, and
  takes commands: `/status`, `/report`, `/balance`, `/pause`, `/resume`, `/stake 2`. You can also forward
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
3. **Deriv access**: Deriv retired its old API (`ws.derivws.com`) in 2026, so the bot uses
   the new Options API at `api.derivws.com`. At https://developers.deriv.com, register an app to
   get an **App ID**, and create a **Personal Access Token** with trading permission. Put them in
   `DERIV_APP_ID` and `DERIV_TOKEN`. Then run `python -m signalbot.main --list-accounts` and copy
   your **demo** account's ID into `DERIV_ACCOUNT_ID`.
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

## Losing less: the edge gate

Rise/Fall on Deriv pays about 95% of the stake, so a signal group must be right **more than about
51.3%** of the time just to break even. Martingale does not change that: it multiplies your
expected result in whichever direction it already goes, and it adds rare but large losses. So the
bot focuses on one question: *do these signals actually win?*

1. **Every signal is scored.** Whether it was traded or not, the bot checks Deriv's tick history
   after the window closes and records in `journal.jsonl` whether the entry and each recovery
   candle won.
2. **Backfill the past.** Measure the group's real record right away instead of waiting weeks:
   ```bash
   python -m signalbot.main --backfill 3000
   ```
3. **`/report`** shows the measured win rate with its 95% range, the break-even rate, a verdict,
   and what flat staking and martingale ×1/×2/×3 would have made, with the drawdown for each.
4. **Edge gate.** Real trades are placed only after `EDGE_MIN_SIGNALS` signals have been tracked
   **and** the win rate over the last `EDGE_WINDOW` signals is at least break-even +
   `EDGE_MARGIN`. If the group's form drops, the bot goes back to paper-tracking on its own.
5. **Limits.** `MAX_LOSING_SEQUENCES` stops the bot after N signals that lose every step in one
   day. `DAILY_LOSS_LIMIT` and `MAX_STAKE` stay in force. `STAKE_PERCENT` sizes stakes as a % of
   your balance, so stakes shrink after losses and grow after wins.

How sure you can be depends on how many signals have been measured. A true 60% win rate looks like
39–78% after 20 signals, 46–72% after 50, 50–69% after 100 and 53–67% after 200. Judge the group
on **100+ signals**, not on a good week.

## Strategy backtest

```bash
python -m signalbot.backtest --days 90
```
Downloads 1-minute prices from Deriv's public feed (no token needed) and tests about 50
strategies per market with a walk-forward split and a luck-corrected significance test.

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
