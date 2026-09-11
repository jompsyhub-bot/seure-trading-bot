from __future__ import annotations

import asyncio
from collections import defaultdict

from .database import Database
from .models import OrderIntent, OrderResult
from .risk import RiskManager


class TradingService:
    def __init__(self, database: Database, risk: RiskManager, broker):
        self.database, self.risk, self.broker = database, risk, broker
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def execute(self, intent: OrderIntent) -> OrderResult:
        async with self._locks[intent.user_id]:
            old = self.database.existing_order(intent.idempotency_key)
            if old:
                return old
            self.risk.validate(intent)
            if not self.database.reserve(intent):
                old = self.database.existing_order(intent.idempotency_key)
                if old:
                    return old
                raise RuntimeError("Order request is already being processed")
            try:
                result = await self.broker.place_market_order(intent)
                # Bybit's create endpoint only acknowledges receipt. Store the
                # actual current exchange state rather than inventing a fill.
                if result.status == "NEW" and hasattr(self.broker, "order_status"):
                    result = await self.broker.order_status(result.order_id)
                self.database.finalize(intent.idempotency_key, result)
                return result
            except Exception:
                self.database.cancel_reservation(intent.idempotency_key)
                raise
