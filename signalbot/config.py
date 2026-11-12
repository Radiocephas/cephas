"""Settings loaded from environment variables / a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise SystemExit(f"Missing required setting {name} (see .env.example)")
    return value or default


def _bool(name: str, default: bool) -> bool:
    return str(_get(name, str(default))).lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Telegram user account (used to *read* the signal group)
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    signal_chats: list[str]
    # Telegram bot (used to send you notifications and accept commands)
    bot_token: str | None
    owner_id: int | None
    # Deriv
    deriv_token: str
    deriv_app_id: str
    deriv_ws_url: str
    currency: str
    # Money management
    base_stake: float
    martingale_multiplier: float
    max_martingale_steps: int
    max_stake: float
    daily_loss_limit: float
    daily_profit_target: float
    late_entry_tolerance: int   # seconds after entry time we still accept a trade
    dry_run: bool
    # Loss protection
    stake_percent: float        # >0: base stake = this % of balance (overrides base_stake)
    max_losing_sequences: int   # stop for the day after this many fully lost signals (0 = off)
    edge_gate: bool             # only trade real money while the group's measured win rate is good
    edge_min_signals: int       # signals to track before live trading can start
    edge_window: int            # judge the group on its most recent N signals
    edge_margin: float          # required win rate = break-even + this
    payout_rate: float          # fallback payout if Deriv can't be asked

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        owner = _get("OWNER_ID")
        return cls(
            tg_api_id=int(_get("TG_API_ID", required=True)),
            tg_api_hash=_get("TG_API_HASH", required=True),
            tg_session=_get("TG_SESSION", "signal_listener"),
            signal_chats=[c.strip() for c in _get("SIGNAL_CHATS", required=True).split(",") if c.strip()],
            bot_token=_get("BOT_TOKEN"),
            owner_id=int(owner) if owner else None,
            deriv_token=_get("DERIV_TOKEN", required=True),
            deriv_app_id=_get("DERIV_APP_ID", "1089"),
            deriv_ws_url=_get("DERIV_WS_URL", "wss://ws.derivws.com/websockets/v3"),
            currency=_get("CURRENCY", "USD"),
            base_stake=float(_get("BASE_STAKE", "1")),
            martingale_multiplier=float(_get("MARTINGALE_MULTIPLIER", "2.2")),
            max_martingale_steps=int(_get("MAX_MARTINGALE_STEPS", "1")),
            max_stake=float(_get("MAX_STAKE", "50")),
            daily_loss_limit=float(_get("DAILY_LOSS_LIMIT", "50")),
            daily_profit_target=float(_get("DAILY_PROFIT_TARGET", "0")),
            late_entry_tolerance=int(_get("LATE_ENTRY_TOLERANCE", "20")),
            dry_run=_bool("DRY_RUN", False),
            stake_percent=float(_get("STAKE_PERCENT", "0")),
            max_losing_sequences=int(_get("MAX_LOSING_SEQUENCES", "2")),
            edge_gate=_bool("EDGE_GATE", True),
            edge_min_signals=int(_get("EDGE_MIN_SIGNALS", "50")),
            edge_window=int(_get("EDGE_WINDOW", "100")),
            edge_margin=float(_get("EDGE_MARGIN", "0.03")),
            payout_rate=float(_get("PAYOUT_RATE", "0.95")),
        )
