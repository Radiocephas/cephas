"""Honest strategy tester for 5-minute Rise/Fall on Deriv synthetic indices.

Downloads 1-minute candles from Deriv, runs a library of common strategies, and checks them
the way a real edge must be checked:

* every strategy is scored on data it was NOT chosen on (walk-forward: pick on the first 60%,
  test on the last 40%);
* results are compared to the break-even win rate (1 / (1 + payout));
* significance is corrected for the number of strategies tried, because testing hundreds of
  rules always produces a few lucky "winners".

    python -m signalbot.backtest --days 90                 # all default markets
    python -m signalbot.backtest --days 30 --symbols R_75 1HZ75V
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from math import erf, sqrt
from typing import Callable

import numpy as np

from .deriv import DerivClient

log = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100",
                   "1HZ10V", "1HZ25V", "1HZ30V", "1HZ50V", "1HZ75V", "1HZ100V",
                   "JD50", "JD75", "RDBEAR", "RDBULL"]
HORIZON = 5        # minutes per contract
WARMUP = 120       # bars needed before indicators are valid
SPLIT = 0.6        # fraction of data used to choose the strategy
MIN_TRADES = 200   # ignore strategies that rarely fire


# ----------------------------------------------------------------------------- indicators
def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def _sma(a: np.ndarray, n: int) -> np.ndarray:
    c = np.cumsum(np.insert(a, 0, 0.0))
    out = np.full_like(a, np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _std(a: np.ndarray, n: int) -> np.ndarray:
    m = _sma(a, n)
    m2 = _sma(a * a, n)
    return np.sqrt(np.maximum(m2 - m * m, 0))


def _rsi(a: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(a, prepend=a[0])
    up, dn = np.maximum(d, 0), np.maximum(-d, 0)
    au, ad = np.zeros_like(a), np.zeros_like(a)
    au[n], ad[n] = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n + 1, len(a)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    rs = np.divide(au, ad, out=np.full_like(a, np.inf), where=ad > 0)
    out = 100 - 100 / (1 + rs)
    out[:n + 1] = np.nan
    return out


def _streak(a: np.ndarray) -> np.ndarray:
    """+k after k rising 1-min candles in a row, -k after k falling ones."""
    d = np.sign(np.diff(a, prepend=a[0]))
    out = np.zeros_like(a)
    for i in range(1, len(a)):
        out[i] = out[i - 1] + d[i] if d[i] != 0 and np.sign(out[i - 1]) == d[i] else d[i]
    return out


# ----------------------------------------------------------------------------- strategies
Strategy = Callable[[dict], np.ndarray]   # returns +1 (rise), -1 (fall) or 0 (no trade) per bar


def build_strategies() -> dict[str, Strategy]:
    s: dict[str, Strategy] = {
        "always rise": lambda f: np.ones_like(f["c"]),
        "always fall": lambda f: -np.ones_like(f["c"]),
    }
    for k in (1, 3, 5, 10, 15, 30, 60):
        s[f"momentum {k}m"] = lambda f, k=k: np.sign(f["c"] - _shift(f["c"], k))
        s[f"reversal {k}m"] = lambda f, k=k: -np.sign(f["c"] - _shift(f["c"], k))
    for fast, slow in ((5, 20), (10, 30), (20, 60), (50, 100)):
        s[f"MA trend {fast}/{slow}"] = lambda f, a=fast, b=slow: np.sign(_sma(f["c"], a) - _sma(f["c"], b))
        s[f"MA revert {fast}/{slow}"] = lambda f, a=fast, b=slow: -np.sign(_sma(f["c"], a) - _sma(f["c"], b))
    for lo, hi in ((30, 70), (20, 80)):
        s[f"RSI revert {lo}/{hi}"] = lambda f, lo=lo, hi=hi: np.where(f["rsi"] < lo, 1, np.where(f["rsi"] > hi, -1, 0))
        s[f"RSI trend {lo}/{hi}"] = lambda f, lo=lo, hi=hi: np.where(f["rsi"] > hi, 1, np.where(f["rsi"] < lo, -1, 0))
    for z in (1.5, 2.0, 2.5):
        s[f"Bollinger revert z{z}"] = lambda f, z=z: np.where(f["z"] < -z, 1, np.where(f["z"] > z, -1, 0))
        s[f"Bollinger breakout z{z}"] = lambda f, z=z: np.where(f["z"] > z, 1, np.where(f["z"] < -z, -1, 0))
    for n in (3, 4, 5, 6, 7):
        s[f"after {n}-candle streak: reverse"] = lambda f, n=n: np.where(np.abs(f["streak"]) >= n, -np.sign(f["streak"]), 0)
        s[f"after {n}-candle streak: follow"] = lambda f, n=n: np.where(np.abs(f["streak"]) >= n, np.sign(f["streak"]), 0)
    return s


def features(closes: np.ndarray) -> dict:
    sd = _std(closes, 20)
    return {
        "c": closes,
        "rsi": _rsi(closes, 14),
        "z": np.divide(closes - _sma(closes, 20), sd, out=np.zeros_like(closes), where=sd > 0),
        "streak": _streak(closes),
    }


# ----------------------------------------------------------------------------- evaluation
def norm_sf(x: float) -> float:
    return 0.5 * (1 - erf(x / sqrt(2)))


@dataclass
class Score:
    trades: int
    wins: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    def profit(self, payout: float) -> float:   # in stake units
        return self.wins * payout - (self.trades - self.wins)

    def p_value(self, breakeven: float) -> float:
        """Chance of doing at least this well with no edge at all."""
        if not self.trades:
            return 1.0
        sd = sqrt(breakeven * (1 - breakeven) / self.trades)
        return norm_sf((self.win_rate - breakeven) / sd)


def score(direction: np.ndarray, closes: np.ndarray, lo: int, hi: int) -> Score:
    """Trade every HORIZON minutes (non-overlapping) between bars lo and hi."""
    idx = np.arange(max(lo, WARMUP), min(hi, len(closes) - HORIZON), HORIZON)
    d = np.nan_to_num(direction[idx]).astype(int)
    move = np.sign(closes[idx + HORIZON] - closes[idx])
    taken = d != 0
    wins = int(np.sum((d == move) & taken & (move != 0)))   # ties lose, as on Deriv
    return Score(int(taken.sum()), wins)


@dataclass
class Result:
    symbol: str
    strategy: str
    payout: float
    train: Score
    test: Score


def evaluate_symbol(symbol: str, closes: np.ndarray, payout: float,
                    strategies: dict[str, Strategy]) -> list[Result]:
    f = features(closes)
    split = int(len(closes) * SPLIT)
    out = []
    for name, strat in strategies.items():
        d = strat(f)
        out.append(Result(symbol, name, payout, score(d, closes, 0, split), score(d, closes, split, len(closes))))
    return out


def report(results: list[Result], days: int) -> str:
    n_tests = len(results)
    lines = [f"# Strategy test: 5-minute Rise/Fall, {days} days of 1-minute data",
             "",
             f"{len({r.symbol for r in results})} markets × {len({r.strategy for r in results})} strategies "
             f"= {n_tests} combinations. Chosen on the first {SPLIT:.0%} of the data, "
             f"tested on the last {1 - SPLIT:.0%}.",
             ""]

    # 1. what a trader would do: pick the best-looking strategy per market, then trade it
    lines += ["## Best strategy per market (picked on past data, then tested on new data)", "",
              "| Market | Payout | Break-even | Picked strategy | Win rate when picked | Win rate on new data | New-data trades | Profit per 100 trades |",
              "|---|---|---|---|---|---|---|---|"]
    total_test = Score(0, 0)
    total_profit = 0.0
    for sym in sorted({r.symbol for r in results}):
        rs = [r for r in results if r.symbol == sym and r.train.trades >= MIN_TRADES]
        if not rs:
            continue
        best = max(rs, key=lambda r: r.train.win_rate)
        be = 1 / (1 + best.payout)
        per100 = best.test.profit(best.payout) / best.test.trades * 100 if best.test.trades else 0
        total_test = Score(total_test.trades + best.test.trades, total_test.wins + best.test.wins)
        total_profit += best.test.profit(best.payout)
        lines.append(f"| {sym} | {best.payout:.1%} | {be:.1%} | {best.strategy} | {best.train.win_rate:.1%} "
                     f"| **{best.test.win_rate:.1%}** | {best.test.trades} | {per100:+.1f} stakes |")
    lines += ["", f"All picked strategies together on new data: **{total_test.win_rate:.2%}** win rate over "
              f"{total_test.trades} trades, profit **{total_profit:+.1f} stakes**.", ""]

    # 2. anything that beats break-even on new data after correcting for luck
    lines += ["## Strategies that beat break-even on new data", ""]
    sig = []
    for r in results:
        be = 1 / (1 + r.payout)
        if r.test.trades >= MIN_TRADES // 2 and r.test.win_rate > be:
            p = r.test.p_value(be)
            sig.append((p, r))
    sig.sort(key=lambda x: x[0])
    alpha = 0.05 / n_tests
    real = [(p, r) for p, r in sig if p < alpha and r.train.win_rate > 1 / (1 + r.payout)]
    lines.append(f"{len(sig)} of {n_tests} combinations were above break-even on new data. With no edge at all, "
                 f"about half the near-50% strategies land above or below by chance, so this alone means nothing.")
    lines.append("")
    if real:
        lines.append(f"**{len(real)} passed the luck-corrected test** (p < {alpha:.1e}, and above break-even "
                     "on BOTH halves):")
        lines.append("")
        for p, r in real:
            lines.append(f"- {r.symbol} – {r.strategy}: picked {r.train.win_rate:.1%} ({r.train.trades}), "
                         f"new data {r.test.win_rate:.1%} ({r.test.trades}), p={p:.1e}")
    else:
        lines.append("**None passed the luck-corrected test.** No strategy showed an edge that could not be "
                     "explained by chance.")
    lines += ["", "Top 10 on new data (for reference; most are expected to be luck):", ""]
    for p, r in sig[:10]:
        lines.append(f"- {r.symbol} – {r.strategy}: picked {r.train.win_rate:.1%}, new data "
                     f"{r.test.win_rate:.1%} ({r.test.trades} trades), p={p:.3f}")

    # 3. overall distribution
    rates = [r.test.win_rate for r in results if r.test.trades >= MIN_TRADES // 2]
    if rates:
        lines += ["", "## All strategies on new data", "",
                  f"Average win rate {np.mean(rates):.2%}, median {np.median(rates):.2%}, "
                  f"range {min(rates):.1%}–{max(rates):.1%}."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- data
async def fetch(symbols: list[str], days: int, cache_dir: str) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    deriv = DerivClient(None, os.getenv("DERIV_APP_ID") or None)
    await deriv.connect()
    available = set(deriv.symbols.values())
    end = int(time.time()) // 60 * 60
    start = end - days * 86400
    data, payouts = {}, {}
    os.makedirs(cache_dir, exist_ok=True)
    for sym in symbols:
        if available and sym not in available:
            log.warning("%s not offered by Deriv, skipping", sym)
            continue
        path = os.path.join(cache_dir, f"{sym}_{days}d.npy")
        if os.path.exists(path):
            data[sym] = np.load(path)
        else:
            log.info("Downloading %s…", sym)
            candles = await deriv.candles(sym, start, end)
            if not candles:
                log.warning("%s: no candles returned, skipping", sym)
                continue
            data[sym] = np.array([float(c.get("close", c.get("c"))) for c in candles])
            np.save(path, data[sym])
        log.info("%s: %d minutes of data", sym, len(data[sym]))
        try:
            payouts[sym] = await deriv.payout_rate(symbol=sym, contract_type="CALL", duration=HORIZON,
                                                   duration_unit="m", currency="USD")
        except Exception as exc:
            log.warning("No payout quote for %s (%s); assuming 95%%", sym, exc)
            payouts[sym] = 0.95
    await deriv.close()
    return data, payouts


async def probe(symbol: str) -> None:
    """Print Deriv's raw replies so API format changes are easy to diagnose."""
    deriv = DerivClient(None, os.getenv("DERIV_APP_ID") or None)
    deriv._load_symbols = lambda: asyncio.sleep(0)   # don't parse anything, just connect
    await deriv.connect()
    end = int(time.time()) // 60 * 60
    requests = [
        {"active_symbols": "brief"},
        {"ticks_history": symbol, "style": "candles", "granularity": 60, "start": end - 300,
         "end": end, "count": 3},
        {"ticks_history": symbol, "style": "ticks", "end": "latest", "count": 3},
        {"proposal": 1, "amount": 10, "basis": "stake", "contract_type": "CALL", "currency": "USD",
         "duration": 5, "duration_unit": "m", "underlying_symbol": symbol},
    ]
    for req in requests:
        print("\n>>>", json.dumps(req))
        try:
            resp = await deriv.request(req, _retry=False, timeout=20)
            for key in ("active_symbols",):
                if isinstance(resp.get(key), list):
                    resp[key] = resp[key][:2] + [f"... {len(resp[key])} total"]
            print(json.dumps(resp, indent=1)[:1500])
        except Exception as exc:
            print("ERROR:", exc)
    await deriv.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--cache", default="backtest_data")
    ap.add_argument("--out", default="backtest_report.md")
    ap.add_argument("--probe", action="store_true", help="print Deriv's raw replies and exit")
    args = ap.parse_args()
    if args.probe:
        asyncio.run(probe(args.symbols[0] if args.symbols != DEFAULT_SYMBOLS else "R_75"))
        return

    data, payouts = asyncio.run(fetch(args.symbols, args.days, args.cache))
    strategies = build_strategies()
    results = []
    for sym, closes in data.items():
        results += evaluate_symbol(sym, closes, payouts[sym], strategies)
    text = report(results, args.days)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(args.out.replace(".md", ".json"), "w", encoding="utf-8") as fh:
        json.dump([{**asdict(r), "train": asdict(r.train), "test": asdict(r.test)} for r in results], fh)
    print(text)


if __name__ == "__main__":
    main()
