from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .database import Database
from .models import OrderIntent


class RiskRejected(ValueError):
    pass


class RiskManager:
    def __init__(self, settings: Settings, database: Database):
        self.settings, self.database = settings, database

    def validate(self, order: OrderIntent) -> None:
        if order.symbol not in self.settings.allowed_symbols:
            raise RiskRejected("Symbol is not in the allow-list")
        if order.notional_usdt <= 0 or order.notional_usdt > self.settings.max_position_notional:
            raise RiskRejected(f"Notional must be > 0 and ≤ {self.settings.max_position_notional} USDT")
        trades, pnl = self.database.today_stats(order.user_id)
        if trades >= self.settings.max_daily_trades:
            raise RiskRejected("Daily trade limit reached")
        if pnl <= -self.settings.max_daily_loss:
            raise RiskRejected("Daily loss limit reached")
