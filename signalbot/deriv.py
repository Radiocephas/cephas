"""Minimal async client for Deriv's Options API (https://developers.deriv.com)."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request

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
    "jump 10 index": "JD10",
    "jump 25 index": "JD25",
    "jump 50 index": "JD50",
    "jump 75 index": "JD75",
    "jump 100 index": "JD100",
    "bear market index": "RDBEAR",
    "bull market index": "RDBULL",
}


def normalize_market(name: str) -> str:
    name = name.lower().replace("™", "")
    name = re.sub(r"[^a-z0-9() ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\(\s*1\s*s\s*\)", "(1s)", name)
    if not name.endswith("index") and name.startswith("volatility"):
        name += " index"
    return name


def _ssl_context() -> ssl.SSLContext:
    """Use certifi's certificates so macOS python.org installs work without extra setup."""
    if os.getenv("SSL_CERT_FILE"):
        return ssl.create_default_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class DerivError(Exception):
    pass


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


# reply msg_type -> request name, where they differ
_REPLY_TYPES = {"history": "ticks_history", "candles": "ticks_history"}

API_BASE = "https://api.derivws.com/trading/v1/options"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


def _rest(method: str, url: str, token: str, app_id: str | None) -> dict:
    """Small blocking REST helper for Deriv's Options API (run it in a thread)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if app_id:
        headers["Deriv-App-ID"] = app_id
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=b"{}" if method == "POST" else None)
    if method == "POST":
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise DerivError(f"{method} {url} -> HTTP {exc.code}: {body}") from None


def _find_ws_url(obj) -> str | None:
    """The OTP response holds a ready-made wss:// URL somewhere; find it."""
    if isinstance(obj, str) and obj.startswith("wss://"):
        return obj
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find_ws_url(v)
            if found:
                return found
    return None


async def list_accounts(token: str, app_id: str | None) -> dict:
    return await asyncio.to_thread(_rest, "GET", f"{API_BASE}/accounts", token, app_id)


class DerivClient:
    """Deriv Options API client.

    * No token: connects to the public, read-only channel (prices, history).
    * With a Personal Access Token + App ID + account ID: gets a one-time password over REST
      and connects to that account's trading channel. Each reconnect fetches a fresh OTP.
    """

    def __init__(self, token: str | None = None, app_id: str | None = None,
                 account_id: str | None = None, public_url: str = PUBLIC_WS):
        self._token = token
        self._app_id = app_id
        self._account_id = account_id
        self._public_url = public_url
        self._ws = None
        self._req_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._types: dict[int, str] = {}
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self.symbols: dict[str, str] = dict(STATIC_SYMBOLS)
        self.account: dict = {}

    async def _ws_url(self) -> str:
        if not self._token:
            return self._public_url + (f"?app_id={self._app_id}" if self._app_id else "")
        if not self._account_id:
            raise DerivError("DERIV_ACCOUNT_ID is not set – run `python -m signalbot.main --list-accounts`")
        otp = await asyncio.to_thread(_rest, "POST", f"{API_BASE}/accounts/{self._account_id}/otp",
                                      self._token, self._app_id)
        url = _find_ws_url(otp)
        if not url:
            raise DerivError(f"No WebSocket URL in OTP response: {str(otp)[:300]}")
        return url

    @property
    def is_demo(self) -> bool:
        return "/ws/demo" in getattr(self, "_last_url", "")

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        async with self._connect_lock:
            if self._ws is not None and self._reader and not self._reader.done():
                return
            log.info("Connecting to Deriv…")
            url = await self._ws_url()
            self._last_url = url.split("?")[0]
            self._ws = await websockets.connect(url, ssl=_ssl_context(), ping_interval=20,
                                                ping_timeout=20)
            self._reader = asyncio.create_task(self._read_loop())
            if self._keepalive is None or self._keepalive.done():
                self._keepalive = asyncio.create_task(self._keepalive_loop())
        if self._token:
            self.account = {"loginid": self._account_id, "is_virtual": self.is_demo}
            try:
                bal = (await self.request({"balance": 1}))["balance"]
                self.account.update(balance=bal.get("balance"), currency=bal.get("currency"))
            except (DerivError, KeyError) as exc:
                log.warning("Could not read balance: %s", exc)
            log.info("Connected to account %s (%s) balance %s %s", self._account_id,
                     "DEMO" if self.is_demo else "REAL", self.account.get("balance"),
                     self.account.get("currency"))
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
                rid = msg.get("req_id")
                if rid is None:   # fall back to the oldest pending request of the same type
                    mtype = msg.get("msg_type") or next(iter(msg), None)
                    mtype = _REPLY_TYPES.get(mtype, mtype)
                    rid = next((k for k, t in self._types.items() if t == mtype), None)
                    if rid is None and len(self._pending) == 1:
                        rid = next(iter(self._pending))
                fut = self._pending.pop(rid, None)
                self._types.pop(rid, None)
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
        self._types[req_id] = next(iter(payload))
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
            self._types.pop(req_id, None)
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
            sym = _first(s, "symbol", "underlying_symbol", "underlying", "code")
            name = _first(s, "display_name", "underlying_symbol_name", "name", "display")
            if sym and name:
                self.symbols[normalize_market(name)] = sym
            elif sym:
                self.symbols[sym.lower()] = sym

    def resolve_symbol(self, market: str) -> str:
        if market.upper() in self.symbols.values() or market.upper() in STATIC_SYMBOLS.values():   # already a symbol, e.g. "1HZ75V"
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

    async def candles(self, symbol: str, start: int, end: int, granularity: int = 60) -> list[dict]:
        """All candles between start and end (epochs), fetched in pages of 5000."""
        out: dict[int, dict] = {}
        cursor = end
        while cursor > start:
            page_start = max(start, cursor - 5000 * granularity)
            resp = await self.request({"ticks_history": symbol, "style": "candles",
                                       "granularity": granularity, "start": page_start,
                                       "end": cursor, "count": 5000, "adjust_start_time": 0}, timeout=60)
            page = resp.get("candles", [])
            if not page:
                break
            for c in page:
                out[int(c["epoch"])] = c
            earliest = min(int(c["epoch"]) for c in page)
            if earliest > page_start + 10 * granularity:
                break   # history doesn't go back any further
            cursor = page_start
        return [out[k] for k in sorted(out)]

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
