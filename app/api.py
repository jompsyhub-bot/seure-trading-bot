from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Literal
from contextlib import asynccontextmanager
import asyncio
import time
import uuid
import logging
import json

import aiohttp

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Settings
from .database import Database
from .exchange import BybitClient, ExchangeError, OKXDemoClient, OrderRejected
from .models import OrderIntent, OrderResult, Side
from .security import CredentialVault
from .web_auth import TelegramAuthError, validate_init_data
from .automation import AutoTradingWorker
from .notifications import TelegramNotifier
from .backtest import MarketDataError, PublicBybitMarket, run_rsi_reversion
from .sessions import (
    EndReason,
    SessionConfig,
    SessionManager,
    SessionState,
    check_can_open,
    compute_risk_amount,
    should_end_session,
)

settings = Settings.from_env()
database = Database(settings.database_path, settings.database_url)
database.initialize()
session_manager = SessionManager(settings.database_url, str(settings.database_path))
session_manager.ensure_schema()
static_dir = Path(__file__).parent / "static"
log = logging.getLogger(__name__)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

DEFAULT_PRICE_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

_instruments_cache: dict = {"data": None, "ts": 0.0}
INSTRUMENTS_CACHE_TTL = 300


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    position_task = None
    ticker_task = None
    worker = None
    notifier = TelegramNotifier(settings.telegram_token)
    app.state.notifier = notifier
    app.state.futures_subscribers = {}
    app.state.futures_price_preferences = {}
    ticker_task = asyncio.create_task(monitor_okx_public_prices(), name="okx-demo-public-tickers")
    if settings.encryption_key:
        worker = AutoTradingWorker(
            session_manager=session_manager,
            database=database,
            vault=CredentialVault(settings.encryption_key),
            notifier=notifier,
        )
        task = asyncio.create_task(worker.run(), name="auto-trading-worker")
        position_task = asyncio.create_task(monitor_okx_private_events(notifier), name="okx-private-event-supervisor")
    try:
        yield
    finally:
        if worker:
            worker.running = False
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if position_task:
            position_task.cancel()
            try:
                await position_task
            except asyncio.CancelledError:
                pass
        if ticker_task:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass
        await notifier.close()


api = FastAPI(title="Safe Trading Bot API", docs_url=None, redoc_url=None, lifespan=lifespan)


class OKXDemoConnectionRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=16, max_length=512)
    passphrase: str = Field(min_length=8, max_length=256)


class PaperSettingsRequest(BaseModel):
    enabled: bool
    max_order_notional: str
    max_daily_loss: str


class AutomationProfileRequest(BaseModel):
    enabled: bool
    strategy: Literal["rsi_reversion_demo"]
    symbol: str = Field(min_length=2, max_length=32)
    max_order_notional: str
    max_trades_per_day: int = Field(ge=1, le=3)
    stop_loss_pct: str
    take_profit_pct: str


class BacktestRequest(BaseModel):
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    interval: Literal["15", "60"] = "15"
    rsi_threshold: float = Field(ge=15, le=45)
    stop_loss_pct: float = Field(ge=0.5, le=5)
    take_profit_pct: float = Field(ge=1, le=10)
    order_notional: float = Field(ge=1, le=50)


class LeverageRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32)


class FuturesPreviewRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32)
    side: Literal["long", "short"]
    leverage: int = Field(ge=1, le=100)
    margin_usdt: Decimal = Field(gt=0)


class PositionModeRequest(BaseModel):
    position_mode: Literal["net_mode", "long_short_mode"]


class FuturesDemoOrderRequest(FuturesPreviewRequest):
    take_profit: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    client_order_id: str = Field(min_length=8, max_length=28, pattern=r"^[A-Za-z0-9]+$")
    confirmation: Literal["PLACE_DEMO_ORDER"]


class ClosePositionRequest(BaseModel):
    instrument: str = Field(min_length=3, max_length=32)
    position_side: Literal["net", "long", "short"]
    confirmation: Literal["CLOSE_DEMO_POSITION"]


class StartSessionRequest(BaseModel):
    mode: Literal["day_trading"] = "day_trading"
    strategy: str = Field(default="daily_pullback", min_length=3, max_length=32)
    duration_hours: float = Field(gt=0, le=24 * 30)
    target_profit: Decimal = Field(gt=0)
    max_loss: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=10)
    safety_floor_pct: Decimal = Field(ge=50, le=95)
    leverage: int = Field(ge=1, le=100)
    markets: str = Field(default="auto", min_length=1, max_length=500)


def resolve_okx_swap_inst_id(symbol: str) -> str:
    s = (symbol or "").upper().strip().replace("/", "-")
    if not s:
        raise HTTPException(400, "Symbol is required.")
    if s.endswith("-SWAP"):
        return s
    if "-" in s:
        return f"{s}-SWAP"
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}-SWAP"
    return f"{s}-USDT-SWAP"


