"""Session-aware auto trading worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import aiohttp

from .exchange import (
    ExchangeError,
    OKXClient,
    OKXDemoClient,
    OKXLiveClient,
    OrderRejected,
)
from .models import OrderIntent, OrderResult, Side
from .position_manager import Action as PMAction
from .position_manager import evaluate as pm_evaluate
from .risk import (
    evaluate_entry,
    pick_safe_leverage,
    rules_for,
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
from .strategies.daily_breakout import DailyBreakoutStrategy

log = logging.getLogger(__name__)

DEFAULT_AUTO_MARKETS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SOL-USDT-SWAP",
    "BNB-USDT-SWAP",
    "XRP-USDT-SWAP",
    "ADA-USDT-SWAP",
    "LTC-USDT-SWAP",
    "LINK-USDT-SWAP",
]

SCAN_INTERVAL_SECONDS = 60

TRANSIENT_ERRORS = (
    ExchangeError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    RuntimeError,
)


def _strategies_for(strategy_field: str):
    names = [s.strip() for s in (strategy_field or "").split(",") if s.strip()]
    out = []
    for name in names:
        if name == "daily_breakout":
            out.append((name, DailyBreakoutStrategy()))
        elif name == "daily_pullback":
            out.append((name, DailyPullbackStrategy()))
        else:
            log.warning("Unknown strategy %r in session config", name)
    return out


def _candle_from_exchange(row: dict) -> Candle:
    return Candle(
        timestamp=int(row["timestamp"]),
        open=float(row["open"]), high=float(row["high"]),
        low=float(row["low"]), close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _algo_clordid_for(client_order_id: str) -> str:
    return f"tp{client_order_id}"[:32]


class AutoTradingWorker:
    def __init__(self, session_manager, database, vault, notifier, settings=None):
        self.session_manager = session_manager
        self.database = database
        self.vault = vault
        self.notifier = notifier
        self.settings = settings
        self.running = True

    def _client_for(self, user_id: int, environment: str) -> OKXClient:
        creds = self.database.exchange_credentials(user_id)
        if not creds or creds.get("provider") != "okx":
            raise ExchangeError("No OKX credentials for user")

        api_key = self.vault.decrypt(creds["encrypted_api_key"])
        api_secret = self.vault.decrypt(creds["encrypted_api_secret"])
        passphrase = self.vault.decrypt(creds["encrypted_passphrase"])

        if environment == "live":
            if self.settings is None or not getattr(self.settings, "allow_live_trading", False):
                raise ExchangeError("Live session refused: ALLOW_LIVE_TRADING is disabled.")
            return OKXLiveClient(api_key, api_secret, passphrase)

        return OKXDemoClient(api_key, api_secret, passphrase)

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

    async def _startup_repair(self) -> None:
        log.info("Running startup repair...")
        try:
            stuck = self.session_manager.list_sessions_with_open_positions()
        except Exception as exc:
            return
        if not stuck:
            return
        for session_id, user_id in stuck:
            try:
                await self._repair_one_session(session_id, user_id)
            except Exception:
                pass

    async def _repair_one_session(self, session_id: int, user_id: int) -> None:
        session = self.session_manager.get_by_id(session_id)
        if not session:
            return
        try:
            client = self._client_for(user_id, session.config.environment)
        except ExchangeError:
            return
        try:
            await self._reconcile_tracked_positions(client, session)
            self.session_manager.recount_session_from_positions(session_id)
        finally:
            await client.close()

    async def _process_session(self, session: SessionState) -> None:
        user_id = session.user_id
        env = session.config.environment

        if env == "live":
            if self.settings is None or not getattr(self.settings, "allow_live_trading", False):
                self.session_manager.end_session(session.id, EndReason.ERROR, session.current_balance)
                return

        try:
            client = self._client_for(user_id, env)
        except ExchangeError as exc:
            return

        try:
            await self._reconcile_tracked_positions(client, session)
            session = self.session_manager.get_by_id(session.id) or session

            balance = await client.account_balance()
            self.session_manager.update_balance(session.id, balance)

            reason = should_end_session(session, balance)
            if reason:
                await self._end_session(client, session, reason, balance)
                return

            await self._manage_open_positions(client, session)

            try:
                positions = await client.swap_positions()
            except TRANSIENT_ERRORS:
                return

            open_inst_ids = {p.get("instId") for p in positions}
            markets = self._resolve_markets(session)
            if not markets:
                return

            strategies = _strategies_for(session.config.strategy)
            if not strategies:
                return

            strategy_names = ",".join(name for name, _ in strategies)
            long_signals = 0
            short_signals = 0
            entries_placed = 0
            skips = 0
            skip_reasons: list[str] = []

            tier_max_concurrent = rules_for(balance).max_concurrent
            placed_this_scan = 0

            for market in markets:
                if market in open_inst_ids:
                    continue

                current_open = len(positions) + placed_this_scan
                if current_open >= tier_max_concurrent:
                    break

                current_exposure = self.session_manager.session_open_exposure(session.id)

                decision = evaluate_entry(
                    balance=balance,
                    session_risk_pct=(session.config.risk_per_trade_pct if session.config.risk_unit != "usdt" else None),
                    session_risk_usdt=(session.config.risk_per_trade_usdt if session.config.risk_unit == "usdt" else None),
                    current_exposure=current_exposure,
                    open_positions_count=current_open,
                    loss_streak=session.loss_streak,
                    last_loss_at=session.last_loss_at,
                )
                if not decision.allowed:
                    skip_reasons.append(f"{market}: risk ({decision.reason})")
                    break

                primary_signal = None
                entry_signal = None
                entry_strategy_name = None

                for strat_name, strat_obj in strategies:
                    try:
                        sig = await self._evaluate_market(client, strat_obj, market)
                    except TRANSIENT_ERRORS:
                        continue

                    if sig is None:
                        continue
                    if primary_signal is None:
                        primary_signal = sig
                    if sig.is_entry and sig.is_valid():
                        entry_signal = sig
                        entry_strategy_name = strat_name
                        break

                if entry_signal is None:
                    skips += 1
                    if primary_signal is not None:
                        skip_reasons.append(f"{market}: {primary_signal.reason}")
                    else:
                        skip_reasons.append(f"{market}: no signal")
                    continue

                if entry_signal.action == "long":
                    long_signals += 1
                else:
                    short_signals += 1

                placed = await self._place_entry(
                    client, session, entry_signal, balance, decision.risk_amount
                )
                if placed:
                    entries_placed += 1
                    placed_this_scan += 1

            log.info(
                "Session %s: scan done — env=%s strategies=%s markets=%d "
                "long_signals=%d short_signals=%d entries=%d skips=%d",
                session.id, env, strategy_names, len(markets),
                long_signals, short_signals, entries_placed, skips,
            )
            if skip_reasons:
                log.info("Session %s: skip detail — %s", session.id, " | ".join(skip_reasons[:6]))

            self.session_manager.touch_scan(session.id)
        finally:
            await client.close()

    async def _manage_open_positions(self, client, session: SessionState) -> None:
        try:
            tracked = self.session_manager.list_open_positions(session.id)
        except Exception:
            return
        if not tracked:
            return

        async def fetch_candles(tp: TrackedPosition):
            try:
                return await client.candles(tp.inst_id, "4H", 30)
            except TRANSIENT_ERRORS:
                return None

        candle_results = await asyncio.gather(*(fetch_candles(tp) for tp in tracked))
        now_ms = int(datetime.now(UTC).timestamp() * 1000)

        for tp, candles in zip(tracked, candle_results, strict=True):
            if tp.initial_sl is None or tp.current_sl is None:
                continue
            if not candles:
                continue

            last = candles[-1]
            last_close = Decimal(str(last["close"]))
            last_ts = int(last["timestamp"])

            side = tp.direction
            if side not in ("long", "short"):
                continue

            if tp.take_profit is not None:
                original_tp = tp.take_profit
            else:
                r_distance = abs(tp.entry_price - tp.initial_sl)
                original_tp = tp.entry_price + r_distance * Decimal("2.5") if side == "long" else tp.entry_price - r_distance * Decimal("2.5")

            atr_series_candles = [_candle_from_exchange(c) for c in candles]
            atr = self._atr(atr_series_candles, 14)
            if atr <= 0:
                continue

            opened_ms = int(tp.opened_at.timestamp() * 1000)
            bars_held = max(1, (now_ms - opened_ms) // (4 * 3600 * 1000))

            action = pm_evaluate(
                side=side, entry_price=tp.entry_price, initial_sl=tp.initial_sl,
                original_tp=original_tp, current_sl=tp.current_sl,
                last_closed_candle_close=last_close, last_closed_candle_ts=last_ts,
                last_action_candle_ts=tp.last_action_candle_ts, atr=atr, bars_held=bars_held,
            )

            if action.action == PMAction.MOVE_SL and action.new_sl is not None:
                algo_id = tp.algo_clordid or _algo_clordid_for(tp.client_order_id)
                try:
                    await client.amend_algo_sl(inst_id=tp.inst_id, algo_clordid=algo_id, new_sl_trigger=action.new_sl)
                    self.session_manager.update_position_sl(tp.client_order_id, action.new_sl, last_ts)
                except TRANSIENT_ERRORS:
                    pass

            elif action.action == PMAction.CLOSE:
                await self._close_tracked_position(client, session, tp)

    async def _close_tracked_position(self, client, session: SessionState, tp: TrackedPosition) -> None:
        try:
            live = await client.swap_positions()
        except TRANSIENT_ERRORS:
            live = []
        match = next((p for p in live if p.get("instId") == tp.inst_id and (p.get("posSide") or "net") == tp.pos_side), None)
        if not match:
            self.session_manager.mark_position_closed(tp.client_order_id, None)
            return

        contracts = abs(Decimal(match.get("pos") or "0"))
        if contracts <= 0:
            return

        close_side = "sell" if tp.pos_side == "long" else "buy" if tp.pos_side == "short" else ("sell" if Decimal(match.get("pos")) > 0 else "buy")
        close_key = f"pm{tp.client_order_id[:22]}"[:28]
        
        try:
            accepted = await client.close_market_swap(
                inst_id=tp.inst_id, side=close_side, position_side=tp.pos_side,
                contracts=contracts, client_order_id=close_key,
            )
            detail = await client.order_detail(tp.inst_id, accepted.get("ordId", ""))
            status = (detail or {}).get("state", "live").upper()
            fill = Decimal((detail or {}).get("avgPx") or match.get("markPx") or "0")
            filled = Decimal((detail or {}).get("accFillSz") or "0")
            pnl = Decimal((detail or {}).get("pnl") or "0")

            is_win = True if pnl > 0 else False if pnl < 0 else None
            self.database.reconcile_exchange_order(close_key, status, fill, filled, pnl)
            
            if self.session_manager.mark_position_closed(tp.client_order_id, pnl):
                try:
                    bal = await client.account_balance()
                except Exception:
                    bal = session.current_balance
                self.session_manager.record_trade_result(session.id, is_win=is_win, new_balance=bal)
        except Exception as exc:
            log.warning("Session %s: close %s failed — %s", session.id, tp.inst_id, exc)

    @staticmethod
    def _atr(candles, period=14) -> Decimal:
        if len(candles) < period + 1:
            return Decimal("0")
        trs = []
        for i in range(1, len(candles)):
            c, prev = candles[i], candles[i - 1]
            trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
        return Decimal(str(sum(trs[-period:]) / period))

    async def _fetch_position_pnl(self, client, tp: TrackedPosition):
        try:
            history = await client.positions_history(tp.inst_id, limit=20)
            candidates = [row for row in history if (row.get("posSide") or "net") == tp.pos_side and row.get("realizedPnl") not in (None, "")]
            if candidates:
                candidates.sort(key=lambda r: int(r.get("uTime") or "0"), reverse=True)
                return (Decimal(str(candidates[0].get("realizedPnl"))), "closed")
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
        except TRANSIENT_ERRORS:
            return
            
        live_keys = {(p.get("instId"), p.get("posSide") or "net") for p in live}

        for tp in tracked:
            if (tp.inst_id, tp.pos_side) in live_keys:
                continue

            pnl, okx_state = await self._fetch_position_pnl(client, tp)
            if okx_state and okx_state.lower() in {"live", "partially_filled", "new"}:
                continue

            if self.session_manager.mark_position_closed(tp.client_order_id, pnl):
                try:
                    bal = await client.account_balance()
                except Exception:
                    bal = session.current_balance
                is_win = True if pnl is not None and pnl > 0 else False if pnl is not None and pnl < 0 else None
                self.session_manager.record_trade_result(session.id, is_win, bal)

    def _resolve_markets(self, session: SessionState) -> list[str]:
        raw = (session.config.markets or "auto").strip()
        return list(DEFAULT_AUTO_MARKETS) if raw == "auto" else [m.strip() for m in raw.split(",") if m.strip()]

    async def _evaluate_market(self, client, strategy, market: str):
        c4h_raw = await client.candles(market, "4H", 200)
        c1d_raw = await client.candles(market, "1Dutc", 100)
        if len(c4h_raw) < 60 or len(c1d_raw) < 60:
            return None
        c4h = [_candle_from_exchange(r) for r in c4h_raw]
        c1d = [_candle_from_exchange(r) for r in c1d_raw]
        return strategy.evaluate(market, {"4h": c4h, "1d": c1d})

    # --------------------------------------------------------------
    # Entry (Fractional Sizing)
    # --------------------------------------------------------------

    async def _place_entry(self, client, session, signal, balance: Decimal, risk_amount: Decimal) -> bool:
        market = signal.market
        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit

        try:
            instrument = await client.swap_instrument(market)
        except TRANSIENT_ERRORS:
            return False

        try:
            ct_val = Decimal(instrument["ctVal"])
            ct_mult = Decimal(instrument.get("ctMult") or "1")
            lot_sz = Decimal(instrument.get("lotSz") or "1")
            min_sz = Decimal(instrument.get("minSz") or "1")
            exchange_max_lev = int(instrument.get("lever") or 1)
        except (KeyError, TypeError, ArithmeticError):
            return False

        # --- OKX APP MATH (Fractional Sizing) ---
        price_distance = abs(entry - sl)
        if price_distance <= 0:
            return False
            
        sl_distance_pct = price_distance / entry
        ideal_notional = risk_amount / sl_distance_pct
        contract_notional = ct_val * ct_mult * entry
        
        target_contracts = ideal_notional / contract_notional
        steps = (target_contracts / lot_sz).to_integral_value(rounding=ROUND_DOWN)
        contracts = steps * lot_sz

        if contracts < min_sz:
            contracts = min_sz

        actual_notional = contracts * contract_notional
        actual_risk = actual_notional * sl_distance_pct

        user_choice = session.config.leverage
        safe_leverage = pick_safe_leverage(
            user_max_leverage=user_choice,
            exchange_max_leverage=exchange_max_lev,
            stop_distance_pct=sl_distance_pct,
            available_margin=balance,
            notional_needed=actual_notional,
        )
        if safe_leverage is None:
            return False

        leverage = Decimal(safe_leverage)
        margin_needed = actual_notional / leverage

        if margin_needed > (balance * Decimal("0.90")):
            return False

        try:
            account = await client.account_config()
        except TRANSIENT_ERRORS:
            return False

        position_side = signal.action if account.get("posMode") == "long_short_mode" else "net"
        exchange_side = "buy" if signal.action == "long" else "sell"

        client_order_id = f"auto{session.id}x{int(datetime.now(UTC).timestamp())}"[:28]
        algo_clordid = _algo_clordid_for(client_order_id)
        intent = OrderIntent(session.user_id, market, Side.BUY if exchange_side == "buy" else Side.SELL, actual_notional, client_order_id)
        
        if not self.database.reserve(intent):
            return False

        try:
            await client.set_leverage(market, leverage, position_side)
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
            
            self.database.finalize(client_order_id, OrderResult(order_id, status, filled_price, filled_contracts))
            self.session_manager.track_position(
                session_id=session.id, inst_id=market, pos_side=position_side,
                direction=signal.action, entry_price=filled_price, contracts=filled_contracts,
                take_profit=tp, client_order_id=client_order_id, order_id=order_id,
                initial_sl=sl, algo_clordid=algo_clordid, notional=actual_notional,
            )

        except OrderRejected as exc:
            self.database.cancel_reservation(client_order_id)
            log.info("Session %s: OKX rejected — %s", session.id, exc)
            return False
        except Exception as exc:
            self.database.cancel_reservation(client_order_id)
            log.warning("Session %s: submission failed — %s", session.id, exc)
            return False

        env_label = "LIVE" if session.config.environment == "live" else "DEMO"
        try:
            await self.notifier.send(
                session.user_id,
                f"🤖 Auto entry [{env_label}] — {signal.action.upper()} {market}\n"
                f"Contracts: {filled_contracts} (${margin_needed:.2f} margin)\n"
                f"Leverage: {leverage}× · Risk: ~${actual_risk:.2f}"
            )
        except Exception:
            pass

        log.info(
            "Session %s: ENTERED [%s] %s %s x%s at %s (lev %sx, margin:$%s, risk:$%s)",
            session.id, env_label, signal.action, market, filled_contracts,
            filled_price, leverage, margin_needed.quantize(Decimal("0.01")), 
            actual_risk.quantize(Decimal("0.01")),
        )
        return True

    async def _end_session(self, client, session: SessionState, reason: EndReason, balance: Decimal) -> None:
        await self._reconcile_tracked_positions(client, session)
        session = self.session_manager.get_by_id(session.id) or session

        try:
            tracked = self.session_manager.list_open_positions(session.id)
        except Exception:
            tracked = []

        for tp in tracked:
            await self._close_tracked_position(client, session, tp)

        if tracked:
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
            await self.notifier.send(session.user_id, self._summary_message(final, reason, balance, session_pnl))
        except Exception:
            pass

    @staticmethod
    def _summary_message(session: SessionState, reason: EndReason, balance: Decimal, session_pnl: Decimal) -> str:
        account_change = balance - session.starting_balance
        reason_lines = {
            EndReason.TARGET: "🎯 Target profit hit",
            EndReason.MAX_LOSS: "🛑 Max loss hit",
            EndReason.SAFETY_STOP: "🛡️ Safety floor hit",
            EndReason.EXPIRED: "⏱️ Duration expired",
            EndReason.USER_STOP: "⏹️ Stopped by user",
            EndReason.ERROR: "⚠️ Stopped due to error",
        }
        env_label = "LIVE" if session.config.environment == "live" else "DEMO"
        sp_sign = "+" if session_pnl >= 0 else "-"
        ac_sign = "+" if account_change >= 0 else "-"
        return (
            f"{reason_lines.get(reason, 'Session ended')}\n\n"
            f"Session #{session.id} · {session.config.mode} · {env_label}\n\n"
            f"🤖 Session P&L:  {sp_sign}${abs(session_pnl):.2f} USDT\n"
            f"   ({session.trades_count} session trades: W {session.wins_count} / L {session.losses_count})\n\n"
            f"📊 Account change: {ac_sign}${abs(account_change):.2f} USDT\n"
            f"   Started: {session.starting_balance:.2f}\n"
            f"   Ended:   {balance:.2f}\n"
            f"   (includes any manual trades you made)"
                                                   )
