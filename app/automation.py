
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN

from .exchange import ExchangeError, OKXDemoClient, OrderRejected
from .models import OrderIntent, OrderResult, Side
from .sessions import (
    EndReason,
    SessionState,
    SessionStatus,
    check_can_open,
    should_end_session,
)
from .strategies.base import Candle
from .strategies.daily_pullback import DailyPullbackStrategy

log = logging.getLogger(__name__)


# Markets the worker trades when the session is set to "auto".
# ETH validated in and out of sample; DOGE and SOL validated in sample.
# BTC intentionally excluded (negative out-of-sample).
DEFAULT_AUTO_MARKETS = [
    "ETH-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SOL-USDT-SWAP",
]

SCAN_INTERVAL_SECONDS = 60
MAX_CONCURRENT_POSITIONS = 3
MARGIN_UTILISATION_CAP = Decimal("90")  # % of balance as initial margin


def _strategy_for(strategy_name: str):
    if strategy_name == "daily_pullback":
        return DailyPullbackStrategy()
    raise ValueError(f"Unknown strategy: {strategy_name}")


def _candle_from_exchange(row: dict) -> Candle:
    return Candle(
        timestamp=int(row["timestamp"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


class AutoTradingWorker:
    def __init__(self, session_manager, database, vault, notifier):
        self.session_manager = session_manager
        self.database = database
        self.vault = vault
        self.notifier = notifier
        self.running = True

    async def run(self) -> None:
        log.info("AutoTradingWorker started")
        # brief warmup so lifespan finishes wiring the app
        await asyncio.sleep(5)
        while self.running:
            try:
                sessions = self.session_manager.list_all_active()
                if sessions:
                    log.info("Scanning %d active session(s)", len(sessions))
                    for session in sessions:
                        try:
                            await self._process_session(session)
                        except Exception as exc:
                            log.warning("Session %s tick failed: %s", session.id, exc)
            except Exception as exc:
                log.warning("AutoTradingWorker tick failed: %s", exc)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Per-session processing
    # ------------------------------------------------------------------

    async def _process_session(self, session: SessionState) -> None:
        user_id = session.user_id

        creds = self.database.exchange_credentials(user_id)
        if not creds or creds.get("provider") != "okx":
            log.debug("Session %s: user %s has no OKX credentials", session.id, user_id)
            return

        client = OKXDemoClient(
            self.vault.decrypt(creds["encrypted_api_key"]),
            self.vault.decrypt(creds["encrypted_api_secret"]),
            self.vault.decrypt(creds["encrypted_passphrase"]),
        )
        try:
            balance = await client.account_balance()
            self.session_manager.update_balance(session.id, balance)

            # 1. Should the entire session end?
            reason = should_end_session(session, balance)
            if reason:
                await self._end_session(client, session, reason, balance)
                return

            # 2. Fetch current positions once
            try:
                positions = await client.swap_positions()
            except ExchangeError as exc:
                log.info("Session %s: positions unavailable (%s)", session.id, exc)
                return

            open_inst_ids = {p.get("instId") for p in positions}

            # 3. Resolve market list
            markets = self._resolve_markets(session)
            if not markets:
                return

            # 4. Evaluate each market
            strategy = _strategy_for(session.config.strategy)
            for market in markets:
                if market in open_inst_ids:
                    continue  # already in a position on this market

                risk_check = check_can_open(
                    session, balance, len(positions), MAX_CONCURRENT_POSITIONS
                )
                if not risk_check.allowed:
                    log.debug("Session %s: entry skipped — %s", session.id, risk_check.reason)
                    break  # no point trying other markets with the same guard

                try:
                    signal = await self._evaluate_market(client, strategy, market)
                except Exception as exc:
                    log.debug("Session %s: evaluate %s failed — %s", session.id, market, exc)
                    continue

                if not signal.is_entry or not signal.is_valid():
                    continue

                placed = await self._place_entry(
                    client, session, signal, balance, risk_check.risk_amount
                )
                if placed:
                    # refresh position count for the next iteration
                    try:
                        positions = await client.swap_positions()
                        open_inst_ids = {p.get("instId") for p in positions}
                    except Exception:
                        pass

            self.session_manager.touch_scan(session.id)

        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Market selection
    # ------------------------------------------------------------------

    def _resolve_markets(self, session: SessionState) -> list[str]:
        raw = (session.config.markets or "auto").strip()
        if raw == "auto":
            return list(DEFAULT_AUTO_MARKETS)
        return [m.strip() for m in raw.split(",") if m.strip()]

    # ------------------------------------------------------------------
    # Strategy evaluation
    # ------------------------------------------------------------------

    async def _evaluate_market(self, client, strategy, market: str):
        c4h_raw = await client.candles(market, "4H", 200)
        c1d_raw = await client.candles(market, "1Dutc", 100)
        if len(c4h_raw) < 60 or len(c1d_raw) < 60:
            return None
        c4h = [_candle_from_exchange(r) for r in c4h_raw]
        c1d = [_candle_from_exchange(r) for r in c1d_raw]
        return strategy.evaluate(market, {"4h": c4h, "1d": c1d})

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    async def _place_entry(self, client, session, signal, balance: Decimal,
                           risk_amount: Decimal) -> bool:
        market = signal.market
        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit

        try:
            instrument = await client.swap_instrument(market)
        except ExchangeError as exc:
            log.info("Session %s: instrument lookup failed for %s — %s",
                     session.id, market, exc)
            return False

        try:
            contract_value = Decimal(instrument["ctVal"]) * Decimal(instrument.get("ctMult") or "1")
            min_sz = Decimal(instrument["minSz"])
            leverage = Decimal(session.config.leverage)
        except (KeyError, TypeError, ArithmeticError) as exc:
            log.info("Session %s: bad instrument data for %s — %s",
                     session.id, market, exc)
            return False

        # Risk-based position sizing:
        # contracts = risk_amount / (price_distance * contract_value)
        price_distance = abs(entry - sl)
        if price_distance <= 0 or contract_value <= 0:
            return False

        contracts = (risk_amount / (price_distance * contract_value)).to_integral_value(
            rounding=ROUND_DOWN
        )
        if contracts < min_sz:
            log.info("Session %s: %s risk budget too small for min contracts %s",
                     session.id, market, min_sz)
            return False

        # Margin cap check
        notional = contracts * contract_value * entry
        initial_margin = notional / leverage
        margin_cap = balance * MARGIN_UTILISATION_CAP / Decimal("100")
        if initial_margin > margin_cap:
            log.info("Session %s: %s margin %.2f exceeds cap %.2f",
                     session.id, market, initial_margin, margin_cap)
            return False

        # Determine position side
        try:
            account = await client.account_config()
        except ExchangeError as exc:
            log.info("Session %s: account config failed — %s", session.id, exc)
            return False

        if account.get("posMode") == "long_short_mode":
            position_side = signal.action
        else:
            position_side = "net"
        exchange_side = "buy" if signal.action == "long" else "sell"

        client_order_id = f"auto{session.id}x{int(datetime.now(UTC).timestamp())}"[:28]
        intent = OrderIntent(
            session.user_id, market,
            Side.BUY if exchange_side == "buy" else Side.SELL,
            notional,
            client_order_id,
        )
        if not self.database.reserve(intent):
            log.info("Session %s: reservation refused for %s", session.id, market)
            return False

        submission_started = False
        try:
            await client.set_leverage(market, leverage, position_side)
            submission_started = True
            accepted = await client.place_market_swap(
                inst_id=market, side=exchange_side, position_side=position_side,
                contracts=contracts, client_order_id=client_order_id,
                take_profit=tp, stop_loss=sl,
            )
            order_id = accepted.get("ordId")
            if not order_id:
                raise ExchangeError("OKX accepted request without ordId")

            detail = await client.order_detail(market, order_id)
            status = (detail or {}).get("state", "live").upper()
            filled_price = Decimal((detail or {}).get("avgPx") or str(entry))
            filled_contracts = Decimal((detail or {}).get("accFillSz") or str(contracts))
            self.database.finalize(
                client_order_id,
                OrderResult(order_id, status, filled_price, filled_contracts),
            )
        except OrderRejected as exc:
            self.database.cancel_reservation(client_order_id)
            log.info("Session %s: OKX rejected %s — %s", session.id, market, exc)
            return False
        except Exception as exc:
            if not submission_started:
                self.database.cancel_reservation(client_order_id)
            log.warning("Session %s: order submission failed for %s — %s",
                        session.id, market, exc)
            return False

        # Notify
        try:
            await self.notifier.send(
                session.user_id,
                f"🤖 Auto entry — {signal.action.upper()} {market}\n"
                f"Entry ~{filled_price:.4f} · SL {sl:.4f} · TP {tp:.4f}\n"
                f"Contracts: {filled_contracts} · Leverage: {leverage}×\n"
                f"Risk: ~{risk_amount:.2f} USDT\n"
                f"Session #{session.id}",
            )
        except Exception:
            pass

        log.info("Session %s: ENTERED %s %s x%s at %s",
                 session.id, signal.action, market, filled_contracts, filled_price)
        return True

    # ------------------------------------------------------------------
    # Session end
    # ------------------------------------------------------------------

    async def _end_session(self, client, session: SessionState,
                           reason: EndReason, balance: Decimal) -> None:
        log.info("Session %s ending: %s (balance=%.2f)",
                 session.id, reason.value, balance)

        # Close every open position
        closed_any = False
        try:
            positions = await client.swap_positions()
        except ExchangeError:
            positions = []

        for pos in positions:
            inst_id = pos.get("instId")
            pos_side = pos.get("posSide") or "net"
            contracts = abs(Decimal(pos.get("pos") or "0"))
            if contracts <= 0:
                continue
            if pos_side == "long":
                close_side = "sell"
            elif pos_side == "short":
                close_side = "buy"
            else:
                close_side = "sell" if Decimal(pos.get("pos")) > 0 else "buy"

            close_key = f"autoend{session.id}{int(datetime.now(UTC).timestamp())}"[:28]
            try:
                accepted = await client.close_market_swap(
                    inst_id=inst_id, side=close_side, position_side=pos_side,
                    contracts=contracts, client_order_id=close_key,
                )
                detail = await client.order_detail(inst_id, accepted.get("ordId", ""))
                status = (detail or {}).get("state", "live").upper()
                fill = Decimal((detail or {}).get("avgPx") or pos.get("markPx") or "0")
                filled = Decimal((detail or {}).get("accFillSz") or "0")
                pnl = Decimal((detail or {}).get("pnl") or "0")
                self.database.reconcile_exchange_order(close_key, status, fill, filled, pnl)
                closed_any = True
            except Exception as exc:
                log.warning("Session %s: close %s failed — %s", session.id, inst_id, exc)

        # Refresh final balance if any positions were closed
        if closed_any:
            await asyncio.sleep(2)
            try:
                balance = await client.account_balance()
            except Exception:
                pass

        self.session_manager.end_session(session.id, reason, balance)

        # Notify
        try:
            await self.notifier.send(
                session.user_id,
                self._summary_message(session, reason, balance),
            )
        except Exception:
            pass

    @staticmethod
    def _summary_message(session: SessionState, reason: EndReason,
                         balance: Decimal) -> str:
        pnl = balance - session.starting_balance
        sign = "+" if pnl >= 0 else "-"
        reason_lines = {
            EndReason.TARGET: "🎯 Target profit hit",
            EndReason.MAX_LOSS: "🛑 Max loss hit",
            EndReason.SAFETY_STOP: "🛡️ Safety floor hit",
            EndReason.EXPIRED: "⏱️ Duration expired",
            EndReason.USER_STOP: "⏹️ Stopped by user",
            EndReason.ERROR: "⚠️ Stopped due to error",
        }
        return (
            f"{reason_lines.get(reason, 'Session ended')}\n\n"
            f"Session #{session.id} · {session.config.mode}\n"
            f"Started: {session.starting_balance:.2f} USDT\n"
            f"Ended:   {balance:.2f} USDT\n"
            f"P&L:     {sign}{abs(pnl):.2f} USDT\n\n"
            f"Trades: {session.trades_count}  "
            f"(W {session.wins_count} / L {session.losses_count})"
        )
