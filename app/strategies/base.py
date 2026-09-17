from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Optional


Action = Literal["long", "short", "skip"]


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. `timestamp` is the opening time in ms (OKX convention)."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    """The output of a strategy evaluation for one market.

    - `action="skip"` means the strategy looked and found nothing. This is
      the normal, expected outcome most of the time. It is NOT a failure.
    - `action="long"` or `"short"` means: enter at `entry_price`, protect at
      `stop_loss`, target `take_profit`. All three prices are always set.
    """
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
        """Sanity check. Worker refuses any entry that fails this."""
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
    """Base class for every trading strategy.

    A strategy is a pure function: given closed candles, it returns a Signal.
    It never talks to the exchange, never reads the DB, never sizes positions.
    That separation is what makes backtesting honest.
    """

    name: str = "base"
    description: str = ""
    required_timeframes: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, market: str, candles: dict[str, list[Candle]]) -> Signal:
        """Evaluate one market.

        Args:
            market:  OKX instId, e.g. "BTC-USDT-SWAP".
            candles: dict keyed by timeframe, each a list of Candle ordered
                     oldest-first. Only CLOSED candles (confirm == True).
                     Keys match `required_timeframes`.

        Returns:
            A Signal. `action="skip"` is a valid, expected outcome.
        """
        raise NotImplementedError
