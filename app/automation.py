from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal

from .database import Database
from .exchange import BybitClient, ExchangeError
from .models import OrderIntent, Side
from .security import CredentialVault


class DemoAutomationWorker:
    """Experimental Demo-only strategy. It has no mainnet configuration path."""
    def __init__(self, database: Database, vault: CredentialVault, notifier=None):
        self.database, self.vault = database, vault
        self.notifier = notifier
        self.running = True

    async def run(self) -> None:
        while self.running:
            for profile in self.database.demo_automation_users():
                try:
                    await self._scan(profile)
                except Exception as exc:
                    # A failed user scan must never stop or affect other users.
                    if self.notifier:
                        await self.notifier.send(int(profile["telegram_user_id"]), f"⚠️ Demo automation scan failed: {type(exc).__name__}. No new order was placed.")
                    continue
            await asyncio.sleep(60)

    async def _scan(self, profile: dict) -> None:
        user_id = int(profile["telegram_user_id"])
        trades, _ = self.database.today_stats(user_id)
        if trades >= int(profile["max_trades_per_day"]):
            return
        key = self.vault.decrypt(profile["encrypted_api_key"])
        secret = self.vault.decrypt(profile["encrypted_api_secret"])
        client = BybitClient(key, secret, environment="demo")
        try:
            rsi = await client.rsi_14(profile["symbol"])
            if rsi > Decimal("30"):
                return
            bucket = int(time.time() // 900)  # never retry the same 15-minute candle
            order_key = f"demo-{user_id}-{profile['symbol']}-{bucket}"
            intent = OrderIntent(user_id, profile["symbol"], Side.BUY, Decimal(profile["max_order_notional"]), order_key)
            if not self.database.reserve(intent):
                return
            price = await client.ticker(intent.symbol)
            stop = price * (Decimal("1") - Decimal(profile["stop_loss_pct"]) / Decimal("100"))
            take = price * (Decimal("1") + Decimal(profile["take_profit_pct"]) / Decimal("100"))
            result = await client.place_market_order(intent, stop_loss=stop, take_profit=take)
            self.database.finalize(intent.idempotency_key, result)
            if self.notifier:
                await self.notifier.send(user_id, f"🧪 Demo order submitted\n{intent.side.value} {intent.symbol}\nNotional: {intent.notional_usdt} USDT\nOrder: {result.order_id}\nStatus: {result.status}\nReview it in Bybit Demo Trading.")
        except (ExchangeError, ValueError):
            self.database.cancel_reservation(locals().get("order_key", ""))
            if self.notifier:
                await self.notifier.send(user_id, "⚠️ Demo automation rejected an order. No confirmed fill was recorded. Check your limits and Bybit Demo account.")
        finally:
            await client.close()
