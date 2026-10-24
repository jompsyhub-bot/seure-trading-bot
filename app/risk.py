from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


MIN_ACCOUNT_BALANCE = Decimal("5")
# Allow up to 50% risk per trade so small accounts can still trade meaningfully
HARD_MAX_RISK_PCT = Decimal("50")

# --- Risk floor -----------------------------------------------------------
# Minimum USDT risk per trade, and minimum risk as % of balance. The effective
# floor is max(MIN_RISK_USDT, balance * MIN_RISK_PCT / 100). This ensures small
# accounts take positions large enough to matter, while scaling up automatically
# as the balance grows. Crossover point is balance = MIN_RISK_USDT * 100 / MIN_RISK_PCT
# = $30 — below that the USDT floor wins, above that the percentage floor wins.
MIN_RISK_USDT = Decimal("1.50")
MIN_RISK_PCT = Decimal("5")

LOSS_STREAK_PAUSE_THRESHOLD = 3
LOSS_STREAK_PAUSE_HOURS = 4

LIQUIDATION_BUFFER = Decimal("0.90")
FREE_MARGIN_FRACTION = Decimal("0.30")


class Tier(str, Enum):
    MICRO = "micro"
    SMALL = "small"
    STANDARD = "standard"
    LARGE = "large"


@dataclass(frozen=True)
class TierRules:
    min_balance: Decimal
    max_concurrent: int
    max_daily_loss_pct: Decimal
    max_total_exposure_pct: Decimal
    concentration_cap_pct: Decimal


TIER_RULES: dict = {
    Tier.MICRO: TierRules(
        min_balance=Decimal("5"),
        max_concurrent=1,
        max_daily_loss_pct=Decimal("50.0"),
        max_total_exposure_pct=Decimal("100.0"),
        concentration_cap_pct=Decimal("50.0"),
    ),
    Tier.SMALL: TierRules(
        min_balance=Decimal("50"),
        max_concurrent=2,
        max_daily_loss_pct=Decimal("20.0"),
        max_total_exposure_pct=Decimal("80.0"),
        concentration_cap_pct=Decimal("20.0"),
    ),
    Tier.STANDARD: TierRules(
        min_balance=Decimal("500"),
        max_concurrent=3,
        max_daily_loss_pct=Decimal("10.0"),
        max_total_exposure_pct=Decimal("60.0"),
        concentration_cap_pct=Decimal("10.0"),
    ),
    Tier.LARGE: TierRules(
        min_balance=Decimal("5000"),
        max_concurrent=3,
        max_daily_loss_pct=Decimal("8.0"),
        max_total_exposure_pct=Decimal("50.0"),
        concentration_cap_pct=Decimal("5.0"),
    ),
}


def classify(balance: Decimal) -> Tier:
    if balance < Decimal("50"):
        return Tier.MICRO
    if balance < Decimal("500"):
        return Tier.SMALL
    if balance < Decimal("5000"):
        return Tier.STANDARD
    return Tier.LARGE


def rules_for(balance: Decimal) -> TierRules:
    return TIER_RULES[classify(balance)]


def loss_streak_multiplier(loss_streak: int) -> Decimal:
    if loss_streak <= 0:
        return Decimal("1.00")
    if loss_streak == 1:
        return Decimal("0.75")
    return Decimal("0.50")


def _minimum_risk(balance: Decimal) -> Decimal:
    """Effective risk floor: $1.50 or 5% of balance, whichever is higher.

    This is the *floor*, not the target. The target comes from the user's
    session setting; the floor only kicks in when the user's computed risk
    is below it. Streak multipliers are applied BEFORE the floor, so on
    accounts where the floor is binding (balance < ~$30) the streak
    reduction is effectively inert.
    """
    pct_floor = balance * MIN_RISK_PCT / Decimal("100")
    return max(MIN_RISK_USDT, pct_floor).quantize(Decimal("0.01"))


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    risk_amount: Decimal = Decimal("0")
    size_multiplier: Decimal = Decimal("1.0")
    tier: Optional[Tier] = None

    @staticmethod
    def reject(reason: str, tier: Optional[Tier] = None) -> "RiskDecision":
        return RiskDecision(allowed=False, reason=reason, tier=tier)

    @staticmethod
    def allow(risk_amount: Decimal, tier: Tier, multiplier: Decimal) -> "RiskDecision":
        return RiskDecision(
            allowed=True, reason="ok",
            risk_amount=risk_amount,
            size_multiplier=multiplier,
            tier=tier,
        )


