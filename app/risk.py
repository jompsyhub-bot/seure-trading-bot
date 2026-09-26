
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Optional


MIN_ACCOUNT_BALANCE = Decimal("5")
HARD_MAX_RISK_PCT = Decimal("5")
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
        max_daily_loss_pct=Decimal("5.0"),
        max_total_exposure_pct=Decimal("30.0"),
        concentration_cap_pct=Decimal("3.0"),
    ),
    Tier.SMALL: TierRules(
        min_balance=Decimal("50"),
        max_concurrent=2,
        max_daily_loss_pct=Decimal("6.0"),
        max_total_exposure_pct=Decimal("40.0"),
        concentration_cap_pct=Decimal("4.0"),
    ),
    Tier.STANDARD: TierRules(
        min_balance=Decimal("500"),
        max_concurrent=3,
        max_daily_loss_pct=Decimal("8.0"),
        max_total_exposure_pct=Decimal("50.0"),
        concentration_cap_pct=Decimal("5.0"),
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


def evaluate_entry(
    *,
    balance: Decimal,
    session_risk_pct: Decimal,
    current_exposure: Decimal,
    open_positions_count: int,
    loss_streak: int,
) -> RiskDecision:
    """Decide whether a new position may be opened right now."""
    if balance < MIN_ACCOUNT_BALANCE:
        return RiskDecision.reject(
            f"balance ${balance:.2f} is below the ${MIN_ACCOUNT_BALANCE} minimum"
        )

    if loss_streak >= LOSS_STREAK_PAUSE_THRESHOLD:
        return RiskDecision.reject(
            f"paused after {loss_streak} consecutive losses — "
            f"wait {LOSS_STREAK_PAUSE_HOURS}h before opening new trades"
        )

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

    effective_pct = min(session_risk_pct, HARD_MAX_RISK_PCT)
    multiplier = loss_streak_multiplier(loss_streak)
    risk_amount = (balance * effective_pct / Decimal("100")) * multiplier
    risk_amount = risk_amount.quantize(Decimal("0.01"))

    concentration_cap = (
        balance * rules.concentration_cap_pct / Decimal("100")
    ).quantize(Decimal("0.01"))
    if risk_amount > concentration_cap:
        risk_amount = concentration_cap

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
    """Pick the smallest leverage that satisfies liquidation safety,
    margin availability, and user/exchange caps.

    Returns None if no leverage works.
    """
    if stop_distance_pct <= 0 or available_margin <= 0 or notional_needed <= 0:
        return None

    # Liquidation distance ≈ 1 / leverage. Require liquidation × buffer ≥ stop.
    safe_max = int(
        (LIQUIDATION_BUFFER / stop_distance_pct).to_integral_value(rounding=ROUND_DOWN)
    )

    ceiling = min(user_max_leverage, exchange_max_leverage, safe_max)
    if ceiling < 1:
        return None

    margin_budget = available_margin * (Decimal("1") - FREE_MARGIN_FRACTION)
    if margin_budget <= 0:
        return None

    min_required = int(
        (notional_needed / margin_budget).to_integral_value(rounding=ROUND_DOWN) + 1
    )
    if min_required > ceiling:
        return None

    return max(min_required, 1)


def compute_contracts(
    *,
    risk_amount: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    contract_value: Decimal,
    contract_multiplier: Decimal = Decimal("1"),
    min_contracts: Decimal = Decimal("1"),
) -> Optional[Decimal]:
    """Convert a dollar risk budget into a contract count.

    Returns None when the exchange minimum can't be reached.
    """
    price_distance = abs(entry_price - stop_price)
    if price_distance <= 0 or contract_value <= 0 or contract_multiplier <= 0:
        return None

    per_contract_risk = price_distance * contract_value * contract_multiplier
    if per_contract_risk <= 0:
        return None

    contracts = (risk_amount / per_contract_risk).to_integral_value(rounding=ROUND_DOWN)
    if contracts < min_contracts:
        return None
    return contracts


def minimum_viable_balance(
    *,
    entry_price: Decimal,
    stop_price: Decimal,
    contract_value: Decimal,
    contract_multiplier: Decimal = Decimal("1"),
    min_contracts: Decimal = Decimal("1"),
    risk_pct: Decimal = Decimal("1"),
) -> Decimal:
    """Return the account balance needed for one minimum-sized position
    at the given risk %. Used for user-facing messages."""
    price_distance = abs(entry_price - stop_price)
    per_contract_risk = price_distance * contract_value * contract_multiplier * min_contracts
    if per_contract_risk <= 0 or risk_pct <= 0:
        return Decimal("0")
    return (per_contract_risk / (risk_pct / Decimal("100"))).quantize(Decimal("0.01"))