def watched_instruments() -> list[str]:
    prefs = getattr(api.state, "futures_price_preferences", {}) or {}
    watched = set(DEFAULT_PRICE_INSTRUMENTS)
    for inst in prefs.values():
        if isinstance(inst, str) and inst:
            watched.add(inst)
    return sorted(watched)


def telegram_user(init_data: str | None) -> dict:
    if not init_data:
        raise HTTPException(401, "Missing Telegram authorization")
    try:
        user = validate_init_data(init_data, settings.telegram_token)
        database.upsert_user(int(user["id"]), user.get("username"), user.get("first_name"))
        return user
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None


def okx_demo_client_for(user_id: int) -> OKXDemoClient:
    credentials = database.exchange_credentials(user_id)
    if not credentials or credentials.get("provider") != "okx" or credentials.get("environment") != "demo":
        raise HTTPException(400, "Connect an OKX Demo account first.")
    if not settings.encryption_key or not credentials.get("encrypted_passphrase"):
        raise HTTPException(503, "OKX Demo credentials are incomplete. Reconnect the account.")
    vault = CredentialVault(settings.encryption_key)
    return OKXDemoClient(vault.decrypt(credentials["encrypted_api_key"]), vault.decrypt(credentials["encrypted_api_secret"]), vault.decrypt(credentials["encrypted_passphrase"]))


async def publish_futures_event(user_id: int, event_type: str) -> None:
    subscribers = list(getattr(api.state, "futures_subscribers", {}).get(user_id, set()))
    for socket in subscribers:
        try:
            await socket.send_json({"type": event_type})
        except (RuntimeError, WebSocketDisconnect):
            api.state.futures_subscribers.get(user_id, set()).discard(socket)


async def publish_live_price(instrument: str, price: str) -> None:
    preferences = getattr(api.state, "futures_price_preferences", {})
    for socket, selected_instrument in list(preferences.items()):
        if selected_instrument != instrument:
            continue
        try:
            await socket.send_json({"type": "ticker", "instrument": instrument, "price": price})
        except (RuntimeError, WebSocketDisconnect):
            preferences.pop(socket, None)


