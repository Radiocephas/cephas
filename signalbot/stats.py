"""Performance statistics for the signal group, from the journal of tracked signals.

Every signal is recorded whether or not it was traded: for each candle in the martingale window
(entry + recoveries) the journal stores whether the signal's direction would have won,
measured from Deriv's tick history. That makes it possible to measure the group's real win rate
and to replay different money-management settings on the same signals.

    python -m signalbot.stats            # print the report from journal.jsonl
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from math import sqrt

JOURNAL = os.getenv("JOURNAL_FILE", "journal.jsonl")


def load_journal(path: str = JOURNAL) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_journal(record: dict, path: str = JOURNAL) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a win rate."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom, (centre + margin) / denom


def breakeven(payout: float) -> float:
    """Win rate needed to break even when a win pays `payout` per 1 staked."""
    return 1 / (1 + payout)


def sequence_profit(candles: list[bool], payout: float, steps: int, multiplier: float) -> float:
    """P/L (in base-stake units) of one signal traded with `steps` martingale recoveries."""
    lost, stake = 0.0, 1.0
    for won in candles[: steps + 1]:
        if won:
            return stake * payout - lost
        lost += stake
        stake *= multiplier
    return -lost


@dataclass
class Simulation:
    steps: int
    profit: float          # total, in base-stake units
    max_drawdown: float    # worst peak-to-trough, in base-stake units
    worst_sequence: float


def simulate(records: list[dict], steps: int, multiplier: float, default_payout: float) -> Simulation:
    equity = peak = drawdown = 0.0
    worst = 0.0
    for r in records:
        p = sequence_profit(r["candles"], r.get("payout") or default_payout, steps, multiplier)
        worst = min(worst, p)
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Simulation(steps, equity, drawdown, worst)


@dataclass
class EdgeReport:
    signals: int
    entry_wins: int
    win_rate: float
    low: float
    high: float
    breakeven: float
    payout: float

    @property
    def verdict(self) -> str:
        if self.signals < 30:
            return "NOT ENOUGH DATA – keep paper-tracking"
        if self.low > self.breakeven:
            return "PROFITABLE EDGE (statistically significant)"
        if self.win_rate > self.breakeven:
            return "POSSIBLE EDGE – not proven yet, keep stakes minimal"
        return "NO EDGE – these signals lose money"


def edge(records: list[dict], default_payout: float, last: int | None = None) -> EdgeReport:
    recs = [r for r in records if r.get("candles")]
    if last:
        recs = recs[-last:]
    wins = sum(1 for r in recs if r["candles"][0])
    n = len(recs)
    payouts = [r["payout"] for r in recs if r.get("payout")]
    payout = sum(payouts) / len(payouts) if payouts else default_payout
    low, high = wilson_interval(wins, n)
    return EdgeReport(n, wins, wins / n if n else 0.0, low, high, breakeven(payout), payout)


def format_report(records: list[dict], multiplier: float, default_payout: float,
                  base_stake: float, max_steps: int = 3) -> str:
    recs = [r for r in records if r.get("candles")]
    e = edge(recs, default_payout)
    if not recs:
        return "📊 No signals tracked yet. Every signal the group posts is recorded automatically."

    later = [c for r in recs for c in r["candles"][1:2] if not r["candles"][0]]
    lines = [
        f"📊 Signal group report – {e.signals} signals tracked",
        f"Entry win rate: {e.win_rate:.1%} (95% range {e.low:.1%}–{e.high:.1%})",
        f"Break-even win rate: {e.breakeven:.1%} (payout {e.payout:.0%})",
    ]
    if later:
        lines.append(f"Recovery-1 win rate: {sum(later) / len(later):.1%} of {len(later)}")
    lines += [f"Verdict: {e.verdict}", "", f"Replay at base stake {base_stake:g} (multiplier {multiplier:g}):"]
    for steps in range(max_steps + 1):
        s = simulate(recs, steps, multiplier, default_payout)
        name = "flat (no martingale)" if steps == 0 else f"martingale ×{steps}"
        lines.append(f"• {name}: P/L {s.profit * base_stake:+.2f}, "
                     f"max drawdown {s.max_drawdown * base_stake:.2f}, "
                     f"worst signal {s.worst_sequence * base_stake:.2f}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(load_journal(), float(os.getenv("MARTINGALE_MULTIPLIER", "2.2")),
                        float(os.getenv("PAYOUT_RATE", "0.95")), float(os.getenv("BASE_STAKE", "1"))))
