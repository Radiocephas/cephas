import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from signalbot.deriv import STATIC_SYMBOLS, normalize_market
from signalbot.parser import parse_signal
from signalbot.stats import breakeven, edge, sequence_profit, simulate, wilson_interval
from signalbot.tracker import candle_outcomes
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
    assert STATIC_SYMBOLS[normalize_market("Jump 50 Index")] == "JD50"
    assert STATIC_SYMBOLS[normalize_market("Bear Market Index")] == "RDBEAR"


def test_group_messages_with_forward_headers():
    msg = """ https://t.me/atwsignals/9201 ATW Signals (Free), [Nov 12, 2026 at 14:56]
✅ Entry confirmed ✅
🌎 Market: Jump 75 Index
⏳ Duration: 5min
📊 Direction: 🔴 FALL
⏰ Entry Time (UTC): 12:30
👉 On loss, use martingale on the next candle, max 3 times"""
    s = parse_signal(msg)
    assert (s.market, s.contract_type, s.entry_hour, s.entry_minute) == ("Jump 75 Index", "PUT", 12, 30)
    for other in ("GAIN Martingale 1 ✅", "LOSS 😔", "End of trading session",
                  "📊 Session Report Session 2\n\n16:50 Jump 50 Index ❌\n✅ Wins: 19"):
        assert parse_signal(other) is None


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
                dry_run=False, currency="USD", stake_percent=0, max_losing_sequences=0,
                edge_gate=False, edge_min_signals=50, edge_window=100, edge_margin=0.03,
                payout_rate=0.95)
    base.update(over)
    return SimpleNamespace(**base)


def _run_sequence(losses, trader_setup=None, **cfg):
    deriv = FakeDeriv(losses)
    msgs = []

    async def notify(t):
        msgs.append(t)

    async def go():
        trader = Trader(_cfg(**cfg), deriv, notify)
        trader._wait_until = lambda when: asyncio.sleep(0, result=True)  # no real waiting
        if trader_setup:
            trader_setup(trader)
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


def test_losing_sequence_limit():
    def setup(t):
        t.stats.lost_sequences = 2
    deriv, trader, msgs = _run_sequence(losses=0, trader_setup=setup, max_losing_sequences=2)
    assert deriv.stakes == []
    assert "fully lost signals" in msgs[-1]


def test_candle_outcomes_rise():
    # 1-second ticks, price goes up for 300s, then down for 300s
    ticks = [(t, 100 + t) for t in range(0, 301)] + [(t, 700 - t) for t in range(301, 610)]
    assert candle_outcomes(ticks, 0, 300, "CALL", 4) == [True, False]   # no data for 3rd candle
    assert candle_outcomes(ticks, 0, 300, "PUT", 2) == [False, True]


def test_breakeven_and_sequence_profit():
    assert round(breakeven(0.95), 3) == 0.513
    assert sequence_profit([True], 0.95, 3, 2.2) == 0.95
    assert round(sequence_profit([False, False, True], 0.95, 3, 2.2), 3) == round(4.84 * 0.95 - 3.2, 3)
    assert round(sequence_profit([False] * 4, 0.95, 3, 2.2), 2) == -18.69
    assert sequence_profit([False, True], 0.95, 0, 2.2) == -1


def test_edge_and_simulation():
    recs = [{"candles": [True], "payout": 0.95}] * 60 + [{"candles": [False, True], "payout": 0.95}] * 40
    e = edge(recs, 0.95)
    assert e.win_rate == 0.6 and e.low < e.breakeven      # 60/100 is not yet proof
    assert "POSSIBLE EDGE" in e.verdict
    strong = [{"candles": [True], "payout": 0.95}] * 140 + [{"candles": [False]}] * 60
    assert "PROFITABLE" in edge(strong, 0.95).verdict    # 70% over 200 is
    flat = simulate(recs, 0, 2.2, 0.95)
    assert round(flat.profit, 2) == round(60 * 0.95 - 40, 2)
    lo, hi = wilson_interval(30, 50)
    assert lo < 0.6 < hi