async def _rest_price_poller() -> None:
    while True:
        try:
            instruments = watched_instruments()
            if instruments:
                client = OKXDemoClient("", "", "")
                try:
                    results = await asyncio.gather(
                        *(client.last_price(inst) for inst in instruments),
                        return_exceptions=True,
                    )
                finally:
                    await client.close()
                for inst, result in zip(instruments, results, strict=True):
                    if isinstance(result, Exception):
                        continue
                    await publish_live_price(inst, str(result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("REST price poll iteration failed: %s", exc)
        await asyncio.sleep(1.0)


async def monitor_okx_public_prices() -> None:
    default_instruments = set(DEFAULT_PRICE_INSTRUMENTS)
    rest_task = asyncio.create_task(_rest_price_poller(), name="okx-rest-price-poller")
    delay = 1
    try:
        while True:
            client = OKXDemoClient("", "", "")
            try:
                async for update in client.public_ticker_events(default_instruments):
                    delay = 1
                    await publish_live_price(str(update["instId"]), str(update["last"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("OKX Demo public ticker feed unavailable: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                await client.close()
    finally:
        rest_task.cancel()
        try:
            await rest_task
        except asyncio.CancelledError:
            pass


def active_position_keys(rows: list[dict]) -> set[tuple[str, str]]:
    return {
        (str(row.get("instId", "")), str(row.get("posSide", "")))
        for row in rows
        if Decimal(str(row.get("pos") or "0")) != 0
    }


async def notify_position_closed(user_id: int, notifier: TelegramNotifier, instrument: str, side: str) -> None:
    await sync_okx_order_history(user_id)
    await notifier.send(
        user_id,
        f"🔔 OKX Demo position closed\n{instrument} · {side}\n"
        "The exchange pushed this update immediately. It may have been a TP, SL, or a manual close; the ticket is reconciling the final P&L now.",
    )
    await publish_futures_event(user_id, "portfolio_changed")


async def monitor_okx_private_user(user_id: int, notifier: TelegramNotifier) -> None:
    known_positions: set[tuple[str, str]] = set()
    delay = 1
    while True:
        client: OKXDemoClient | None = None
        try:
            client = okx_demo_client_for(user_id)
            async for event in client.private_events():
                delay = 1
                channel = event.get("arg", {}).get("channel")
                rows = event.get("data") or []
                if channel == "positions":
                    changed = active_position_keys(rows)
                    if event.get("action") == "snapshot":
                        closed = known_positions - changed
                        known_positions = changed
                    else:
                        closed = set()
                        for row in rows:
                            key = (str(row.get("instId", "")), str(row.get("posSide", "")))
                            if Decimal(str(row.get("pos") or "0")) == 0:
                                if key in known_positions:
                                    known_positions.discard(key)
                                    closed.add(key)
                            else:
                                known_positions.add(key)
                    for instrument, side in closed:
                        await notify_position_closed(user_id, notifier, instrument, side)
                    await publish_futures_event(user_id, "portfolio_changed")
                elif channel == "orders":
                    if any(row.get("state") in {"filled", "canceled"} for row in rows):
                        await sync_okx_order_history(user_id)
                        await publish_futures_event(user_id, "portfolio_changed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("OKX Demo private feed unavailable for user %s: %s", user_id, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        finally:
            if client:
                await client.close()


async def monitor_okx_private_events(notifier: TelegramNotifier) -> None:
    tasks: dict[int, asyncio.Task] = {}
    try:
        while True:
            users = set(database.okx_demo_users())
            for user_id in users - tasks.keys():
                tasks[user_id] = asyncio.create_task(
                    monitor_okx_private_user(user_id, notifier), name=f"okx-demo-private-{user_id}"
                )
            for user_id in set(tasks) - users:
                tasks.pop(user_id).cancel()
            await asyncio.sleep(15)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)


def reject_unsafe_permissions(info: dict) -> None:
    if info.get("readOnly") != 0:
        raise HTTPException(400, "This Bybit API key is read-only")
    permissions = info.get("permissions", {})
    wallet_permissions = permissions.get("Wallet", [])
    forbidden = {"Withdraw", "AccountTransfer", "SubMemberTransfer", "SubMemberTransferList"}
    if forbidden.intersection(wallet_permissions):
        raise HTTPException(400, "Use a trade-only Bybit key: withdrawal and transfer permissions are forbidden")
    trade_permissions = set(permissions.get("Spot", [])) | set(permissions.get("Derivatives", [])) | set(permissions.get("ContractTrade", []))
    if not trade_permissions:
        raise HTTPException(400, "This key has no trading permission")


@api.get("/")
async def mini_app() -> FileResponse:
    return FileResponse(static_dir / "index.html", headers=NO_CACHE_HEADERS)


@api.get("/futures")
async def futures_ticket() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


@api.get("/backtest")
async def backtest_screen() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


@api.websocket("/ws/futures")
async def futures_updates(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        auth_message = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        auth_payload = json.loads(auth_message)
        if auth_payload.get("type") != "authenticate":
            raise ValueError("Missing WebSocket authentication")
        user = validate_init_data(auth_payload.get("init_data") or "", settings.telegram_token)
        user_id = int(user["id"])
    except (asyncio.TimeoutError, TelegramAuthError, KeyError, ValueError, json.JSONDecodeError):
        try:
            await websocket.close(code=1008, reason="Invalid Telegram authorization")
        except Exception:
            pass
        return
    database.upsert_user(user_id, user.get("username"), user.get("first_name"))
    subscribers = api.state.futures_subscribers.setdefault(user_id, set())
    subscribers.add(websocket)
    api.state.futures_price_preferences[websocket] = "BTC-USDT-SWAP"
    try:
        await websocket.send_json({"type": "connected", "environment": "demo", "price_instrument": "BTC-USDT-SWAP"})
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "subscribe_ticker":
                instrument = payload.get("instrument")
                if (
                    isinstance(instrument, str)
                    and 3 <= len(instrument) <= 32
                    and instrument.endswith("-SWAP")
                ):
                    api.state.futures_price_preferences[websocket] = instrument
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.discard(websocket)
        api.state.futures_price_preferences.pop(websocket, None)


@api.get("/health")
async def health() -> dict:
    return {"ok": True, "mode": settings.trading_mode, "automation_environment": "demo-only"}


@api.get("/health/okx")
async def okx_connectivity() -> dict:
    url = "https://www.okx.com/api/v5/public/instruments"
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"}) as response:
                if response.status != 200:
                    raise HTTPException(502, f"OKX returned HTTP {response.status}")
                data = await response.json(content_type=None)
        if not isinstance(data, dict) or data.get("code") != "0" or not data.get("data"):
            raise HTTPException(502, "OKX returned an unexpected public API response")
        instrument = data["data"][0]
        return {"ok": True, "exchange": "okx", "instrument": instrument["instId"], "state": instrument.get("state"), "max_leverage": instrument.get("lever")}
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(502, "OKX public API is unreachable from this host") from None


@api.get("/api/dashboard")
async def dashboard(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    result = database.dashboard(int(user["id"]))
    result["automation"] = database.automation_profile(int(user["id"]))
    return result


@api.get("/api/exchange/instruments")
async def exchange_instruments(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    provider = database.connection_provider(int(user["id"]))
    if provider != "okx":
        raise HTTPException(400, "Connect an OKX Demo account first.")

    now = time.monotonic()
    if _instruments_cache["data"] is not None and (now - _instruments_cache["ts"]) < INSTRUMENTS_CACHE_TTL:
        return _instruments_cache["data"]

    client = OKXDemoClient("", "", "")
    try:
        rows = await client.list_swap_instruments()
        result = []
        for row in rows:
            inst_id = row.get("instId")
            if not inst_id or not inst_id.endswith("-SWAP"):
                continue
            parts = inst_id.split("-")
            if len(parts) != 3:
                continue
            base, quote, _ = parts
            result.append({
                "instId": inst_id,
                "base": base,
                "quote": quote,
                "displayName": f"{base} / {quote}",
                "maxLeverage": row.get("lever", "1"),
                "minContracts": row.get("minSz", "1"),
            })

        def sort_key(row):
            priority = {"BTC": 0, "ETH": 1}.get(row["base"], 2)
            return (priority, row["base"])

        result.sort(key=sort_key)
        _instruments_cache["data"] = result
        _instruments_cache["ts"] = now
        return result
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Could not load OKX instruments: {exc}") from None
    finally:
        await client.close()


@api.post("/api/okx/connect")
async def connect_okx_demo(request: OKXDemoConnectionRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    if not settings.encryption_key:
        raise HTTPException(503, "Credential vault is not configured")
    client = OKXDemoClient(request.api_key, request.api_secret, request.passphrase)
    try:
        account = await client.account_config()
        vault = CredentialVault(settings.encryption_key)
        database.save_okx_connection(int(user["id"]), vault.encrypt(request.api_key), vault.encrypt(request.api_secret), vault.encrypt(request.passphrase), account)
        _instruments_cache["data"] = None
        _instruments_cache["ts"] = 0.0
        return {"connected": True, "provider": "okx", "environment": "demo"}
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(400, f"OKX Demo connection rejected: {exc}") from None
    finally:
        await client.close()


@api.post("/api/paper-settings")
async def paper_settings(request: PaperSettingsRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    try:
        database.update_paper_settings(int(user["id"]), request.enabled, Decimal(request.max_order_notional), Decimal(request.max_daily_loss))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Limits must be valid positive numbers") from None
    return database.dashboard(int(user["id"]))


@api.post("/api/automation")
async def automation_settings(request: AutomationProfileRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    dashboard_data = database.dashboard(int(user["id"]))
    if request.enabled and (dashboard_data.get("environment") != "demo" or dashboard_data.get("provider") != "bybit"):
        raise HTTPException(400, "OKX execution is not enabled yet. Automation remains paused until the OKX futures execution adapter is tested.")
    try:
        database.save_automation_profile(int(user["id"]), request.enabled, request.strategy, request.symbol, Decimal(request.max_order_notional), request.max_trades_per_day, Decimal(request.stop_loss_pct), Decimal(request.take_profit_pct))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Check your strategy limits") from None
    state = "started" if request.enabled else "paused"
    await api.state.notifier.send(int(user["id"]), f"🧪 Demo automation {state}\nStrategy: {request.strategy}\nSymbol: {request.symbol}\nMax order: {request.max_order_notional} USDT\nMax trades/day: {request.max_trades_per_day}\nUse STOP ALL AUTOMATION in the dashboard to disable future scans.")
    return database.automation_profile(int(user["id"]))


@api.post("/api/automation/kill-switch")
async def kill_switch(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    database.set_kill_switch(int(user["id"]))
    await api.state.notifier.send(int(user["id"]), "🛑 Demo automation stopped. No future scans will submit orders. Review open orders in Bybit Demo Trading separately.")
    return {"enabled": False, "message": "Automation stopped"}


@api.delete("/api/exchange/connection")
async def disconnect_exchange(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    database.disconnect_bybit(int(user["id"]))
    _instruments_cache["data"] = None
    _instruments_cache["ts"] = 0.0
    await api.state.notifier.send(int(user["id"]), "🔒 Exchange Demo connection removed and automation stopped.")
    return {"connected": False}


@api.get("/api/history")
async def history(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    await sync_okx_order_history(int(user["id"]))
    return database.history(int(user["id"]))


async def sync_okx_order_history(user_id: int) -> None:
    if database.connection_provider(user_id) != "okx":
        return
    client = okx_demo_client_for(user_id)
    try:
        for row in database.orders_for_sync(user_id):
            detail = await client.order_detail(row["symbol"], row["order_id"])
            if not detail:
                continue
            price = Decimal(detail.get("avgPx") or detail.get("fillPx") or "0")
            quantity = Decimal(detail.get("accFillSz") or "0")
            pnl = Decimal(detail.get("pnl") or "0")
            database.reconcile_exchange_order(row["idempotency_key"], detail.get("state", "unknown").upper(), price, quantity, pnl)
    finally:
        await client.close()


@api.get("/api/portfolio")
async def portfolio(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    await sync_okx_order_history(user_id)
    client = okx_demo_client_for(user_id)
    try:
        positions = await client.swap_positions()
        active = [{"instrument": row.get("instId"), "side": row.get("posSide"), "contracts": row.get("pos"), "entry_price": row.get("avgPx"), "mark_price": row.get("markPx"), "unrealized_pnl": row.get("upl"), "realized_pnl": row.get("realizedPnl"), "leverage": row.get("lever"), "margin_mode": row.get("mgnMode"), "liquidation_price": row.get("liqPx") or None} for row in positions]
        return {"exchange": "okx", "environment": "demo", "active_positions": active, "bot_trade_history": database.history(user_id)}
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation) as exc:
        raise HTTPException(502, f"OKX portfolio sync unavailable: {exc}") from None
    finally:
        await client.close()


@api.post("/api/backtest")
async def backtest(request: BacktestRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    telegram_user(x_telegram_init_data)
    try:
        candles = await PublicBybitMarket().candles(request.symbol, request.interval)
        return run_rsi_reversion(candles, rsi_threshold=request.rsi_threshold, stop_loss_pct=request.stop_loss_pct, take_profit_pct=request.take_profit_pct, order_notional=request.order_notional)
    except (MarketDataError, ValueError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Backtest market data unavailable: {exc}") from None


@api.post("/api/futures/leverage-options")
async def leverage_options(request: LeverageRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    environment = database.connection_environment(int(user["id"]))
    provider = database.connection_provider(int(user["id"]))
    if environment != "demo" or provider != "okx":
        raise HTTPException(400, "Connect an OKX Demo account before checking futures rules.")
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    client = OKXDemoClient("", "", "")
    try:
        instrument = await client.swap_instrument(inst_id)
        return {"symbol": request.symbol, "instrument": inst_id, "min_leverage": "1", "max_leverage": instrument.get("lever", "1"), "leverage_step": "1", "tick_size": instrument["tickSz"], "min_contracts": instrument["minSz"], "contract_value": instrument["ctVal"], "contract_currency": instrument["ctValCcy"], "state": instrument["state"]}
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Futures rules unavailable: {exc}") from None
    finally:
        await client.close()


@api.post("/api/futures/preview")
async def futures_preview(request: FuturesPreviewRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = okx_demo_client_for(user_id)
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    try:
        instrument, price, available_balance = await asyncio.gather(client.swap_instrument(inst_id), client.last_price(inst_id), client.account_balance())
        maximum_leverage = Decimal(instrument.get("lever") or "1")
        leverage = Decimal(request.leverage)
        if leverage > maximum_leverage:
            raise HTTPException(400, f"{inst_id} supports up to {maximum_leverage}× leverage on OKX.")
        requested_notional = request.margin_usdt * leverage
        multiplier = Decimal(instrument.get("ctMult") or "1")
        contract_value = Decimal(instrument["ctVal"]) * multiplier
        contract_notional = contract_value * price
        contracts = (requested_notional / contract_notional).to_integral_value(rounding=ROUND_DOWN)
        if contracts < Decimal(instrument["minSz"]):
            raise HTTPException(400, f"Margin is too small for OKX's {instrument['minSz']} contract minimum at the current price.")
        actual_notional = contracts * contract_notional
        initial_margin = actual_notional / leverage
        permitted_margin = available_balance * settings.max_demo_margin_pct / Decimal("100")
        if initial_margin > permitted_margin:
            raise HTTPException(400, f"Balance safety check: about {initial_margin:.2f} USDT margin is needed. This ticket permits up to {permitted_margin:.2f} USDT ({settings.max_demo_margin_pct}% of your {available_balance:.2f} USDT Demo balance), leaving a buffer for fees and adverse moves.")
        return {"preview_only": True, "exchange": "okx", "environment": "demo", "instrument": inst_id, "side": request.side, "mark_price": str(price), "leverage": str(leverage), "contracts": str(contracts), "contract_notional_usdt": str(contract_notional), "estimated_notional_usdt": str(actual_notional), "estimated_initial_margin_usdt": str(initial_margin), "available_usdt": str(available_balance), "maximum_recommended_margin_usdt": str(permitted_margin), "remaining_usdt_before_fees": str(available_balance - initial_margin), "position_mode": "Not changed by this preview", "liquidation_price": "Not estimated: it depends on OKX risk tiers, maintenance margin, account mode, and other positions.", "next_step": "No order was placed. Execution requires a separate confirmation-and-reconciliation build step."}
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        raise HTTPException(502, f"OKX futures preview unavailable: {exc}") from None
    finally:
        await client.close()


@api.get("/api/futures/account")
async def futures_account(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    client = okx_demo_client_for(int(user["id"]))
    try:
        account = await client.account_config()
        levels = {"1": "Spot", "2": "Futures", "3": "Multi-currency margin", "4": "Portfolio margin"}
        return {"exchange": "okx", "environment": "demo", "account_mode": levels.get(account.get("acctLv"), "Unknown"), "account_mode_code": account.get("acctLv"), "position_mode": account.get("posMode"), "can_change_position_mode": account.get("acctLv") != "4", "note": "Account mode is controlled in OKX Demo's website/app. Position mode changes require no open positions or pending orders."}
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"OKX account configuration unavailable: {exc}") from None
    finally:
        await client.close()


@api.post("/api/futures/position-mode")
async def set_futures_position_mode(request: PositionModeRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    client = okx_demo_client_for(int(user["id"]))
    try:
        account = await client.account_config()
        if account.get("acctLv") == "4" and request.position_mode != "net_mode":
            raise HTTPException(400, "OKX Portfolio Margin only supports net position mode.")
        changed = await client.set_position_mode(request.position_mode)
        return {"position_mode": changed.get("posMode", request.position_mode), "message": "OKX Demo position mode updated. No order was placed."}
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise HTTPException(400, f"OKX position mode was not changed: {exc}") from None
    finally:
        await client.close()


@api.post("/api/futures/demo-order")
async def place_futures_demo_order(request: FuturesDemoOrderRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = okx_demo_client_for(user_id)
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    try:
        account, instrument, price, available_balance = await asyncio.gather(client.account_config(), client.swap_instrument(inst_id), client.last_price(inst_id), client.account_balance())
        if account.get("acctLv") not in {"2", "3", "4"}:
            raise HTTPException(400, "OKX Demo account is in Spot mode. In OKX Demo Trading, switch Account mode to Futures mode or Multi-currency margin mode before submitting a perpetual order.")
        leverage = Decimal(request.leverage)
        if leverage > Decimal(instrument.get("lever") or "1"):
            raise HTTPException(400, f"{inst_id} supports up to {instrument.get('lever')}× leverage.")
        if request.side == "long" and not (request.stop_loss < price < request.take_profit):
            raise HTTPException(400, "For a long order, stop loss must be below and take profit above the current price.")
        if request.side == "short" and not (request.take_profit < price < request.stop_loss):
            raise HTTPException(400, "For a short order, take profit must be below and stop loss above the current price.")
        contract_notional = Decimal(instrument["ctVal"]) * Decimal(instrument.get("ctMult") or "1") * price
        contracts = ((request.margin_usdt * leverage) / contract_notional).to_integral_value(rounding=ROUND_DOWN)
        if contracts < Decimal(instrument["minSz"]):
            raise HTTPException(400, "Margin is too small for the exchange minimum contract size.")
        actual_notional = contracts * contract_notional
        initial_margin = actual_notional / leverage
        permitted_margin = available_balance * settings.max_demo_margin_pct / Decimal("100")
        if initial_margin > permitted_margin:
            raise HTTPException(400, f"Balance safety check refused {initial_margin:.2f} USDT margin; maximum is {permitted_margin:.2f} USDT.")
        if account.get("posMode") == "long_short_mode":
            position_side = request.side
        else:
            position_side = "net"
        exchange_side = "buy" if request.side == "long" else "sell"
        intent = OrderIntent(user_id, inst_id, Side.BUY if exchange_side == "buy" else Side.SELL, actual_notional, request.client_order_id)
        if not database.reserve(intent):
            existing = database.existing_order(request.client_order_id)
            if existing:
                return {"accepted": True, "replayed": True, "order_id": existing.order_id, "status": existing.status}
            raise HTTPException(409, "This Demo order is already being processed. Do not submit it again.")
        submission_started = False
        try:
            await client.set_leverage(inst_id, leverage, position_side)
            submission_started = True
            accepted = await client.place_market_swap(inst_id=inst_id, side=exchange_side, position_side=position_side, contracts=contracts, client_order_id=request.client_order_id, take_profit=request.take_profit, stop_loss=request.stop_loss)
            order_id = accepted.get("ordId")
            if not order_id:
                raise ExchangeError("OKX accepted the request without an order ID; check Demo order history before retrying.")
            detail = await client.order_detail(inst_id, order_id)
            status = (detail or {}).get("state", "live").upper()
            filled_price = Decimal((detail or {}).get("avgPx") or str(price))
            filled_contracts = Decimal((detail or {}).get("accFillSz") or str(contracts))
            database.finalize(request.client_order_id, OrderResult(order_id, status, filled_price, filled_contracts))
        except OrderRejected:
            database.cancel_reservation(request.client_order_id)
            raise
        except Exception:
            if not submission_started:
                database.cancel_reservation(request.client_order_id)
            raise
        await api.state.notifier.send(user_id, f"🧪 OKX Demo order accepted\n{request.side.upper()} {inst_id}\nLeverage: {leverage}× · Contracts: {contracts}\nOrder: {order_id}\nStatus: {status}\nTP: {request.take_profit} · SL: {request.stop_loss}")
        return {"accepted": True, "replayed": False, "exchange": "okx", "environment": "demo", "order_id": order_id, "status": status, "contracts": str(contracts), "estimated_initial_margin_usdt": str(initial_margin), "take_profit": str(request.take_profit), "stop_loss": str(request.stop_loss), "message": "Demo order accepted and reconciled. Check the OKX Demo Trading order/position screen as the source of truth."}
    except HTTPException as exc:
        log.info("OKX Demo order rejected: instrument=%s status=%s reason=%s", inst_id, exc.status_code, exc.detail)
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        log.warning("OKX Demo order exchange rejection: instrument=%s reason=%s", inst_id, exc)
        raise HTTPException(400, f"OKX Demo order was not placed: {exc}") from None
    finally:
        await client.close()


@api.post("/api/futures/close-position")
async def close_futures_demo_position(request: ClosePositionRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = okx_demo_client_for(user_id)
    close_key = f"close{uuid.uuid4().hex[:23]}"
    try:
        positions = await client.swap_positions()
        position = next((row for row in positions if row.get("instId") == request.instrument and row.get("posSide") == request.position_side), None)
        if not position:
            raise HTTPException(404, "That position is no longer active. Refresh the portfolio.")
        contracts = abs(Decimal(position["pos"]))
        if contracts <= 0:
            raise HTTPException(404, "That position has no contracts to close.")
        if request.position_side == "long":
            close_side = "sell"
        elif request.position_side == "short":
            close_side = "buy"
        else:
            close_side = "sell" if Decimal(position["pos"]) > 0 else "buy"
        intent = OrderIntent(user_id, request.instrument, Side.SELL if close_side == "sell" else Side.BUY, Decimal("0"), close_key)
        if not database.reserve(intent):
            raise HTTPException(409, "Could not reserve the close request. Refresh and retry once.")
        submission_started = False
        try:
            submission_started = True
            accepted = await client.close_market_swap(inst_id=request.instrument, side=close_side, position_side=request.position_side, contracts=contracts, client_order_id=close_key)
            order_id = accepted.get("ordId")
            if not order_id:
                raise ExchangeError("OKX accepted the close request without an order ID. Check OKX Demo before retrying.")
            detail = await client.order_detail(request.instrument, order_id)
            status = (detail or {}).get("state", "live").upper()
            price = Decimal((detail or {}).get("avgPx") or (detail or {}).get("fillPx") or position.get("markPx") or "0")
            filled = Decimal((detail or {}).get("accFillSz") or "0")
            pnl = Decimal((detail or {}).get("pnl") or "0")
            database.finalize(close_key, OrderResult(order_id, status, price, filled))
            database.reconcile_exchange_order(close_key, status, price, filled, pnl)
        except OrderRejected:
            database.cancel_reservation(close_key)
            raise
        except Exception:
            if not submission_started:
                database.cancel_reservation(close_key)
            raise
        await api.state.notifier.send(user_id, f"🧪 OKX Demo close accepted\n{request.instrument} · {request.position_side}\nOrder: {order_id}\nStatus: {status}\nRealized P&L reported by order: {pnl} USDT\nCheck OKX Demo as the source of truth.")
        return {"closed": True, "order_id": order_id, "status": status, "realized_pnl_usdt": str(pnl), "fill_price": str(price), "contracts": str(filled or contracts), "message": "Close order accepted. The realized P&L is from OKX's order response and may update after final exchange settlement."}
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        raise HTTPException(400, f"OKX Demo position was not closed: {exc}") from None
    finally:
        await client.close()


def _session_to_dict(s: "SessionState") -> dict:
    floor = s.starting_balance * s.config.safety_floor_pct / Decimal("100")
    try:
        session_pnl = session_manager.session_realized_pnl(s.id)
    except Exception:
        session_pnl = Decimal("0")
    return {
        "id": s.id,
        "mode": s.config.mode,
        "strategy": s.config.strategy,
        "markets": s.config.markets,
        "started_at": s.started_at.isoformat(),
        "ends_at": s.ends_at.isoformat(),
        "seconds_remaining": s.seconds_remaining,
        "starting_balance": str(s.starting_balance),
        "current_balance": str(s.current_balance),
        "ending_balance": str(s.ending_balance) if s.ending_balance is not None else None,
        "pnl": str(s.pnl),
        "session_pnl": str(session_pnl),
        "target_profit": str(s.target_profit),
        "max_loss": str(s.max_loss),
        "risk_per_trade_pct": str(s.config.risk_per_trade_pct),
        "safety_floor_pct": str(s.config.safety_floor_pct),
        "safety_floor_value": str(floor),
        "leverage": s.config.leverage,
        "trades_count": s.trades_count,
        "wins_count": s.wins_count,
        "losses_count": s.losses_count,
        "status": s.status.value,
        "end_reason": s.end_reason,
    }


@api.post("/api/auto/start")
async def auto_start(request: StartSessionRequest,
                     x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    existing = session_manager.get_active(user_id)
    if existing:
        raise HTTPException(409, "A session is already running. Stop it first.")

    client = okx_demo_client_for(user_id)
    try:
        balance = await client.account_balance()
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Could not read Demo balance: {exc}") from None
    finally:
        await client.close()

    if balance <= 0:
        raise HTTPException(400, "Demo balance is zero. Fund the account first.")

    config = SessionConfig(
        mode=request.mode,
        strategy=request.strategy,
        duration_hours=request.duration_hours,
        target_profit=request.target_profit,
        max_loss=request.max_loss,
        risk_per_trade_pct=request.risk_per_trade_pct,
        safety_floor_pct=request.safety_floor_pct,
        leverage=request.leverage,
        markets=request.markets,
    )
    session_id = session_manager.create_session(user_id, config, balance)
    session = session_manager.get_by_id(session_id)

    try:
        await api.state.notifier.send(
            user_id,
            f"🚀 Auto trading started — session #{session_id}\n"
            f"Mode: {request.mode}\n"
            f"Duration: {request.duration_hours}h\n"
            f"Target: +{request.target_profit} USDT · Max loss: -{request.max_loss} USDT\n"
            f"Risk/trade: {request.risk_per_trade_pct}% of balance\n"
            f"Starting balance: {balance:.2f} USDT",
        )
    except Exception:
        pass

    return {"started": True, "session": _session_to_dict(session)}


@api.post("/api/auto/stop")
async def auto_stop(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    session = session_manager.get_active(user_id)
    if not session:
        raise HTTPException(404, "No active session.")

    client = okx_demo_client_for(user_id)
    try:
        try:
            tracked = session_manager.list_open_positions(session.id)
        except Exception:
            tracked = []

        try:
            live = await client.swap_positions()
        except ExchangeError:
            live = []
        live_map = {(p.get("instId"), p.get("posSide") or "net"): p for p in live}

        for tp in tracked:
            live_pos = live_map.get((tp.inst_id, tp.pos_side))
            if not live_pos:
                session_manager.mark_position_closed(tp.client_order_id, None)
                continue

            contracts = abs(Decimal(live_pos.get("pos") or "0"))
            if contracts <= 0:
                continue

            if tp.pos_side == "long":
                close_side = "sell"
            elif tp.pos_side == "short":
                close_side = "buy"
            else:
                close_side = "sell" if Decimal(live_pos.get("pos")) > 0 else "buy"

            close_key = f"stop{session.id}{uuid.uuid4().hex[:18]}"[:28]
            try:
                accepted = await client.close_market_swap(
                    inst_id=tp.inst_id, side=close_side, position_side=tp.pos_side,
                    contracts=contracts, client_order_id=close_key,
                )
                detail = await client.order_detail(tp.inst_id, accepted.get("ordId", ""))
                status = (detail or {}).get("state", "live").upper()
                fill = Decimal((detail or {}).get("avgPx") or live_pos.get("markPx") or "0")
                filled = Decimal((detail or {}).get("accFillSz") or "0")
                pnl = Decimal((detail or {}).get("pnl") or "0")
                database.reconcile_exchange_order(close_key, status, fill, filled, pnl)
                session_manager.mark_position_closed(tp.client_order_id, pnl)
            except Exception as exc:
                log.warning("Manual stop close failed for %s: %s", tp.inst_id, exc)

        await asyncio.sleep(1.5)
        try:
            balance = await client.account_balance()
        except Exception:
            balance = session.current_balance
    finally:
        await client.close()

    session_manager.end_session(session.id, EndReason.USER_STOP, balance)
    updated = session_manager.get_by_id(session.id)

    try:
        session_pnl = session_manager.session_realized_pnl(session.id)
    except Exception:
        session_pnl = Decimal("0")

    account_change = balance - session.starting_balance
    sp_sign = "+" if session_pnl >= 0 else "-"
    ac_sign = "+" if account_change >= 0 else "-"

    try:
        await api.state.notifier.send(
            user_id,
            f"⏹️ Auto trading stopped — session #{session.id}\n\n"
            f"🤖 Session P&L:  {sp_sign}${abs(session_pnl):.2f} USDT\n"
            f"   ({session.trades_count} session trades: "
            f"W {session.wins_count} / L {session.losses_count})\n\n"
            f"📊 Account change: {ac_sign}${abs(account_change):.2f} USDT\n"
            f"   Started: {session.starting_balance:.2f}\n"
            f"   Ended:   {balance:.2f}\n"
            f"   (includes any manual trades you made)",
        )
    except Exception:
        pass

    return {"stopped": True, "session": _session_to_dict(updated)}


@api.get("/api/auto/status")
async def auto_status(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    session = session_manager.get_active(user_id)
    if not session:
        return {"active": False, "session": None}

    try:
        client = okx_demo_client_for(user_id)
        try:
            balance = await client.account_balance()
            session_manager.update_balance(session.id, balance)
            session = session_manager.get_by_id(session.id)
        finally:
            await client.close()
    except Exception:
        pass

    return {"active": True, "session": _session_to_dict(session)}


@api.get("/api/auto/history")
async def auto_history(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    rows = session_manager.list_history(int(user["id"]), limit=20)
    return [_session_to_dict(s) for s in rows]
