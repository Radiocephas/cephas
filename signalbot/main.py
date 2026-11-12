"""Entry point: listen to the signal group and trade on Deriv.

    python -m signalbot.main              # run the bot
    python -m signalbot.main --list-chats # print your chats and their IDs
    python -m signalbot.main --list-accounts  # print your Deriv accounts and their IDs
    python -m signalbot.main --backfill 2000  # score the group's past signals
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from telethon import TelegramClient, events

from .config import Config
from .deriv import DerivClient, list_accounts
from .parser import parse_signal
from .stats import format_report, load_journal
from .trader import Trader

log = logging.getLogger("signalbot")

HELP = """Commands:
/status – account, today's P/L, running trades
/report – the group's real win rate and what each strategy would have made
/balance – Deriv balance
/pause – stop taking new signals
/resume – resume
/stake <amount> – change base stake
/help – this message

You can also forward or paste a signal to me to trade it manually."""


def _chat_ref(value: str):
    return int(value) if value.lstrip("-").isdigit() else value


async def list_chats(cfg: Config) -> None:
    async with TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash) as user:
        async for d in user.iter_dialogs():
            print(f"{d.id:>16}  {d.name}")


async def backfill(cfg: Config, limit: int) -> None:
    """Score the group's past signals against Deriv's price history."""
    from .tracker import track_signal

    journal = load_journal()
    seen = {(r["entry"], r["market"], r["direction"]) for r in journal}
    deriv = DerivClient(cfg.deriv_token, cfg.deriv_app_id, cfg.deriv_account_id)
    await deriv.connect()
    added = 0
    async with TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash) as user:
        signals = []
        for chat in cfg.signal_chats:
            async for msg in user.iter_messages(_chat_ref(chat), limit=limit):
                sig = parse_signal(msg.raw_text or "")
                if sig:
                    signals.append((sig.entry_datetime(now=msg.date), sig))
        for entry, sig in sorted(signals, key=lambda x: x[0]):
            if (entry.isoformat(), sig.market, sig.direction_label) in seen:
                continue
            seen.add((entry.isoformat(), sig.market, sig.direction_label))
            if await track_signal(deriv, sig, entry, traded=False, default_payout=cfg.payout_rate,
                                  currency=cfg.currency):
                added += 1
    await deriv.close()
    print(f"Added {added} past signals.\n")
    print(format_report(load_journal(), cfg.martingale_multiplier, cfg.payout_rate, cfg.base_stake))


async def run(cfg: Config) -> None:
    user = TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
    await user.start()  # asks for phone + login code on first run

    bot = None
    if cfg.bot_token:
        bot = TelegramClient(cfg.tg_session + "_bot", cfg.tg_api_id, cfg.tg_api_hash)
        await bot.start(bot_token=cfg.bot_token)

    owner_id = cfg.owner_id or (await user.get_me()).id

    async def notify(text: str) -> None:
        log.info(text.replace("\n", " | "))
        try:
            if bot:
                await bot.send_message(owner_id, text)
            else:
                await user.send_message("me", text)   # Saved Messages
        except Exception as exc:
            log.warning("Notification failed: %s", exc)

    deriv = DerivClient(cfg.deriv_token, cfg.deriv_app_id, cfg.deriv_account_id)
    await deriv.connect()
    trader = Trader(cfg, deriv, notify)

    acct = deriv.account
    await notify(
        f"🤖 Signal bot started\nAccount {acct.get('loginid')} "
        f"({'DEMO' if acct.get('is_virtual') else 'REAL MONEY'}) – balance {acct.get('balance')} {acct.get('currency')}\n"
        f"Base stake {cfg.base_stake} ×{cfg.martingale_multiplier} up to {cfg.max_martingale_steps} steps, "
        f"max stake {cfg.max_stake}, daily loss limit {cfg.daily_loss_limit}"
        + ("\n🧪 DRY RUN – no real trades" if cfg.dry_run else "")
        + (f"\n🛡 Edge gate: {trader.edge_gate_reason() or 'group track record OK – trading live'}"
           if cfg.edge_gate else "")
    )

    # --- signals from the group -------------------------------------------------
    chats = [_chat_ref(c) for c in cfg.signal_chats]

    @user.on(events.NewMessage(chats=chats))
    @user.on(events.MessageEdited(chats=chats))
    async def on_signal(event):
        signal = parse_signal(event.raw_text)
        if not signal:
            return
        rejected = trader.submit(signal)
        if rejected and rejected != "duplicate signal ignored":
            await notify(f"📝 {signal.summary()}\nNot trading: {rejected}")

    # --- commands --------------------------------------------------------------
    control = bot or user
    owner_filter = (lambda e: e.sender_id == owner_id) if bot else (lambda e: e.out and e.chat_id == owner_id)  # Saved Messages

    @control.on(events.NewMessage(func=owner_filter))
    async def on_command(event):
        text = (event.raw_text or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]

        if cmd in ("/start", "/help"):
            await event.reply(HELP)
        elif cmd == "/status":
            trader.stats.roll()
            s = trader.stats
            running = "\n".join(f"• {k}" for k in trader.active) or "none"
            await event.reply(
                f"{'⏸ PAUSED' if trader.paused else '▶️ RUNNING'}\n"
                f"Account {acct.get('loginid')} ({'DEMO' if acct.get('is_virtual') else 'REAL'})\n"
                f"Today: {s.profit:+.2f} ({s.wins}W/{s.losses}L)\n"
                f"Base stake {await trader.base_stake():.2f}\n"
                f"Edge gate: {trader.edge_gate_reason() or 'open'}\nRunning:\n{running}")
        elif cmd == "/report":
            await event.reply(format_report(load_journal(trader.journal), cfg.martingale_multiplier,
                                            cfg.payout_rate, await trader.base_stake()))
        elif cmd == "/balance":
            bal, cur = await deriv.balance()
            await event.reply(f"💰 {bal:.2f} {cur}")
        elif cmd == "/pause":
            trader.paused = True
            await event.reply("⏸ Paused – new signals will be skipped (running sequences continue).")
        elif cmd == "/resume":
            trader.paused = False
            await event.reply("▶️ Resumed.")
        elif cmd == "/stake":
            try:
                value = float(arg)
                if not 0.35 <= value <= cfg.max_stake:
                    raise ValueError
            except ValueError:
                await event.reply(f"Usage: /stake <amount between 0.35 and {cfg.max_stake}>")
                return
            cfg.base_stake = value
            await event.reply(f"Base stake set to {value:.2f}")
        elif bot:   # manual signals only via the bot chat (avoids loops in Saved Messages)
            signal = parse_signal(text)
            if signal:
                rejected = trader.submit(signal)
                if rejected:
                    await event.reply(f"Skipped: {rejected}")

    log.info("Listening for signals in %s", cfg.signal_chats)
    await user.run_until_disconnected()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("trades.log")])
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-chats", action="store_true", help="print your Telegram chats and IDs")
    ap.add_argument("--list-accounts", action="store_true", help="print your Deriv Options accounts")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="score the last N messages of the signal group against price history")
    args = ap.parse_args()
    cfg = Config.load()
    if args.list_accounts:
        import json
        print(json.dumps(asyncio.run(list_accounts(cfg.deriv_token, cfg.deriv_app_id)), indent=2))
        print("\nPut the ID of the account to trade (use a DEMO one first) in DERIV_ACCOUNT_ID.")
    elif args.list_chats:
        asyncio.run(list_chats(cfg))
    elif args.backfill:
        asyncio.run(backfill(cfg, args.backfill))
    else:
        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
