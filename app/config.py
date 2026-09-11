from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Makes configuration usable in minimal test environments.
    def load_dotenv() -> bool:
        return False


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_user_id: int
    database_path: Path
    database_url: str | None
    mini_app_url: str | None
    trading_mode: str
    allow_live_trading: bool
    bybit_api_key: str | None
    bybit_api_secret: str | None
    bybit_testnet: bool
    encryption_key: str | None
    live_confirmation_code: str | None
    max_daily_trades: int
    max_risk_per_trade_pct: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    allowed_symbols: frozenset[str]

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        admin = os.getenv("ADMIN_USER_ID", "")
        mode = os.getenv("TRADING_MODE", "paper").lower()
        if not token or not admin:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and ADMIN_USER_ID must be set in .env")
        if mode not in {"paper", "live"}:
            raise RuntimeError("TRADING_MODE must be paper or live")
        settings = cls(
            telegram_token=token, admin_user_id=int(admin), database_path=Path(os.getenv("DATABASE_PATH", "trading_bot.db")),
            database_url=os.getenv("DATABASE_URL") or None, mini_app_url=os.getenv("MINI_APP_URL"),
            trading_mode=mode, allow_live_trading=_bool("ALLOW_LIVE_TRADING"),
            bybit_api_key=os.getenv("BYBIT_API_KEY"), bybit_api_secret=os.getenv("BYBIT_API_SECRET"),
            bybit_testnet=_bool("BYBIT_TESTNET", True), encryption_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY"),
            live_confirmation_code=os.getenv("LIVE_CONFIRMATION_CODE"), max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "3")),
            max_risk_per_trade_pct=Decimal(os.getenv("MAX_RISK_PER_TRADE_PCT", "1")),
            max_position_notional=Decimal(os.getenv("MAX_POSITION_NOTIONAL_USDT", "50")),
            max_daily_loss=Decimal(os.getenv("MAX_DAILY_LOSS_USDT", "20")),
            allowed_symbols=frozenset(x.strip().upper() for x in os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if x.strip()),
        )
        if settings.trading_mode == "live":
            if not settings.allow_live_trading:
                raise RuntimeError("Live mode requires ALLOW_LIVE_TRADING=true")
            if not all([settings.bybit_api_key, settings.bybit_api_secret, settings.live_confirmation_code]):
                raise RuntimeError("Live mode requires Bybit credentials and LIVE_CONFIRMATION_CODE")
        return settings
