from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Optional


Action = Literal["long", "short", "skip"]


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    action: Action
    market: str
    reason: str
    entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    atr: Optional[Decimal] = None
    risk_reward: Optional[float] = None
    indicators: dict = field(default_factory=dict)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_entry(self) -> bool:
        return self.action in ("long", "short")

    def is_valid(self) -> bool:
        if self.action == "skip":
            return True
        if self.entry_price is None or self.stop_loss is None or self.take_profit is None:
            return False
        if self.entry_price <= 0 or self.stop_loss <= 0 or self.take_profit <= 0:
            return False
        if self.action == "long":
            return self.stop_loss < self.entry_price < self.take_profit
        if self.action == "short":
            return self.take_profit < self.entry_price < self.stop_loss
        return False


class Strategy(ABC):
    name: str = "base"
    description: str = ""
    required_timeframes: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, market: str, candles: dict[str, list[Candle]]) -> Signal:
        raise NotImplementedError
