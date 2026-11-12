import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from signalbot.deriv import STATIC_SYMBOLS, normalize_market
from signalbot.parser import parse_signal
from signalbot.trader import Trader, martingale_stake

SIGNAL = """Entry confirmed ✅
🌎 Market: Volatility 75 (1s) Index
⏳ Duration: 5min
📊 Direction: 🟢 RISE
⏰ Entry Time (UTC): 11:30
👉 On loss, use martingale on the next candle, max 3 times
1º Recover at 11:35
2º Recover at 11:40
3º Recover if needed"""


def test_parse_example_signal():
    s = parse_signal(SIGNAL)
    assert s is not None
    assert s.market == "Volatility 75 (1s) Index"
    assert s.contract_type == "CALL"
    assert (s.duration, s.duration_unit, s.duration_seconds) == (5, "m", 300)
    assert (s.entry_hour, s.entry_minute) == (11, 30)
    assert s.martingale_steps == 3


def test_parse_fall_and_other_units():
    s = parse_signal(SIGNAL.replace("🟢 RISE", "🔴 FALL").replace("5min", "1 minute"))
    assert s.contract_type == "PUT" and s.duration == 1 and s.duration_unit == "m"


def test_non_signal_ignored():
    assert parse_signal("WIN ✅✅ +95% profit, join VIP!") is None
    assert parse_signal("") is None


def test_market_resolution():
    assert STATIC_SYMBOLS[normalize_market("Volatility 75 (1s) Index")] == "1HZ75V"
    assert STATIC_SYMBOLS[normalize_market("volatility 75 ( 1s )")] == "1HZ75V"
    assert STATIC_SYMBOLS[normalize_market("Volatility 100 Index")] == "R_100"


def test_entry_datetime_midnight_rollover():
    s = parse_signal(SIGNAL.replace("11:30", "00:02"))
    now = datetime(2026, 1, 1, 23, 58, tzinfo=timezone.utc)
    assert s.entry_datetime(now) == datetime(2026, 1, 2, 0, 2, tzinfo=timezone.utc)


def test_martingale_stakes():
    assert [martingale_stake(1, 2.2, n) for n in range(4)] == [1.0, 2.2, 4.84, 10.65]


class FakeDeriv:
    """Loses `losses` times, then wins."""

    def __init__(self, losses):
        self.losses = losses
        self.stakes = []

    def resolve_symbol(self, market):
        return "1HZ75V"

    async def buy(self, **kw):
        self.stakes.append(kw["stake"])
        return {"contract_id": len(self.stakes)}

    async def wait_for_result(self, contract_id, expected_seconds):
        if contract_id <= self.losses:
            return {"status": "lost", "profit": -self.stakes[-1]}
        return {"status": "won", "profit": round(self.stakes[-1] * 0.95, 2)}


def _cfg(**over):
    base = dict(base_stake=1.0, martingale_multiplier=2.2, max_martingale_steps=3, max_stake=50,
                daily_loss_limit=50, daily_profit_target=0, late_entry_tolerance=20,
                dry_run=False, currency="USD")
    base.update(over)
    return SimpleNamespace(**base)


def _run_sequence(losses, **cfg):
    deriv = FakeDeriv(losses)
    msgs = []

    async def notify(t):
        msgs.append(t)

    async def go():
        trader = Trader(_cfg(**cfg), deriv, notify)
        trader._wait_until = lambda when: asyncio.sleep(0, result=True)  # no real waiting
        sig = parse_signal(SIGNAL)
        now = datetime.now(timezone.utc) + timedelta(minutes=1)
        await trader._execute(sig, now)
        return trader

    trader = asyncio.run(go())
    return deriv, trader, msgs


def test_win_after_two_recoveries():
    deriv, trader, msgs = _run_sequence(losses=2)
    assert deriv.stakes == [1.0, 2.2, 4.84]
    assert trader.stats.wins == 1 and trader.stats.losses == 2
    assert round(trader.stats.profit, 2) == round(-1 - 2.2 + 4.6, 2)


def test_stops_after_max_steps():
    deriv, trader, msgs = _run_sequence(losses=10)
    assert len(deriv.stakes) == 4  # entry + 3 recoveries
    assert "Sequence lost" in msgs[-1]


def test_max_stake_caps_sequence():
    deriv, trader, msgs = _run_sequence(losses=10, max_stake=5)
    assert deriv.stakes == [1.0, 2.2, 4.84]
    assert "exceeds MAX_STAKE" in msgs[-1]


def test_daily_loss_limit_stops_sequence():
    deriv, trader, msgs = _run_sequence(losses=10, daily_loss_limit=3)
    assert deriv.stakes == [1.0, 2.2]
    assert "daily loss limit" in msgs[-1]