def _hours_since(ts: Optional[datetime]) -> float:
    if ts is None:
        return 999.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def evaluate_entry(
    *,
    balance: Decimal,
    session_risk_pct: Optional[Decimal] = None,
    session_risk_usdt: Optional[Decimal] = None,
    current_exposure: Decimal,
    open_positions_count: int,
    loss_streak: int = 0,
    last_loss_at: Optional[datetime] = None,
) -> RiskDecision:
    if balance < MIN_ACCOUNT_BALANCE:
        return RiskDecision.reject(
            f"balance ${balance:.2f} is below the ${MIN_ACCOUNT_BALANCE} minimum"
        )

    if loss_streak >= LOSS_STREAK_PAUSE_THRESHOLD:
        hours = _hours_since(last_loss_at)
        if hours < LOSS_STREAK_PAUSE_HOURS:
            remaining = LOSS_STREAK_PAUSE_HOURS - hours
            return RiskDecision.reject(
                f"paused after {loss_streak} consecutive losses — "
                f"{remaining:.1f}h remaining"
            )
        loss_streak = 0

    tier = classify(balance)
    rules = TIER_RULES[tier]

    if open_positions_count >= rules.max_concurrent:
        return RiskDecision.reject(
            f"already {open_positions_count} open position(s); "
            f"{tier.value} tier caps at {rules.max_concurrent}",
            tier=tier,
        )

    max_exposure = balance * rules.max_total_exposure_pct / Decimal("100")
    if current_exposure >= max_exposure:
        return RiskDecision.reject(
            f"exposure ${current_exposure:.2f} is at the "
            f"{rules.max_total_exposure_pct}% cap (${max_exposure:.2f})",
            tier=tier,
        )

    multiplier = loss_streak_multiplier(loss_streak)

    # ---- Compute base risk from the user's session setting -----------------
    if session_risk_usdt is not None and session_risk_usdt > 0:
        hard_cap = balance * HARD_MAX_RISK_PCT / Decimal("100")
        base_risk = min(session_risk_usdt, hard_cap)
    elif session_risk_pct is not None and session_risk_pct > 0:
        effective_pct = min(session_risk_pct, HARD_MAX_RISK_PCT)
        base_risk = balance * effective_pct / Decimal("100")
    else:
        return RiskDecision.reject("no risk amount specified", tier=tier)

    # ---- Apply loss-streak multiplier --------------------------------------
    calculated_risk = (base_risk * multiplier).quantize(Decimal("0.01"))

    # ---- Apply MINIMUM RISK FLOOR ($1.50 or 5% of balance) -----------------
    # Per user intent: even on a $7.22 account, every trade should risk enough
    # to produce a meaningful P&L. Streak reduction happens above; the floor
    # lifts the result back up if the streak dropped it below the minimum.
    min_risk = _minimum_risk(balance)
    risk_amount = max(calculated_risk, min_risk)
    if risk_amount > calculated_risk:
        log.info(
            "Risk floored: calculated=$%s → floored=$%s "
            "(balance=$%s, floor=$%s, tier=%s)",
            calculated_risk, risk_amount, balance, min_risk, tier.value,
        )

    # ---- Apply concentration cap, but never below the floor (Q1=A) --------
    tier_cap = (
        balance * rules.concentration_cap_pct / Decimal("100")
    ).quantize(Decimal("0.01"))
    effective_cap = max(tier_cap, min_risk)
    if risk_amount > effective_cap:
        risk_amount = effective_cap

    # ---- Apply the hard 50% ceiling, but never below the floor ------------
    hard_cap = (
        balance * HARD_MAX_RISK_PCT / Decimal("100")
    ).quantize(Decimal("0.01"))
    effective_hard_cap = max(hard_cap, min_risk)
    if risk_amount > effective_hard_cap:
        risk_amount = effective_hard_cap

    if risk_amount <= 0:
        return RiskDecision.reject("computed risk amount is zero", tier=tier)

    return RiskDecision.allow(risk_amount, tier, multiplier)


def pick_safe_leverage(
    *,
    user_max_leverage: int,
    exchange_max_leverage: int,
    stop_distance_pct: Decimal,
    available_margin: Decimal,
    notional_needed: Decimal,
) -> Optional[int]:
    """Choose leverage for an entry.

    Policy (per user intent):
      * Use the leverage the user picked at session setup.
      * Cap it at the exchange's maximum for the token — never exceed it.
      * If the user did not pick (0/None/auto), use the exchange maximum.
      * If the user's choice is too low to fit the trade within 90% of
        available balance, bump to the highest the exchange allows
        (up to what's actually needed to fit).

    `stop_distance_pct` is retained for signature compatibility with the
    caller but is NOT used to cap leverage. Small accounts trading isolated
    margin at high leverage can only lose the margin posted — the user
    accepts liquidation as the effective stop in exchange for the size
    flexibility this enables on balances that can't otherwise afford BTC.

    Returns None only when the trade is genuinely unfittable (invalid
    inputs, or the exchange max still can't cover the required margin).
    """
    if available_margin <= 0 or notional_needed <= 0:
        return None

    exchange_max = max(1, int(exchange_max_leverage))

    # User's explicit choice, or exchange max if unspecified, capped at
    # the exchange maximum. Nothing else lowers this number.
    if user_max_leverage and user_max_leverage > 0:
        target = min(int(user_max_leverage), exchange_max)
    else:
        target = exchange_max

    # If the chosen leverage can't fit the trade within 90% of balance,
    # bump to whatever is needed — capped at the exchange maximum.
    margin_budget = available_margin * Decimal("0.90")
    if margin_budget > 0:
        needed = (notional_needed / margin_budget).to_integral_value(
            rounding=ROUND_CEILING
        )
        needed_int = int(needed)
        if needed_int > target:
            target = min(needed_int, exchange_max)

    return target
