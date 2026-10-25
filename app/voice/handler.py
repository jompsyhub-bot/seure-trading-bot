"""Execute parsed voice intents against the OKX client."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, ROUND_DOWN

from ..exchange import OKXClient, ExchangeError, OrderRejected
from ..sessions import EndReason, SessionConfig
from .parser import VoiceIntent

log = logging.getLogger(__name__)


HELP_TEXT = (
    "I can do these things. "
    "Say: long BTC 100x. short ETH 50x. close BTC. "
    "What's my PNL. Show positions. My balance. "
    "Signal BTC. Start auto trade. Stop everything. "
    "Say cancel or confirm at any prompt."
)


async def execute_query(intent: VoiceIntent, client: OKXClient) -> str:
    if intent.intent_type == "help":
        return HELP_TEXT

    if intent.intent_type == "query_balance":
        bal = await client.account_balance()
        return f"Your balance is {bal:.2f} US dollars."

    if intent.intent_type == "query_pnl":
        positions = await client.swap_positions()
        total = sum(Decimal(p.get("upl") or "0") for p in positions)
        bal = await client.account_balance()
        sign = "up" if total >= 0 else "down"
        return f"Balance {bal:.2f} dollars. Open positions are {sign} {abs(total):.2f} dollars."

    if intent.intent_type == "query_positions":
        positions = await client.swap_positions()
        if not positions:
            return "You have no open positions."
        parts = []
        for p in positions[:5]:
            inst = p.get("instId", "").replace("-USDT-SWAP", "")
            side = p.get("posSide", "net")
            if side == "net":
                side = "long" if Decimal(p.get("pos") or "0") > 0 else "short"
            pnl = Decimal(p.get("upl") or "0")
            sign = "up" if pnl >= 0 else "down"
            parts.append(f"{side} {inst} {sign} {abs(pnl):.2f}")
        return f"You have {len(positions)} positions. " + ". ".join(parts)

    if intent.intent_type == "signal":
        from ..signals import analyze_token, SignalNotSupported
        from ..signals.analyzer import InsufficientHistory
        try:
            result = await analyze_token(intent.inst_id.replace("-USDT-SWAP", "/USDT"), client)
        except (SignalNotSupported, InsufficientHistory) as exc:
            return f"Could not analyze {intent.inst_id}: {exc}"

        sig = result["signal"]
        if sig["direction"] == "none":
            reason = (sig.get("reasons") or [""])[0]
            return f"No signal for {intent.inst_id.replace('-USDT-SWAP', '')}. {reason}"

        return (
            f"{sig['direction'].upper()} signal on {intent.inst_id.replace('-USDT-SWAP', '')}. "
            f"Confidence {sig['confidence']} percent. "
            f"Entry {sig['entry_ref']}. Stop {sig['stop_loss']}. "
            f"Strategy {sig['strategy'].replace('_', ' ')}."
        )

    return "Sorry, I didn't catch that."


def build_confirmation_message(intent: VoiceIntent) -> str:
    if intent.intent_type == "trade_open":
        parts = [f"{intent.side.upper()} {intent.inst_id.replace('-USDT-SWAP', '')}"]
        if intent.leverage:
            parts.append(f"at {intent.leverage}x leverage")
        if intent.margin_usdt:
            parts.append(f"with {intent.margin_usdt} dollar margin")
        return "Confirm " + " ".join(parts) + ". Default ATR-based stop and target will be used. Say confirm or cancel."

    if intent.intent_type == "trade_close":
        return f"Confirm closing your {intent.inst_id.replace('-USDT-SWAP', '')} position. Say confirm or cancel."

    if intent.intent_type == "auto_start":
        parts = ["Confirm starting auto trade"]
        if intent.leverage:
            parts.append(f"at {intent.leverage}x leverage")
        if intent.strategy:
            parts.append(f"with {intent.strategy.replace('_', ' ')} strategy")
        if intent.rr:
            parts.append(f"at {intent.rr} risk reward")
        return " ".join(parts) + ". Say confirm or cancel."

    if intent.intent_type == "stop":
        return "Confirm stopping all automation and closing all bot positions. Say confirm or cancel."

    return "Say confirm to proceed."


async def _atr_distance(client: OKXClient, inst_id: str) -> Decimal:
    candles = await client.candles(inst_id, "4H", 20)
    if len(candles) < 15:
        price = await client.last_price(inst_id)
        return price * Decimal("0.015")
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr = max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"]))
        trs.append(tr)
    atr = sum(trs[-14:]) / 14
    return Decimal(str(atr))


async def execute_trade_open(intent: VoiceIntent, client: OKXClient, user_id: int, database) -> str:
    try:
        instrument = await client.swap_instrument(intent.inst_id)
    except ExchangeError as exc:
        return f"Could not find {intent.inst_id}: {exc}"

    exchange_max = int(instrument.get("lever") or 100)
    leverage = min(intent.leverage or exchange_max, exchange_max)

    price = await client.last_price(intent.inst_id)
    atr = await _atr_distance(client, intent.inst_id)
    sl_dist = atr * Decimal("2")
    tp_dist = sl_dist * Decimal("3")

    if intent.side == "long":
        sl = price - sl_dist
        tp = price + tp_dist
    else:
        sl = price + sl_dist
        tp = price - tp_dist

    balance = await client.account_balance()
    margin = intent.margin_usdt or max(Decimal("1.50"), balance * Decimal("0.05"))
    notional = margin * Decimal(leverage)

    ct_val = Decimal(instrument["ctVal"])
    ct_mult = Decimal(instrument.get("ctMult") or "1")
    lot_sz = Decimal(instrument.get("lotSz") or "1")
    min_sz = Decimal(instrument.get("minSz") or "1")
    contract_notional = ct_val * ct_mult * price
    target = notional / contract_notional
    steps = (target / lot_sz).to_integral_value(rounding=ROUND_DOWN)
    contracts = steps * lot_sz
    if contracts < min_sz:
        contracts = min_sz

    actual_notional = contracts * contract_notional
    actual_margin = actual_notional / Decimal(leverage)

    if actual_margin > balance * Decimal("0.90"):
        return f"Not enough balance. Need {actual_margin:.2f} margin, have {balance:.2f}."

    account = await client.account_config()
    if account.get("acctLv") not in {"2", "3", "4"}:
        return "OKX account is in Spot mode. Switch to Futures mode in OKX settings first."

    position_side = intent.side if account.get("posMode") == "long_short_mode" else "net"
    exchange_side = "buy" if intent.side == "long" else "sell"
    order_key = f"voice{uuid.uuid4().hex[:20]}"[:28]

    try:
        await client.set_leverage(intent.inst_id, Decimal(leverage), position_side)
        accepted = await client.place_market_swap(
            inst_id=intent.inst_id, side=exchange_side, position_side=position_side,
            contracts=contracts, client_order_id=order_key,
            take_profit=tp, stop_loss=sl,
        )
    except OrderRejected as exc:
        return f"OKX rejected the order: {exc}"
    except ExchangeError as exc:
        return f"Exchange error: {exc}"

    ord_id = accepted.get("ordId")
    if not ord_id:
        return "OKX returned no order ID. Check the OKX app before retrying."

    return (
        f"Order placed. {intent.side.upper()} {intent.inst_id.replace('-USDT-SWAP', '')} "
        f"at {leverage}x. {contracts} contracts, {actual_margin:.2f} dollars margin. "
        f"Stop {sl:.2f}, target {tp:.2f}."
    )


async def execute_trade_close(intent: VoiceIntent, client: OKXClient) -> str:
    positions = await client.swap_positions()
    match = next((p for p in positions if p.get("instId") == intent.inst_id), None)
    if not match:
        return f"You have no open {intent.inst_id.replace('-USDT-SWAP', '')} position."

    pos_side = match.get("posSide") or "net"
    contracts = abs(Decimal(match.get("pos") or "0"))
    if contracts <= 0:
        return "Position has no contracts to close."

    if pos_side == "long":
        close_side = "sell"
    elif pos_side == "short":
        close_side = "buy"
    else:
        close_side = "sell" if Decimal(match.get("pos") or "0") > 0 else "buy"

    order_key = f"voice{uuid.uuid4().hex[:20]}"[:28]
    try:
        accepted = await client.close_market_swap(
            inst_id=intent.inst_id, side=close_side,
            position_side=pos_side, contracts=contracts,
            client_order_id=order_key,
        )
    except OrderRejected as exc:
        return f"OKX rejected the close: {exc}"
    except ExchangeError as exc:
        return f"Exchange error: {exc}"

    return f"Closed {intent.inst_id.replace('-USDT-SWAP', '')} position."


async def execute_auto_start(intent: VoiceIntent, client: OKXClient, user_id: int,
                             session_manager, settings) -> str:
    existing = session_manager.get_active(user_id)
    if existing:
        return f"You already have an active session number {existing.id}. Stop it first."

    balance = await client.account_balance()
    if balance < Decimal("5"):
        return f"Balance {balance:.2f} is below the 5 dollar minimum."

    env = "live" if getattr(client, "live", False) else "demo"
    leverage = intent.leverage or 20
    strategy_name = intent.strategy or "daily_breakout"
    if strategy_name == "both":
        strategy_str = "daily_breakout,daily_pullback"
    elif strategy_name == "daily_pullback":
        strategy_str = "daily_pullback"
    else:
        strategy_str = "daily_breakout"

    config = SessionConfig(
        mode="day_trading",
        strategy=strategy_str,
        duration_hours=168,
        target_profit=min(Decimal("5"), balance * Decimal("0.10")),
        max_loss=min(Decimal("5"), balance * Decimal("0.05")),
        risk_per_trade_pct=Decimal("2"),
        risk_per_trade_usdt=Decimal("0"),
        risk_unit="pct",
        safety_floor_pct=Decimal("80"),
        leverage=leverage,
        markets="auto",
        environment=env,
    )
    session_id = session_manager.create_session(user_id, config, balance)

    return (
        f"Session {session_id} started. "
        f"{strategy_str.replace('_', ' ')} on 9 markets. "
        f"{leverage}x leverage ceiling. "
        f"Target plus {config.target_profit:.2f}, max loss minus {config.max_loss:.2f}. "
        f"Running for 7 days."
    )


async def execute_stop(client: OKXClient, user_id: int, session_manager) -> str:
    session = session_manager.get_active(user_id)
    if not session:
        return "No active session to stop."

    positions = await client.swap_positions()
    closed = 0
    for p in positions:
        inst = p.get("instId")
        if not inst:
            continue
        pos_side = p.get("posSide") or "net"
        contracts = abs(Decimal(p.get("pos") or "0"))
        if contracts <= 0:
            continue
        close_side = "sell" if (pos_side == "long" or (pos_side == "net" and Decimal(p.get("pos") or "0") > 0)) else "buy"
        try:
            await client.close_market_swap(
                inst_id=inst, side=close_side, position_side=pos_side,
                contracts=contracts, client_order_id=f"voice{uuid.uuid4().hex[:20]}"[:28],
            )
            closed += 1
        except Exception:
            pass

    balance = await client.account_balance()
    session_manager.end_session(session.id, EndReason.USER_STOP, balance)
    return f"Session {session.id} stopped. Closed {closed} position{'s' if closed != 1 else ''}."
