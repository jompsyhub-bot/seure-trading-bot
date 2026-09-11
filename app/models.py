from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "Buy"
    SELL = "Sell"


@dataclass(frozen=True)
class OrderIntent:
    user_id: int
    symbol: str
    side: Side
    notional_usdt: Decimal
    idempotency_key: str


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    price: Decimal
    quantity: Decimal
