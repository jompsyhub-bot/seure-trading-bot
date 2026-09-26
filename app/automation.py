
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .exchange import ExchangeError, OKXDemoClient, OrderRejected
from .models import OrderIntent, OrderResult, Side
from .position_manager import Action as PMAction
from .position_manager import evaluate as pm_evaluate
from .risk import (
    evaluate_entry,
    pick_safe_leverage,
    compute_contracts,
    minimum_viable_balance,
    MIN_ACCOUNT_BALANCE,
)
from .sessions import (
    EndReason,
    SessionState,
    TrackedPosition,
    should_end_session,
)
from .strategies.base import Candle
from .strategies.daily_pullback import DailyPullbackStrategy

log = logging.getLogger(__name__)


DEFAULT_AUTO_MARKETS = [
    "ETH-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SOL-USDT-SWAP",
]

SCAN_INTERVAL_SECONDS = 60


def _strategy_for(strategy_name: str):
    if strategy_name == "daily_pullback":
        return DailyPullbackStrategy()
    raise ValueError(f"Unknown strategy: {strategy_name}")


def _candle_from_exchange(row: dict) -> Candle:
    return Candle(
        timestamp=int(row["timestamp"]),
        open=float(row["open"]), high=float(row["high"]),
        low=float(row["low"]), close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _algo_clordid_for(client_order_id: str) -> str:
    """Must match the attachAlgoClOrdId sent in place_market_swap."""
    return f"tp{client_order_id}"[:32]


class AutoTradingWorker:
    def __init__(self, session_manager, database, vault, notifier):
        self.session_manager = session_manager
        self.database = database
        self.vault = vault
        self.notifier = notifier
        self.running = True

    async def run(self) -> None:
        log.info("AutoTradingWorker started")
        await asyncio.sleep(5)
        try:
            self.session_manager.ensure_schema()
        except Exception as exc:
            log.warning("worker ensure_schema failed: %s", exc)
        try:
            await self._startup_repair()
        except Exception as exc:
            log.warning("Startup repair failed: %s", exc)
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

    # --------------------------------------------------------------
    # Startup repair
    # --------------------------------------------------------------

    async def _startup_repair(self) -> None:
        log.info("Running startup repair for unreconciled session positions...")
        try:
            stuck = self.session_manager.list_sessions_with_open_positions()
        except Exception as exc:
            log.warning("Could not list stuck sessions: %s", exc)
            return
        if not stuck:
            log.info("No stuck sessions found.")
            return
        log.info("Found %d session(s) with unreconciled positions", len(stuck))
        for session_id, user_id in stuck:
            try:
                await self._repair_one_session(session_id, user_id)
            except Exception as exc:
                log.warning("Startup repair failed for session %s: %s",
                            session_id, exc)
        log.info("Startup repair complete.")

    async def _repair_one_session(self, session_id: int, user_id: int) -> None:
        session = self.session_manager.get_by_id(session_id)
        if not session:
            return
        creds = self.database.exchange_credentials(user_id)
        if not creds or creds.get("provider") != "okx":
            log.info("Session %s: no OKX credentials, skipping", session_id)
            return
        client = OKXDemoClient(
            self.vault.decrypt(creds["encrypted_api_key"]),
            self.vault.decrypt(creds["encrypted_api_secret"]),
            self.vault.decrypt(creds["encrypted_passphrase"]),
        )
        try:
            await self._reconcile_tracked_positions(client, session)
            trades, wins, losses = self.session_manager.recount_session_from_positions(session_id)
            log.info("Repaired session %s: trades=%d wins=%d losses=%d",
                     session_id, trades, wins, losses)
        finally:
            await client.close()

    # --------------------------------------------------------------
    # Main processing
    # --------------------------------------------------------------

    async def _process_session(self, session: SessionState) -> None:
        user_id = session.user_id
        creds = self.database.exchange_credentials(user_id)
        if not creds or creds.get("provider") != "okx":
            return

        client = OKXDemoClient(
            self.vault.decrypt(creds["encrypted_api_key"]),
            self.vault.decrypt(creds["encrypted_api_secret"]),
            self.vault.decrypt(creds["encrypted_passphrase"]),
        )
        try:
            # 1. Reconcile positions that closed since last scan
            await self._reconcile_tracked_positions(client, session)
            session = self.session_manager.get_by_id(session.id) or session

            # 2. Balance + end conditions
            balance = await client.account_balance()
            self.session_manager.update_balance(session.id, balance)

            reason = should_end_session(session, balance)
            if reason:
                await self._end_session(client, session, reason, balance)
                return

            # 3. Manage existing positions (SL moves, closes)
            await self._manage_open_positions(client, session)

            # 4. Look for new entries
            try:
                positions = await client.swap_positions()
            except ExchangeError as exc:
                log.info("Session %s: positions unavailable (%s)", session.id, exc)
                return

            open_inst_ids = {p.get("instId") for p in positions}
            markets = self._resolve_markets(session)
            if not markets:
                return

            strategy = _strategy_for(session.config.strategy)
            for market in markets:
                if market in open_inst_ids:
                    continue

                decision = evaluate_entry(
                    balance=balance,
                    session_risk_pct=session.config.risk_per_trade_pct,
                    current_exposure=self.session_manager.session_open_exposure(session.id),
                    open_positions_count=len(positions),
                    loss_streak=session.loss_streak,
                )
                if not decision.allowed:
                    log.debug("Session %s: entry skipped — %s", session.id, decision.reason)
                    break

                try:
                    signal = await self._evaluate_market(client, strategy, market)
                except Exception as exc:
                    log.debug("Session %s: evaluate %s failed — %s", session.id, market, exc)
                    continue

                if not signal.is_entry or not signal.is_valid():
                    continue

                placed = await self._place_entry(
                    client, session, signal, balance, decision.risk_amount
                )
                if placed:
                    try:
                        positions = await client.swap_positions()
                        open_inst_ids = {p.get("instId") for p in positions}
                    except Exception:
                        pass

            self.session_manager.touch_scan(session.id)
        finally:
            await client.close()

    # --------------------------------------------------------------
    # Position management (breakeven, trailing, time stop)
    # --------------------------------------------------------------

    async def _manage_open_positions(self, client, session: SessionState) -> None:
        try:
            tracked = self.session_manager.list_open_positions(session.id)
        except Exception as exc:
            log.warning("Session %s: list positions failed — %s", session.id, exc)
            return
        if not tracked:
            return

        for tp in tracked:
            if tp.initial_sl is None or tp.current_sl is None:
                continue

            try:
                candles = await client.candles(tp.inst_id, "4H", 30)
            except ExchangeError:
                continue
            if not candles:
                continue

            last = candles[-1]
            last_close = Decimal(str(last["close"]))
            last_ts = int(last["timestamp"])

            # Need the original TP. Reconstruct from initial SL distance.
            r_distance = abs(tp.entry_price - tp.initial_sl)
            if tp.pos_side in ("long",):
                original_tp = tp.entry_price + r_distance * Decimal("2.5")
            elif tp.pos_side in ("short",):
                original_tp = tp.entry_price - r_distance * Decimal("2.5")
            else:
                original_tp = tp.entry_price + r_distance * Decimal("2.5")

            try:
                atr_series_candles = [_candle_from_exchange(c) for c in candles]
                atr = self._atr(atr_series_candles, 14)
            except Exception:
                atr = Decimal("0")
            if atr <= 0:
                continue

            bars_held = max(1, (int(datetime.now(UTC).timestamp() * 1000) - int(tp.opened_at.timestamp() * 1000)) // (4 * 3600 * 1000))

            action = pm_evaluate(
                side=tp.pos_side if tp.pos_side != "net" else "long",
                entry_price=tp.entry_price,
                initial_sl=tp.initial_sl,
                original_tp=original_tp,
                current_sl=tp.current_sl,
                last_closed_candle_close=last_close,
                last_closed_candle_ts=last_ts,
                last_action_candle_ts=tp.last_action_candle_ts,
                atr=atr,
                bars_held=bars_held,
            )

            if action.action == PMAction.MOVE_SL and action.new_sl is not None:
                algo_id = tp.algo_clordid or _algo_clordid_for(tp.client_order_id)
                try:
                    await client.amend_algo_sl(
                        inst_id=tp.inst_id,
                        algo_clordid=algo_id,
                        new_sl_trigger=action.new_sl,
                    )
                    self.session_manager.update_position_sl(
                        tp.client_order_id,
                        action.new_sl,
                        last_ts,
                    )
                    log.info("Session %s: %s SL -> %s (%s)",
                             session.id, tp.inst_id, action.new_sl, action.reason)
                except Exception as exc:
                    log.warning("Session %s: amend SL failed for %s — %s",
                                session.id, tp.inst_id, exc)

            elif action.action == PMAction.CLOSE:
                log.info("Session %s: closing %s — %s",
                         session.id, tp.inst_id, action.reason)
                await self._close_tracked_position(client, session, tp)

    async def _close_tracked_position(self, client, session: SessionState,
                                      tp: TrackedPosition) -> None:
        try:
            live = await client.swap_positions()
        except ExchangeError:
            live = []
        match = next(
            (p for p in live
             if p.get("instId") == tp.inst_id and (p.get("posSide") or "net") == tp.pos_side),
            None,
        )
        if not match:
            self.session_manager.mark_position_closed(tp.client_order_id, None)
            return

        contracts = abs(Decimal(match.get("pos") or "0"))
        if contracts <= 0:
            return

        if tp.pos_side == "long":
            close_side = "sell"
        elif tp.pos_side == "short":
            close_side = "buy"
        else:
            close_side = "sell" if Decimal(match.get("pos")) > 0 else "buy"

        close_key = f"pm{tp.client_order_id[:22]}"[:28]
        try:
            accepted = await client.close_market_swap(
                inst_id=tp.inst_id, side=close_side,
                position_side=tp.pos_side, contracts=contracts,
                client_order_id=close_key,
            )
            detail = await client.order_detail(tp.inst_id, accepted.get("ordId", ""))
            status = (detail or {}).get("state", "live").upper()
            fill = Decimal((detail or {}).get("avgPx") or match.get("markPx") or "0")
            filled = Decimal((detail or {}).get("accFillSz") or "0")
            pnl = Decimal((detail or {}).get("pnl") or "0")
            self.database.reconcile_exchange_order(close_key, status, fill, filled, pnl)
            newly_closed = self.session_manager.mark_position_closed(tp.client_order_id, pnl)
            if newly_closed:
                try:
                    bal = await client.account_balance()
                except Exception:
                    bal = session.current_balance
                self.session_manager.record_trade_result(
                    session.id, is_win=(pnl > 0) if pnl != 0 else None, new_balance=bal
                )
        except Exception as exc:
            log.warning("Session %s: close %s failed — %s", session.id, tp.inst_id, exc)

    @staticmethod
    def _atr(candles, period=14) -> Decimal:
        if len(candles) < period + 1:
            return Decimal("0")
        trs = []
        for i in range(1, len(candles)):
            c, prev = candles[i], candles[i - 1]
            tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
            trs.append(tr)
        avg = sum(trs[-period:]) / period
        return Decimal(str(avg))

    # --------------------------------------------------------------
    # Reconciliation
    # --------------------------------------------------------------

    async def _fetch_position_pnl(self, client, tp: TrackedPosition):
        if tp.order_id:
            try:
                detail = await client.order_detail(tp.inst_id, tp.order_id)
                if detail:
                    raw = detail.get("pnl") or detail.get("realizedPnl")
                    state = detail.get("state")
                    return (Decimal(str(raw)) if raw is not None else None, state)
            except Exception:
                pass
        try:
            detail = await client.order_detail_by_clordid(tp.inst_id, tp.client_order_id)
            if detail:
                raw = detail.get("pnl") or detail.get("realizedPnl")
                state = detail.get("state")
                return (Decimal(str(raw)) if raw is not None else None, state)
        except Exception:
            pass
        return (None, None)

    async def _reconcile_tracked_positions(self, client, session: SessionState) -> None:
        try:
            tracked = self.session_manager.list_open_positions(session.id)
        except Exception:
            return
        if not tracked:
            return

        try:
            live = await client.swap_positions()
        except ExchangeError:
            return
        live_keys = {(p.get("instId"), p.get("posSide") or "net") for p in live}

        for tp in tracked:
            if (tp.inst_id, tp.pos_side) in live_keys:
                continue

            pnl, okx_state = await self._fetch_position_pnl(client, tp)
            if okx_state and okx_state.lower() in {"live", "partially_filled", "new"}:
                continue

            newly_closed = self.session_manager.mark_position_closed(tp.client_order_id, pnl)
            if not newly_closed:
                continue

            try:
                bal = await client.account_balance()
            except Exception:
                bal = session.current_balance

            is_win: Optional[bool] = None
            if pnl is not None:
                is_win = pnl > 0

            self.session_manager.record_trade_result(session.id, is_win, bal)
            log.info("Session %s: reconciled %s %s (pnl=%s, win=%s)",
                     session.id, tp.inst_id, tp.pos_side, pnl, is_win)

    # --------------------------------------------------------------
    # Market selection + strategy
    # --------------------------------------------------------------

    def _resolve_markets(self, session: SessionState) -> list[str]:
        raw = (session.config.markets or "auto").strip()
        if raw == "auto":
            return list(DEFAULT_AUTO_MARKETS)
        return [m.strip() for m in raw.split(",") if m.strip()]

    async def _evaluate_market(self, client, strategy, market: str):
        c4h_raw = await client.candles(market, "4H", 200)
        c1d_raw = await client.candles(market, "1Dutc", 100)
        if len(c4h_raw) < 60 or len(c1d_raw) < 60:
            return None
        c4h = [_candle_from_exchange(r) for r in c4h_raw]
        c1d = [_candle_from_exchange(r) for r in c1d_raw]
        return strategy.evaluate(market, {"4h": c4h, "1d": c1d})

    # --------------------------------------------------------------
    # Entry
    # --------------------------------------------------------------

    async def _place_entry(self, client, session, signal, balance: Decimal,
                           risk_amount: Decimal) -> bool:
        market = signal.market
        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit

        # Instrument lookup
        try:
            instrument = await client.swap_instrument(market)
        except ExchangeError as exc:
            log.info("Session %s: instrument lookup failed — %s", session.id, exc)
            return False

        try:
            contract_value = Decimal(instrument["ctVal"]) * Decimal(instrument.get("ctMult") or "1")
            min_sz = Decimal(instrument["minSz"])
            exchange_max_lev = int(instrument.get("lever") or 1)
        except (KeyError, TypeError, ArithmeticError) as exc:
            log.info("Session %s: bad instrument data — %s", session.id, exc)
            return False

        # Contracts from risk budget
        contracts = compute_contracts(
            risk_amount=risk_amount,
            entry_price=entry,
            stop_price=sl,
            contract_value=Decimal(instrument["ctVal"]),
            contract_multiplier=Decimal(instrument.get("ctMult") or "1"),
            min_contracts=min_sz,
        )

        if contracts is None:
            # Not enough capital for the exchange minimum at this risk level.
            # Compute the balance the user would need to trade this market.
            needed = minimum_viable_balance(
                entry_price=entry,
                stop_price=sl,
                contract_value=Decimal(instrument["ctVal"]),
                contract_multiplier=Decimal(instrument.get("ctMult") or "1"),
                min_contracts=min_sz,
                risk_pct=session.config.risk_per_trade_pct,
            )
            log.info(
                "Session %s: %s needs at least $%s balance to trade at %s%% risk",
                session.id, market, needed, session.config.risk_per_trade_pct,
            )
            return False

        # Notional from contracts
        notional = contracts * contract_value * entry

        # Pick safe leverage: user choice capped by exchange, liquidation safety,
        # and available margin.
        user_choice = session.config.leverage
        sl_distance_pct = (abs(entry - sl) / entry).quantize(Decimal("0.00001")) if entry > 0 else Decimal("0")
        safe_leverage = pick_safe_leverage(
            user_max_leverage=user_choice,
            exchange_max_leverage=exchange_max_lev,
            stop_distance_pct=sl_distance_pct,
            available_margin=balance,
            notional_needed=notional,
        )
        if safe_leverage is None:
            log.info(
                "Session %s: no safe leverage fits %s at notional $%s (balance $%s)",
                session.id, market, notional.quantize(Decimal("0.01")), balance,
            )
            return False

        leverage = Decimal(safe_leverage)

        # Position side + exchange side
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
        algo_clordid = _algo_clordid_for(client_order_id)

        intent = OrderIntent(
            session.user_id, market,
            Side.BUY if exchange_side == "buy" else Side.SELL,
            notional, client_order_id,
        )
        if not self.database.reserve(intent):
            log.info("Session %s: reservation refused", session.id)
            return False

        submission_started = False
        order_id: Optional[str] = None
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

            try:
                self.session_manager.track_position(
                    session_id=session.id,
                    inst_id=market,
                    pos_side=position_side,
                    entry_price=filled_price,
                    contracts=filled_contracts,
                    client_order_id=client_order_id,
                    order_id=order_id,
                    initial_sl=sl,
                    algo_clordid=algo_clordid,
                )
            except Exception as exc:
                log.warning("Session %s: could not track position — %s", session.id, exc)

        except OrderRejected as exc:
            self.database.cancel_reservation(client_order_id)
            log.info("Session %s: OKX rejected — %s", session.id, exc)
            return False
        except Exception as exc:
            if not submission_started:
                self.database.cancel_reservation(client_order_id)
            log.warning("Session %s: submission failed — %s", session.id, exc)
            return False

        try:
            await self.notifier.send(
                session.user_id,
                f"🤖 Auto entry — {signal.action.upper()} {market}\n"
                f"Entry ~{filled_price:.4f} · SL {sl:.4f} · TP {tp:.4f}\n"
                f"Contracts: {filled_contracts} · Leverage: {leverage}×\n"
                f"Risk: ~{risk_amount:.2f} USDT · Session #{session.id}",
            )
        except Exception:
            pass

        log.info(
            "Session %s: ENTERED %s %s x%s at %s (lev %sx, order %s)",
            session.id, signal.action, market, filled_contracts,
            filled_price, leverage, order_id,
        )
        return True

    # --------------------------------------------------------------
    # End session
    # --------------------------------------------------------------

    async def _end_session(self, client, session: SessionState,
                           reason: EndReason, balance: Decimal) -> None:
        log.info("Session %s ending: %s (balance=%.2f)",
                 session.id, reason.value, balance)

        await self._reconcile_tracked_positions(client, session)
        session = self.session_manager.get_by_id(session.id) or session

        try:
            tracked = self.session_manager.list_open_positions(session.id)
        except Exception:
            tracked = []

        closed_any = False
        for tp in tracked:
            await self._close_tracked_position(client, session, tp)
            closed_any = True

        if closed_any:
            await asyncio.sleep(2)
            try:
                balance = await client.account_balance()
            except Exception:
                pass

        self.session_manager.end_session(session.id, reason, balance)
        final = self.session_manager.get_by_id(session.id) or session

        try:
            session_pnl = self.session_manager.session_realized_pnl(session.id)
        except Exception:
            session_pnl = Decimal("0")

        try:
            await self.notifier.send(
                session.user_id,
                self._summary_message(final, reason, balance, session_pnl),
            )
        except Exception:
            pass

    @staticmethod
    def _summary_message(session: SessionState, reason: EndReason,
                         balance: Decimal, session_pnl: Decimal) -> str:
        account_change = balance - session.starting_balance
        reason_lines = {
            EndReason.TARGET: "🎯 Target profit hit",
            EndReason.MAX_LOSS: "🛑 Max loss hit",
            EndReason.SAFETY_STOP: "🛡️ Safety floor hit",
            EndReason.EXPIRED: "⏱️ Duration expired",
            EndReason.USER_STOP: "⏹️ Stopped by user",
            EndReason.ERROR: "⚠️ Stopped due to error",
        }
        sp_sign = "+" if session_pnl >= 0 else "-"
        ac_sign = "+" if account_change >= 0 else "-"
        return (
            f"{reason_lines.get(reason, 'Session ended')}\n\n"
            f"Session #{session.id} · {session.config.mode}\n\n"
            f"🤖 Session P&L:  {sp_sign}${abs(session_pnl):.2f} USDT\n"
            f"   ({session.trades_count} session trades: "
            f"W {session.wins_count} / L {session.losses_count})\n\n"
            f"📊 Account change: {ac_sign}${abs(account_change):.2f} USDT\n"
            f"   Started: {session.starting_balance:.2f}\n"
            f"   Ended:   {balance:.2f}\n"
            f"   (includes any manual trades you made)"
        )
