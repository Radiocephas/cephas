"""Records how every signal actually played out, using Deriv tick history."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .deriv import DerivClient
from .parser import Signal
from .stats import JOURNAL, append_journal

log = logging.getLogger(__name__)

TRACK_CANDLES = 4   # entry + 3 recoveries, so replays can test up to 3 martingale steps


def candle_outcomes(ticks: list[tuple[int, float]], start: int, duration: int,
                    contract_type: str, candles: int) -> list[bool]:
    """Win/loss of consecutive Rise/Fall contracts starting at `start`.

    Like Deriv: the entry spot is the first tick after the start time, the exit spot is the
    tick at (or just before) expiry, and a tie counts as a loss.
    """
    out: list[bool] = []
    for i in range(candles):
        s, e = start + i * duration, start + (i + 1) * duration
        entry = next((p for t, p in ticks if t > s), None)
        exit_ = next((p for t, p in reversed(ticks) if t <= e), None)
        if entry is None or exit_ is None or not ticks or ticks[-1][0] < e:
            break   # not enough data
        out.append(exit_ > entry if contract_type == "CALL" else exit_ < entry)
    return out


async def track_signal(deriv: DerivClient, signal: Signal, entry: datetime, *, traded: bool,
                       default_payout: float, currency: str = "USD",
                       path: str = JOURNAL) -> dict | None:
    try:
        symbol = deriv.resolve_symbol(signal.market)
    except Exception as exc:
        log.warning("Not tracking %s: %s", signal.market, exc)
        return None

    try:
        payout = await deriv.payout_rate(symbol=symbol, contract_type=signal.contract_type,
                                         duration=signal.duration, duration_unit=signal.duration_unit,
                                         currency=currency)
    except Exception as exc:
        log.warning("Could not fetch payout, using default %.2f: %s", default_payout, exc)
        payout = default_payout

    dur = signal.duration_seconds
    end = entry + timedelta(seconds=dur * TRACK_CANDLES + 5)
    await asyncio.sleep(max(0.0, (end - datetime.now(timezone.utc)).total_seconds()))

    start = int(entry.timestamp())
    try:
        ticks = await deriv.ticks_between(symbol, start - 5, start + dur * TRACK_CANDLES + 5)
    except Exception as exc:
        log.warning("Could not fetch ticks for %s: %s", symbol, exc)
        return None

    record = {
        "entry": entry.isoformat(),
        "market": signal.market,
        "symbol": symbol,
        "direction": signal.direction_label,
        "duration_s": dur,
        "payout": round(payout, 4),
        "traded": traded,
        "candles": candle_outcomes(ticks, start, dur, signal.contract_type, TRACK_CANDLES),
    }
    append_journal(record, path)
    log.info("Tracked %s", record)
    return record