def test_edge_gate_blocks_until_proven(tmp_path):
    journal = tmp_path / "j.jsonl"
    trader = Trader(_cfg(edge_gate=True, edge_min_signals=50), FakeDeriv(0), None, journal=str(journal))
    assert "collecting track record (0/50" in trader.edge_gate_reason()
    import json
    with open(journal, "w") as fh:
        for i in range(100):
            fh.write(json.dumps({"candles": [i % 2 == 0], "payout": 0.95}) + "\n")  # 50% win rate
    assert "below" in trader.edge_gate_reason()
    with open(journal, "w") as fh:
        for i in range(100):
            fh.write(json.dumps({"candles": [i % 5 != 0], "payout": 0.95}) + "\n")  # 80% win rate
    assert trader.edge_gate_reason() is None


def _walk(n, seed, revert=0.0):
    import numpy as np
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = x[i - 1] * (1 - revert) + rng.normal()
    return 1000 + x


def test_backtest_finds_nothing_in_random_walk():
    from signalbot.backtest import build_strategies, evaluate_symbol, report
    res = evaluate_symbol("RANDOM", _walk(60_000, 1), 0.95, build_strategies())
    assert "None passed the luck-corrected test" in report(res, 42)


def test_backtest_detects_a_real_edge():
    # strongly mean-reverting series: "reversal"/"revert" strategies genuinely win
    from signalbot.backtest import build_strategies, evaluate_symbol, report
    res = evaluate_symbol("PLANTED", _walk(60_000, 2, revert=0.05), 0.95, build_strategies())
    text = report(res, 42)
    assert "passed the luck-corrected test" in text and "None passed" not in text


def test_find_ws_url_in_otp_response():
    from signalbot.deriv import _find_ws_url
    resp = {"data": {"otp": "abc", "url": "wss://api.derivws.com/trading/v1/options/ws/demo?otp=abc"}}
    assert _find_ws_url(resp).endswith("ws/demo?otp=abc")
    assert _find_ws_url({"data": {"otp": "abc"}}) is None


def test_client_matches_replies_with_and_without_req_id():
    import json as _json
    import websockets
    from signalbot.deriv import DerivClient

    async def handler(ws):
        async for raw in ws:
            msg = _json.loads(raw)
            if "active_symbols" in msg:
                await ws.send(_json.dumps({"msg_type": "active_symbols", "req_id": msg["req_id"],
                                           "active_symbols": [{"symbol": "R_75", "display_name": "Volatility 75 Index"}]}))
            elif "ticks_history" in msg:   # reply WITHOUT req_id
                await ws.send(_json.dumps({"msg_type": "history",
                                           "history": {"times": [1, 2], "prices": ["1.5", "2.5"]}}))

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = DerivClient(public_url=f"ws://127.0.0.1:{port}")
            import signalbot.deriv as d
            orig = d.websockets.connect
            d.websockets.connect = lambda url, ssl=None, **kw: orig(url, **kw)   # plain ws for the test
            try:
                await c.connect()
                assert c.resolve_symbol("Volatility 75 Index") == "R_75"
                ticks = await asyncio.wait_for(c.ticks_between("R_75", 0, 10), 5)
            finally:
                d.websockets.connect = orig
                await c.close()
            return ticks

    assert asyncio.run(go()) == [(1, 1.5), (2, 2.5)]


def test_candles_pagination_with_capped_pages():
    from signalbot.deriv import DerivClient

    class Capped(DerivClient):
        async def request(self, payload, timeout=30, _retry=True):
            assert "adjust_start_time" not in payload
            lo, hi = max(payload["start"], 6000), payload["end"]   # data exists from t=6000
            epochs = list(range(lo - lo % 60, hi + 1, 60))[-1000:]   # server caps at 1000
            return {"candles": [{"epoch": e, "close": e / 60} for e in epochs if e >= lo]}

    got = asyncio.run(Capped().candles("R_75", 0, 600_000))
    epochs = [c["epoch"] for c in got]
    assert epochs == sorted(set(epochs)) and epochs[0] == 6000 and epochs[-1] == 600_000
    assert len(epochs) == (600_000 - 6000) // 60 + 1
