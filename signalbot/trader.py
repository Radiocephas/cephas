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
from .stats import JOURNAL, edge, load_journal
from .tracker import track_signal

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
    lost_sequences: int = 0

    def roll(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.profit, self.wins, self.losses, self.lost_sequences = today, 0.0, 0, 0, 0


class Trader:
    def __init__(self, cfg: Config, deriv: DerivClient, notify: Notify, journal: str = JOURNAL):
        self.cfg = cfg
        self.journal = journal
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

        gate = self.limits_reason() or self.edge_gate_reason()
        live = gate is None
        # Every signal is recorded, traded or not, so the group's real win rate is known.
        asyncio.create_task(track_signal(self.deriv, signal, entry, traded=live,
                                         default_payout=self.cfg.payout_rate,
                                         currency=self.cfg.currency, path=self.journal))
        if not live:
            return f"paper-tracked only – {gate}"

        task = asyncio.create_task(self._run(signal, entry, key))
        self.active[key] = task
        task.add_done_callback(lambda _t: self.active.pop(key, None))
        return None

    def edge_gate_reason(self) -> str | None:
        """Why real-money trading is currently blocked by the group's track record, if it is."""
        if not self.cfg.edge_gate:
            return None
        e = edge(load_journal(self.journal), self.cfg.payout_rate, last=self.cfg.edge_window)
        if e.signals < self.cfg.edge_min_signals:
            return f"collecting track record ({e.signals}/{self.cfg.edge_min_signals} signals)"
        needed = e.breakeven + self.cfg.edge_margin
        if e.win_rate < needed:
            return (f"group win rate {e.win_rate:.1%} over last {e.signals} signals is below "
                    f"the {needed:.1%} needed to profit")
        return None

    async def base_stake(self) -> float:
        if self.cfg.stake_percent > 0:
            try:
                balance, _ = await self.deriv.balance()
                return max(0.35, round(balance * self.cfg.stake_percent / 100, 2))
            except Exception as exc:
                log.warning("Balance unavailable, using BASE_STAKE: %s", exc)
        return self.cfg.base_stake

    def limits_reason(self) -> str | None:
        self.stats.roll()
        if self.paused:
            return "bot is paused (/resume to continue)"
        if self.cfg.daily_loss_limit > 0 and self.stats.profit <= -self.cfg.daily_loss_limit:
            return f"daily loss limit reached ({self.stats.profit:.2f})"
        if 0 < self.cfg.max_losing_sequences <= self.stats.lost_sequences:
            return f"{self.stats.lost_sequences} fully lost signals today – stopped until tomorrow (UTC)"
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

        base = await self.base_stake()
        await self.notify(f"📥 Signal accepted: {signal.summary()}\n"
                          f"Symbol {symbol}, base stake {base:.2f}, up to {steps} recoveries")

        sequence_profit = 0.0
        for step in range(steps + 1):
            reason = self.limits_reason()
            if reason:
                await self.notify(f"⛔ Not trading step {step}: {reason}")
                return

            stake = martingale_stake(base, self.cfg.martingale_multiplier, step)
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

        self.stats.lost_sequences += 1
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
