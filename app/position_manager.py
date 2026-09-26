from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


BREAKEVEN_TRIGGER_R = Decimal("1.5")
BREAKEVEN_BUFFER_ATR = Decimal("0.3")

TRAIL_START_R = Decimal("2.0")
TRAIL_WIDE_ATR = Decimal("0.5")

TIGHTEN_TRIGGER_R = Decimal("3.0")
TRAIL_TIGHT_ATR = Decimal("0.3")

TIME_STOP_BARS = 30


class Action(str, Enum):
    NONE = "none"
    MOVE_SL = "move_sl"
    CLOSE = "close"


@dataclass
class PositionAction:
    action: Action
    new_sl: Optional[Decimal] = None
    reason: str = ""
    candle_ts: Optional[int] = None

    @staticmethod
    def none(reason: str = "") -> "PositionAction":
        return PositionAction(Action.NONE, None, reason)

    @staticmethod
    def move_sl(new_sl: Decimal, reason: str, candle_ts: int) -> "PositionAction":
        return PositionAction(Action.MOVE_SL, new_sl, reason, candle_ts)

    @staticmethod
    def close(reason: str) -> "PositionAction":
        return PositionAction(Action.CLOSE, None, reason)


def evaluate(
    *,
    side: str,
    entry_price: Decimal,
    initial_sl: Decimal,
    original_tp: Decimal,
    current_sl: Decimal,
    last_closed_candle_close: Decimal,
    last_closed_candle_ts: int,
    last_action_candle_ts: Optional[int],
    atr: Decimal,
    bars_held: int,
) -> PositionAction:
    if entry_price <= 0 or initial_sl <= 0 or last_closed_candle_close <= 0 or atr <= 0:
        return PositionAction.none("invalid inputs")

    r_distance = abs(entry_price - initial_sl)
    if r_distance <= 0:
        return PositionAction.none("zero R distance")

    candle_close = last_closed_candle_close

    if side == "long":
        profit = candle_close - entry_price
    else:
        profit = entry_price - candle_close
    r_multiple = profit / r_distance

    if side == "long" and candle_close >= original_tp:
        return PositionAction.close("reached take profit")
    if side == "short" and candle_close <= original_tp:
        return PositionAction.close("reached take profit")

    if bars_held >= TIME_STOP_BARS:
        return PositionAction.close(f"time stop after {bars_held} bars")

    if last_action_candle_ts == last_closed_candle_ts:
        return PositionAction.none("already acted on this candle")

    if r_multiple >= BREAKEVEN_TRIGGER_R:
        buffer = atr * BREAKEVEN_BUFFER_ATR
        if side == "long":
            candidate = entry_price + buffer
        else:
            candidate = entry_price - buffer
        if _would_tighten(side, current_sl, candidate):
            return PositionAction.move_sl(
                candidate,
                f"breakeven+buffer at +{r_multiple:.1f}R (candle close)",
                last_closed_candle_ts,
            )

    if r_multiple >= TRAIL_START_R:
        if r_multiple >= TIGHTEN_TRIGGER_R:
            mult = TRAIL_TIGHT_ATR
            why = f"tight trail at +{r_multiple:.1f}R"
        else:
            mult = TRAIL_WIDE_ATR
            why = f"trail at +{r_multiple:.1f}R"

        if side == "long":
            candidate = candle_close - atr * mult
        else:
            candidate = candle_close + atr * mult

        if _would_tighten(side, current_sl, candidate):
            return PositionAction.move_sl(candidate, why, last_closed_candle_ts)

    return PositionAction.none("holding")


def _would_tighten(side: str, current_sl: Decimal, candidate: Decimal) -> bool:
    if side == "long":
        return candidate > current_sl
    return candidate < current_sl
