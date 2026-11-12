"""Executes signals on Deriv with martingale recovery and risk limits."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Config
from .deriv import DerivClient, DerivError
from .parser import Signal

log = logging.getLogger(__name__)
Notify = Callable[[str], Awaitable[None]]


def martingale_stake(base: float, multiplier: float, step: int) -> float:
    return round(base * multiplier ** step, 2)


@dataclass
class DayStats:
    day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    profit: float = 0.0
    wins: int = 0
    losses: int = 0

    def roll(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.profit, self.wins, self.losses = today, 0.0, 0, 0


class Trader:
    def __init__(self, cfg: Config, deriv: DerivClient, notify: Notify):
        self.cfg = cfg
        self.deriv = deriv
        self.notify = notify
        self.paused = False
        self.stats = DayStats()
        self.active: dict[str, asyncio.Task] = {}
        self._seen: set[str] = set()

    # --------------------------------------------------------------- public API
    def submit(self, signal: Signal) -> str | None:
        """Schedule a signal. Returns a reason string if it was rejected."""
        entry = signal.entry_datetime()
        key = f"{signal.market}|{signal.contract_type}|{entry:%Y-%m-%d %H:%M}"
        if key in self._seen:
            return "duplicate signal ignored"
        self._seen.add(key)
        task = asyncio.create_task(self._run(signal, entry, key))
        self.active[key] = task
        task.add_done_callback(lambda _t: self.active.pop(key, None))
        return None

    def limits_reason(self) -> str | None:
        self.stats.roll()
        if self.paused:
            return "bot is paused (/resume to continue)"
        if self.cfg.daily_loss_limit > 0 and self.stats.profit <= -self.cfg.daily_loss_limit:
            return f"daily loss limit reached ({self.stats.profit:.2f})"
        if self.cfg.daily_profit_target > 0 and self.stats.profit >= self.cfg.daily_profit_target:
            return f"daily profit target reached (+{self.stats.profit:.2f})"
        return None

    # --------------------------------------------------------------- internals
    async def _run(self, signal: Signal, entry: datetime, key: str) -> None:
        try:
            await self._execute(signal, entry)
        except Exception as exc:  # keep the bot alive whatever happens
            log.exception("Signal %s failed", key)
            await self.notify(f"⚠️ Error while trading {signal.summary()}:\n{exc}")

    async def _execute(self, signal: Signal, entry: datetime) -> None:
        symbol = self.deriv.resolve_symbol(signal.market)
        steps = self.cfg.max_martingale_steps
        if signal.martingale_steps is not None:
            steps = min(steps, signal.martingale_steps)

        await self.notify(f"📥 Signal accepted: {signal.summary()}\n"
                          f"Symbol {symbol}, base stake {self.cfg.base_stake:.2f}, up to {steps} recoveries")

        sequence_profit = 0.0
        for step in range(steps + 1):
            reason = self.limits_reason()
            if reason:
                await self.notify(f"⛔ Not trading step {step}: {reason}")
                return

            stake = martingale_stake(self.cfg.base_stake, self.cfg.martingale_multiplier, step)
            if stake > self.cfg.max_stake:
                await self.notify(f"⛔ Step {step} stake {stake:.2f} exceeds MAX_STAKE "
                                  f"{self.cfg.max_stake:.2f}; stopping sequence.")
                return

            step_time = entry + timedelta(seconds=signal.duration_seconds * step)
            if not await self._wait_until(step_time):
                await self.notify(f"⌛ Missed entry {step_time:%H:%M:%S} UTC by more than "
                                  f"{self.cfg.late_entry_tolerance}s; skipping.")
                return

            label = "Entry" if step == 0 else f"Recovery {step}/{steps}"
            won, profit = await self._place(signal, symbol, stake, label)
            if won is None:          # buy failed – don't martingale on unknown state
                return
            sequence_profit += profit
            if won:
                await self.notify(f"✅ WIN on {label}. Sequence P/L {sequence_profit:+.2f} | "
                                  f"Today {self.stats.profit:+.2f}")
                return

        await self.notify(f"❌ Sequence lost after {steps} recoveries. P/L {sequence_profit:+.2f} | "
                          f"Today {self.stats.profit:+.2f}")

    async def _wait_until(self, when: datetime) -> bool:
        delay = (when - datetime.now(timezone.utc)).total_seconds()
        if delay < -self.cfg.late_entry_tolerance:
            return False
        if delay > 0:
            await asyncio.sleep(delay)
        return True

    async def _place(self, signal: Signal, symbol: str, stake: float, label: str) -> tuple[bool | None, float]:
        if self.cfg.dry_run:
            await self.notify(f"🧪 DRY RUN {label}: {signal.direction_label} {symbol} "
                              f"stake {stake:.2f} for {signal.duration}{signal.duration_unit}")
            return True, 0.0

        try:
            buy = await self.deriv.buy(symbol=symbol, contract_type=signal.contract_type, stake=stake,
                                       duration=signal.duration, duration_unit=signal.duration_unit,
                                       currency=self.cfg.currency)
        except DerivError as exc:
            await self.notify(f"⚠️ {label} buy failed ({stake:.2f}): {exc}\n"
                              f"Check your Deriv account – if the connection dropped the trade may still exist.")
            return None, 0.0

        await self.notify(f"🚀 {label}: {signal.direction_label} {symbol} stake {stake:.2f} "
                          f"(contract {buy['contract_id']})")
        poc = await self.deriv.wait_for_result(buy["contract_id"], signal.duration_seconds)
        profit = float(poc.get("profit", 0))
        won = poc.get("status") == "won" or profit > 0

        self.stats.roll()
        self.stats.profit += profit
        if won:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
            await self.notify(f"🔻 {label} lost {profit:.2f}")
        log.info("%s %s stake=%.2f profit=%.2f", label, symbol, stake, profit)
        return won, profit
