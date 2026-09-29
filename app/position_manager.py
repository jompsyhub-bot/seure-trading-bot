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
    if side not in ("long", "short"):
        return PositionAction.none(f"unknown side {side!r}")

    if entry_price <= 0 or initial_sl <= 0 or last_closed_candle_close <= 0 or atr <= 0:
        return PositionAction.none("invalid inputs")

    r_distance = abs(entry_price - initial_sl)
    if r_distance <= 0:
        return PositionAction.none("zero R distance")

    candle_close = last_closed_candle_close

    # Profit in R terms, based on the closed candle price
    if side == "long":
        profit = candle_close - entry_price
    else:
        profit = entry_price - candle_close
    r_multiple = profit / r_distance

    # ----------------------------------------------------------------
    # 1. Defensive close: candle closed PAST the current SL.
    #    Exchange SL should have fired already; if not, exit now.
    # ----------------------------------------------------------------
    if side == "long" and candle_close <= current_sl:
        return PositionAction.close(
            f"candle closed below current SL ({candle_close} <= {current_sl})"
        )
    if side == "short" and candle_close >= current_sl:
        return PositionAction.close(
            f"candle closed above current SL ({candle_close} >= {current_sl})"
        )

    # ----------------------------------------------------------------
    # 2. Take profit on candle close (backup for exchange-attached TP)
    # ----------------------------------------------------------------
    if side == "long" and candle_close >= original_tp:
        return PositionAction.close("reached take profit")
    if side == "short" and candle_close <= original_tp:
        return PositionAction.close("reached take profit")

    # ----------------------------------------------------------------
    # 3. Time stop
    # ----------------------------------------------------------------
    if bars_held >= TIME_STOP_BARS:
        return PositionAction.close(f"time stop after {bars_held} bars")

    # ----------------------------------------------------------------
    # 4. Only act on SL moves once per candle
    # ----------------------------------------------------------------
    if last_action_candle_ts == last_closed_candle_ts:
        return PositionAction.none("already acted on this candle")

    # ----------------------------------------------------------------
    # 5. Collect ALL valid SL candidates and pick the tightest one.
    #    This fixes the "breakeven blocks trailing" one-candle lag.
    # ----------------------------------------------------------------
    candidates: list[tuple[Decimal, str]] = []

    # 5a. Breakeven + ATR buffer at +1.5R
    if r_multiple >= BREAKEVEN_TRIGGER_R:
        buffer = atr * BREAKEVEN_BUFFER_ATR
        if side == "long":
            be_candidate = entry_price + buffer
        else:
            be_candidate = entry_price - buffer
        candidates.append((
            be_candidate,
            f"breakeven+buffer at +{r_multiple:.1f}R",
        ))

    # 5b. Trailing stop at +2R (wide) or +3R (tight)
    if r_multiple >= TRAIL_START_R:
        if r_multiple >= TIGHTEN_TRIGGER_R:
            mult = TRAIL_TIGHT_ATR
            why = f"tight trail at +{r_multiple:.1f}R"
        else:
            mult = TRAIL_WIDE_ATR
            why = f"trail at +{r_multiple:.1f}R"

        if side == "long":
            trail_candidate = candle_close - atr * mult
        else:
            trail_candidate = candle_close + atr * mult
        candidates.append((trail_candidate, why))

    # 5c. Keep only candidates that tighten the current SL
    valid = [
        (c, why) for c, why in candidates
        if _would_tighten(side, current_sl, c)
    ]

    if not valid:
        return PositionAction.none("holding")

    # 5d. Pick the tightest — highest for long, lowest for short
    if side == "long":
        best_sl, best_reason = max(valid, key=lambda item: item[0])
    else:
        best_sl, best_reason = min(valid, key=lambda item: item[0])

    return PositionAction.move_sl(best_sl, best_reason, last_closed_candle_ts)


def _would_tighten(side: str, current_sl: Decimal, candidate: Decimal) -> bool:
    """True if the candidate SL is tighter than the current one."""
    if side == "long":
        return candidate > current_sl
    return candidate < current_sl
