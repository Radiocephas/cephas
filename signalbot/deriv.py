"""Minimal async client for the Deriv WebSocket API (https://api.deriv.com)."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re

import websockets

log = logging.getLogger(__name__)

# Fallback mapping of display names -> Deriv symbols. At start-up the client also
# loads `active_symbols`, so any market Deriv offers can be matched by name.
STATIC_SYMBOLS = {
    "volatility 10 index": "R_10",
    "volatility 25 index": "R_25",
    "volatility 50 index": "R_50",
    "volatility 75 index": "R_75",
    "volatility 100 index": "R_100",
    "volatility 10 (1s) index": "1HZ10V",
    "volatility 15 (1s) index": "1HZ15V",
    "volatility 25 (1s) index": "1HZ25V",
    "volatility 30 (1s) index": "1HZ30V",
    "volatility 50 (1s) index": "1HZ50V",
    "volatility 75 (1s) index": "1HZ75V",
    "volatility 90 (1s) index": "1HZ90V",
    "volatility 100 (1s) index": "1HZ100V",
    "volatility 150 (1s) index": "1HZ150V",
    "volatility 250 (1s) index": "1HZ250V",
}


def normalize_market(name: str) -> str:
    name = name.lower().replace("™", "")
    name = re.sub(r"[^a-z0-9() ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\(\s*1\s*s\s*\)", "(1s)", name)
    if not name.endswith("index") and name.startswith("volatility"):
        name += " index"
    return name


class DerivError(Exception):
    pass


class DerivClient:
    def __init__(self, token: str, app_id: str, url: str):
        self._token = token
        self._url = f"{url}?app_id={app_id}"
        self._ws = None
        self._req_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self.symbols: dict[str, str] = dict(STATIC_SYMBOLS)
        self.account: dict = {}

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        async with self._connect_lock:
            if self._ws is not None and self._reader and not self._reader.done():
                return
            log.info("Connecting to Deriv…")
            self._ws = await websockets.connect(self._url, ping_interval=20, ping_timeout=20)
            self._reader = asyncio.create_task(self._read_loop())
            if self._keepalive is None or self._keepalive.done():
                self._keepalive = asyncio.create_task(self._keepalive_loop())
        auth = await self.request({"authorize": self._token})
        self.account = auth["authorize"]
        log.info("Authorized as %s (%s) balance %s %s", self.account.get("loginid"),
                 "DEMO" if self.account.get("is_virtual") else "REAL",
                 self.account.get("balance"), self.account.get("currency"))
        await self._load_symbols()

    async def close(self) -> None:
        for task in (self._keepalive, self._reader):
            if task:
                task.cancel()
        if self._ws:
            await self._ws.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                fut = self._pending.pop(msg.get("req_id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        except websockets.ConnectionClosed as exc:
            log.warning("Deriv connection closed: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DerivError("connection lost"))
            self._pending.clear()

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.request({"ping": 1})
            except Exception as exc:  # reconnect handled by request()
                log.warning("Keep-alive failed: %s", exc)

    async def request(self, payload: dict, timeout: float = 30, _retry: bool = True) -> dict:
        if self._ws is None or self._reader is None or self._reader.done():
            await self.connect()
        req_id = next(self._req_ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({**payload, "req_id": req_id}))
            msg = await asyncio.wait_for(fut, timeout)
        except (websockets.ConnectionClosed, DerivError):
            self._pending.pop(req_id, None)
            # Never blindly resend a buy: it could open a duplicate position.
            if _retry and "buy" not in payload:
                await self.connect()
                return await self.request(payload, timeout, _retry=False)
            raise
        finally:
            self._pending.pop(req_id, None)
        if "error" in msg:
            raise DerivError(f"{msg['error'].get('code')}: {msg['error'].get('message')}")
        return msg

    # ------------------------------------------------------------------ helpers
    async def _load_symbols(self) -> None:
        try:
            resp = await self.request({"active_symbols": "brief"})
        except DerivError as exc:
            log.warning("Could not load active symbols, using static list: %s", exc)
            return
        for s in resp.get("active_symbols", []):
            self.symbols[normalize_market(s["display_name"])] = s["symbol"]

    def resolve_symbol(self, market: str) -> str:
        if market.upper() in self.symbols.values():   # already a symbol, e.g. "1HZ75V"
            return market.upper()
        key = normalize_market(market)
        if key in self.symbols:
            return self.symbols[key]
        raise DerivError(f"Unknown market '{market}'")

    async def balance(self) -> tuple[float, str]:
        resp = await self.request({"balance": 1})
        return float(resp["balance"]["balance"]), resp["balance"]["currency"]

    async def buy(self, *, symbol: str, contract_type: str, stake: float, duration: int,
                  duration_unit: str, currency: str) -> dict:
        resp = await self.request({
            "buy": 1,
            "price": round(stake, 2),
            "parameters": {
                "amount": round(stake, 2),
                "basis": "stake",
                "contract_type": contract_type,
                "currency": currency,
                "duration": duration,
                "duration_unit": duration_unit,
                "symbol": symbol,
            },
        })
        return resp["buy"]

    async def ticks_between(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        """Return (epoch, price) ticks with start <= epoch <= end."""
        resp = await self.request({"ticks_history": symbol, "start": start, "end": end,
                                   "style": "ticks", "count": 5000, "adjust_start_time": 0})
        hist = resp["history"]
        return list(zip(hist["times"], map(float, hist["prices"])))

    async def payout_rate(self, *, symbol: str, contract_type: str, duration: int,
                          duration_unit: str, currency: str) -> float:
        """Profit per 1 unit staked if the contract wins (e.g. 0.95)."""
        resp = await self.request({
            "proposal": 1, "amount": 10, "basis": "stake", "contract_type": contract_type,
            "currency": currency, "duration": duration, "duration_unit": duration_unit,
            "symbol": symbol,
        })
        return float(resp["proposal"]["payout"]) / 10 - 1

    async def wait_for_result(self, contract_id: int, expected_seconds: float) -> dict:
        """Sleep until expiry, then poll until Deriv has settled the contract."""
        await asyncio.sleep(max(0.0, expected_seconds))
        while True:
            try:
                resp = await self.request({"proposal_open_contract": 1, "contract_id": contract_id})
                poc = resp["proposal_open_contract"]
                if poc.get("is_sold"):
                    return poc
            except DerivError as exc:
                log.warning("Polling contract %s failed: %s", contract_id, exc)
            await asyncio.sleep(1)
