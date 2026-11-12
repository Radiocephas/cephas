"""Parse trading signals posted in the Telegram group.

Expected format (emojis and spacing are optional):

    Entry confirmed ✅
    🌎 Market: Volatility 75 (1s) Index
    ⏳ Duration: 5min
    📊 Direction: 🟢 RISE
    ⏰ Entry Time (UTC): 11:30
    👉 On loss, use martingale on the next candle, max 3 times
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_MARKET_RE = re.compile(r"market\s*:\s*(?P<v>[^\n\r]+)", re.I)
_DURATION_RE = re.compile(
    r"duration\s*:\s*(?P<n>\d+)\s*(?P<u>s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hours?|t|ticks?)\b",
    re.I,
)
_DIRECTION_RE = re.compile(r"direction\s*:\s*[^A-Za-z\n]*(?P<v>[A-Za-z]+)", re.I)
_ENTRY_RE = re.compile(r"entry\s*time[^:\n]*:\s*(?P<h>\d{1,2})[:.h](?P<m>\d{2})", re.I)
_MARTINGALE_RE = re.compile(r"martingale.*?max\s*(?P<n>\d+)", re.I | re.S)

_DIRECTIONS = {
    "RISE": "CALL", "CALL": "CALL", "UP": "CALL", "BUY": "CALL", "HIGHER": "CALL",
    "FALL": "PUT", "PUT": "PUT", "DOWN": "PUT", "SELL": "PUT", "LOWER": "PUT",
}

_UNITS = {"s": "s", "m": "m", "h": "h", "t": "t"}


@dataclass(frozen=True)
class Signal:
    market: str                 # human name, e.g. "Volatility 75 (1s) Index"
    contract_type: str          # "CALL" (rise) or "PUT" (fall)
    duration: int               # e.g. 5
    duration_unit: str          # "s", "m", "h" or "t"
    entry_hour: int             # UTC
    entry_minute: int           # UTC
    martingale_steps: int | None  # max recovery steps announced in the signal

    @property
    def duration_seconds(self) -> int:
        return self.duration * {"s": 1, "m": 60, "h": 3600, "t": 2}[self.duration_unit]

    @property
    def direction_label(self) -> str:
        return "RISE" if self.contract_type == "CALL" else "FALL"

    def entry_datetime(self, now: datetime | None = None) -> datetime:
        """Resolve HH:MM (UTC) to the nearest matching datetime around `now`."""
        now = now or datetime.now(timezone.utc)
        candidate = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        # Signals are posted shortly before entry; handle midnight roll-over.
        if candidate < now - timedelta(hours=12):
            candidate += timedelta(days=1)
        elif candidate > now + timedelta(hours=12):
            candidate -= timedelta(days=1)
        return candidate

    def summary(self) -> str:
        mg = f", martingale max {self.martingale_steps}" if self.martingale_steps is not None else ""
        return (f"{self.market} | {self.direction_label} | {self.duration}{self.duration_unit} | "
                f"entry {self.entry_hour:02d}:{self.entry_minute:02d} UTC{mg}")


def parse_signal(text: str) -> Signal | None:
    """Return a Signal if `text` looks like an entry signal, otherwise None."""
    if not text:
        return None
    market = _MARKET_RE.search(text)
    duration = _DURATION_RE.search(text)
    direction = _DIRECTION_RE.search(text)
    entry = _ENTRY_RE.search(text)
    if not (market and duration and direction and entry):
        return None

    contract_type = _DIRECTIONS.get(direction.group("v").upper())
    if contract_type is None:
        return None

    hour, minute = int(entry.group("h")), int(entry.group("m"))
    if hour > 23 or minute > 59:
        return None

    mg = _MARTINGALE_RE.search(text)
    return Signal(
        market=market.group("v").strip(),
        contract_type=contract_type,
        duration=int(duration.group("n")),
        duration_unit=_UNITS[duration.group("u")[0].lower()],
        entry_hour=hour,
        entry_minute=minute,
        martingale_steps=int(mg.group("n")) if mg else None,
    )
