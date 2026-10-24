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
from .exchange import (
    BybitClient,
    ExchangeError,
    OKXClient,
    OKXDemoClient,
    OKXLiveClient,
    OrderRejected,
)
from .models import OrderIntent, OrderResult, Side
from .security import CredentialVault, secure_equals
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
from .risk import classify, rules_for, MIN_ACCOUNT_BALANCE

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

_portfolio_cache: dict = {}
PORTFOLIO_CACHE_TTL = 5.0

_telegram_user_cache: dict = {}
TELEGRAM_USER_CACHE_TTL = 60.0

_okx_client_cache: dict = {}
OKX_CLIENT_CACHE_TTL = 300.0
_okx_client_lock = asyncio.Lock()

_portfolio_sync_in_flight: set[int] = set()

# Signals: cache is per-instId (shared across users — a signal for BTC-USDT-SWAP
# is identical for everyone). Rate limit is per-user to prevent abuse.
_signal_cache: dict[str, tuple[float, dict]] = {}
SIGNAL_CACHE_TTL = 60.0
_signal_rate_limit: dict[int, list[float]] = {}
SIGNAL_RATE_WINDOW = 60.0
SIGNAL_RATE_MAX = 10


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
    log.info(
        "Application startup — mode=%s, automation=%s, live_trading=%s",
        settings.trading_mode,
        "okx-demo" if settings.encryption_key else "disabled",
        "ENABLED" if settings.allow_live_trading else "disabled",
    )
    ticker_task = asyncio.create_task(monitor_okx_public_prices(), name="okx-demo-public-tickers")
    if settings.encryption_key:
        worker = AutoTradingWorker(
            session_manager=session_manager,
            database=database,
            vault=CredentialVault(settings.encryption_key),
            notifier=notifier,
            settings=settings,
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


class OKXConnectionRequest(BaseModel):
    """Credentials for connecting an OKX account.

    For demo connections only `api_key`, `api_secret`, and `passphrase`
    are needed. For live connections, the client must also provide the
    `confirmation_code` matching `LIVE_CONFIRMATION_CODE` in the server
    environment.
    """
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=16, max_length=512)
    passphrase: str = Field(min_length=8, max_length=256)
    environment: Literal["demo", "live"] = "demo"
    confirmation_code: str | None = None


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
    # Comma-separated list of strategies. Examples:
    #   "daily_breakout"
    #   "daily_pullback"
    #   "daily_breakout,daily_pullback"   (priority = left-to-right)
    strategy: str = Field(default="daily_breakout", min_length=3, max_length=64)
    duration_hours: float = Field(gt=0, le=24 * 366)
    target_profit: Decimal = Field(gt=0)
    max_loss: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(ge=0, le=10)
    risk_per_trade_usdt: Decimal = Field(ge=0)
    risk_unit: Literal["pct", "usdt"] = "pct"
    safety_floor_pct: Decimal = Field(ge=50, le=95)
    leverage: int = Field(ge=1, le=100)
    markets: str = Field(default="auto", min_length=1, max_length=500)


class SignalAnalyzeRequest(BaseModel):
    """Payload for /api/signals/analyze.

    `token` accepts "BTC", "BTC/USDT", "BTC-USDT", or "BTC-USDT-SWAP".
    Strict match: must exist as a USDT perpetual on OKX, exactly as listed.
    """
    token: str = Field(min_length=1, max_length=32)


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
    cache_key = hash(init_data)
    now = time.monotonic()
    cached = _telegram_user_cache.get(cache_key)
    if cached and (now - cached[0]) < TELEGRAM_USER_CACHE_TTL:
        return cached[1]
    try:
        user = validate_init_data(init_data, settings.telegram_token)
        last_write_key = f"wrote_{int(user['id'])}"
        last_write = _telegram_user_cache.get(last_write_key, (0, None))[0]
        if (now - last_write) > 300:
            database.upsert_user(int(user["id"]), user.get("username"), user.get("first_name"))
            _telegram_user_cache[last_write_key] = (now, None)
        _telegram_user_cache[cache_key] = (now, user)
        return user
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None


# ---------------------------------------------------------------------------
# Client factory — routes to demo or live based on the stored connection
# ---------------------------------------------------------------------------

def okx_client_for(user_id: int) -> OKXClient:
    """Return an OKX client for this user.

    Reads the stored connection's `environment` field and returns
    OKXDemoClient or OKXLiveClient accordingly. Live connections are
    refused at this layer when the server has live trading disabled.
    """
    credentials = database.exchange_credentials(user_id)
    if not credentials or credentials.get("provider") != "okx":
        raise HTTPException(400, "Connect an OKX account first.")

    environment = credentials.get("environment") or "demo"

    if not settings.encryption_key:
        raise HTTPException(503, "Credential vault is not configured.")
    if not credentials.get("encrypted_api_key") or not credentials.get("encrypted_api_secret"):
        raise HTTPException(503, "OKX credentials are incomplete. Reconnect the account.")

    vault = CredentialVault(settings.encryption_key)
    api_key = vault.decrypt(credentials["encrypted_api_key"])
    api_secret = vault.decrypt(credentials["encrypted_api_secret"])

    if environment == "live":
        # Guard 1: server must permit live trading.
        if not settings.allow_live_trading:
            raise HTTPException(
                503,
                "Live trading is disabled on this server. "
                "Contact the operator to enable it.",
            )
        # Guard 2: live requires a passphrase (OKX always requires one).
        if not credentials.get("encrypted_passphrase"):
            raise HTTPException(503, "Live credentials are incomplete. Reconnect the account.")
        passphrase = vault.decrypt(credentials["encrypted_passphrase"])
        return OKXLiveClient(api_key, api_secret, passphrase)

    # Default: demo
    if not credentials.get("encrypted_passphrase"):
        raise HTTPException(503, "Demo credentials are incomplete. Reconnect the account.")
    passphrase = vault.decrypt(credentials["encrypted_passphrase"])
    return OKXDemoClient(api_key, api_secret, passphrase)


async def okx_client_for_cached(user_id: int) -> OKXClient:
    """Return a cached OKX client, or create one.

    The stale client is closed BEFORE a new one is created. Skipping that
    step leaks the aiohttp session, and uvloop eventually reports
    "Fatal error on SSL protocol" during garbage collection.
    """
    now = time.monotonic()
    cached = _okx_client_cache.get(user_id)
    if cached and (now - cached[0]) < OKX_CLIENT_CACHE_TTL:
        return cached[1]
    async with _okx_client_lock:
        cached = _okx_client_cache.get(user_id)
        if cached and (time.monotonic() - cached[0]) < OKX_CLIENT_CACHE_TTL:
            return cached[1]
        if cached:
            try:
                await cached[1].close()
            except Exception:
                pass
        client = okx_client_for(user_id)
        _okx_client_cache[user_id] = (time.monotonic(), client)
        return client


def invalidate_okx_client(user_id: int) -> None:
    """Drop a cached OKX client and close it in the background."""
    old = _okx_client_cache.pop(user_id, None)
    if not old:
        return

    async def _safe_close():
        try:
            await old[1].close()
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_safe_close())
    except RuntimeError:
        pass


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
    """Poll REST prices for the watched instruments.

    Reuses one OKXDemoClient for the poller's lifetime. Creating a new
    client every second raced aiohttp's session close against in-flight
    requests, which produced a flood of "Fatal error on SSL protocol"
    messages in the logs.
    """
    client = OKXDemoClient("", "", "")
    try:
        while True:
            try:
                instruments = watched_instruments()
                if instruments:
                    results = await asyncio.gather(
                        *(client.last_price(inst) for inst in instruments),
                        return_exceptions=True,
                    )
                    for inst, result in zip(instruments, results, strict=True):
                        if isinstance(result, Exception):
                            continue
                        await publish_live_price(inst, str(result))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("REST price poll iteration failed: %s: %s",
                          type(exc).__name__, exc)
            await asyncio.sleep(1.0)
    finally:
        await client.close()


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
                log.warning("OKX Demo public ticker feed unavailable: %s: %s",
                            type(exc).__name__, exc)
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


async def notify_position_closed(user_id: int, notifier: TelegramNotifier,
                                 instrument: str, side: str) -> None:
    await sync_okx_order_history(user_id)
    _portfolio_cache.pop(user_id, None)
    await notifier.send(
        user_id,
        f"🔔 OKX position closed\n{instrument} · {side}\n"
        "The exchange pushed this update immediately. It may have been a TP, SL, or a manual close; "
        "the ticket is reconciling the final P&L now.",
    )
    await publish_futures_event(user_id, "portfolio_changed")


async def monitor_okx_private_user(user_id: int, notifier: TelegramNotifier) -> None:
    known_positions: set[tuple[str, str]] = set()
    delay = 1
    while True:
        client: OKXClient | None = None
        try:
            client = okx_client_for(user_id)
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
                    _portfolio_cache.pop(user_id, None)
                    await publish_futures_event(user_id, "portfolio_changed")
                elif channel == "orders":
                    if any(row.get("state") in {"filled", "canceled"} for row in rows):
                        _portfolio_cache.pop(user_id, None)
                        await sync_okx_order_history(user_id)
                        await publish_futures_event(user_id, "portfolio_changed")
        except asyncio.CancelledError:
            raise
        except HTTPException:
            # User has no usable connection (disconnected, or live disabled).
            # Back off and try again later.
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as exc:
            log.warning("OKX private feed unavailable for user %s: %s: %s",
                        user_id, type(exc).__name__, exc)
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
                    monitor_okx_private_user(user_id, notifier),
                    name=f"okx-private-{user_id}",
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
    trade_permissions = (
        set(permissions.get("Spot", []))
        | set(permissions.get("Derivatives", []))
        | set(permissions.get("ContractTrade", []))
    )
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
    except WebSocketDisconnect:
        return
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
        await websocket.send_json({
            "type": "connected",
            "environment": "demo",
            "price_instrument": "BTC-USDT-SWAP",
        })
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
    return {
        "ok": True,
        "mode": settings.trading_mode,
        "automation_environment": "okx",
        "live_trading_enabled": bool(settings.allow_live_trading),
    }


@api.get("/health/okx")
async def okx_connectivity() -> dict:
    url = "https://www.okx.com/api/v5/public/instruments"
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as connector:
            async with connector.get(url, params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"}) as response:
                if response.status != 200:
                    raise HTTPException(502, f"OKX returned HTTP {response.status}")
                data = await response.json(content_type=None)
        if not isinstance(data, dict) or data.get("code") != "0" or not data.get("data"):
            raise HTTPException(502, "OKX returned an unexpected public API response")
        instrument = data["data"][0]
        return {
            "ok": True,
            "exchange": "okx",
            "instrument": instrument["instId"],
            "state": instrument.get("state"),
            "max_leverage": instrument.get("lever"),
        }
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(502, "OKX public API is unreachable from this host") from None


@api.get("/api/dashboard")
async def dashboard(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    result = database.dashboard(int(user["id"]))
    result["automation"] = database.automation_profile(int(user["id"]))
    result["live_trading_enabled"] = bool(settings.allow_live_trading)
    return result


@api.get("/api/exchange/instruments")
async def exchange_instruments(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    provider = database.connection_provider(int(user["id"]))
    if provider != "okx":
        raise HTTPException(400, "Connect an OKX account first.")

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


@api.get("/api/okx/environment")
async def okx_environment(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    """Return the user's current OKX environment and server capabilities.

    The UI uses this to decide whether to show the demo/live toggle, and
    whether the "Live" option should be enabled or disabled.
    """
    user = telegram_user(x_telegram_init_data)
    environment = database.connection_environment(int(user["id"]))
    return {
        "environment": environment,  # None if not connected
        "live_trading_enabled_on_server": bool(settings.allow_live_trading),
        "min_account_balance": str(MIN_ACCOUNT_BALANCE),
    }


@api.post("/api/okx/connect")
async def connect_okx(
    request: OKXConnectionRequest,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """Connect an OKX demo or live account.

    For live connections the request must include `confirmation_code`
    matching the server's `LIVE_CONFIRMATION_CODE` environment variable.
    Live connections are also refused when `ALLOW_LIVE_TRADING=false`.
    """
    user = telegram_user(x_telegram_init_data)
    if not settings.encryption_key:
        raise HTTPException(503, "Credential vault is not configured")

    environment = request.environment

    if environment == "live":
        # Guard: server must permit live trading.
        if not settings.allow_live_trading:
            raise HTTPException(
                503,
                "Live trading is disabled on this server. "
                "The operator must enable it before live connections are accepted.",
            )
        # Guard: server must have a confirmation code configured.
        if not settings.live_confirmation_code:
            raise HTTPException(
                503,
                "Live trading is misconfigured on this server (no confirmation code). "
                "Contact the operator.",
            )
        # Guard: user must supply the correct confirmation code.
        if not request.confirmation_code or not secure_equals(
            request.confirmation_code, settings.live_confirmation_code
        ):
            raise HTTPException(400, "Incorrect live confirmation code.")

    # Validate the credentials against OKX by calling /account/config.
    # This request carries the correct x-simulated-trading header for
    # whichever environment the user selected.
    if environment == "live":
        client: OKXClient = OKXLiveClient(
            request.api_key, request.api_secret, request.passphrase
        )
    else:
        client = OKXDemoClient(
            request.api_key, request.api_secret, request.passphrase
        )

    try:
        account = await client.account_config()
        vault = CredentialVault(settings.encryption_key)
        database.save_okx_connection(
            int(user["id"]),
            vault.encrypt(request.api_key),
            vault.encrypt(request.api_secret),
            vault.encrypt(request.passphrase),
            account,
            environment=environment,
        )
        _instruments_cache["data"] = None
        _instruments_cache["ts"] = 0.0
        _portfolio_cache.pop(int(user["id"]), None)
        invalidate_okx_client(int(user["id"]))
        return {
            "connected": True,
            "provider": "okx",
            "environment": environment,
        }
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(400, f"OKX connection rejected: {exc}") from None
    finally:
        await client.close()


@api.post("/api/paper-settings")
async def paper_settings(request: PaperSettingsRequest,
                         x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    try:
        database.update_paper_settings(
            int(user["id"]),
            request.enabled,
            Decimal(request.max_order_notional),
            Decimal(request.max_daily_loss),
        )
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Limits must be valid positive numbers") from None
    return database.dashboard(int(user["id"]))


@api.post("/api/automation")
async def automation_settings(request: AutomationProfileRequest,
                              x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    dash = database.dashboard(int(user["id"]))
    if request.enabled and (dash.get("environment") != "demo" or dash.get("provider") != "bybit"):
        raise HTTPException(
            400,
            "OKX execution is not enabled through this endpoint. Use the auto-trading sessions tab.",
        )
    try:
        database.save_automation_profile(
            int(user["id"]),
            request.enabled,
            request.strategy,
            request.symbol,
            Decimal(request.max_order_notional),
            request.max_trades_per_day,
            Decimal(request.stop_loss_pct),
            Decimal(request.take_profit_pct),
        )
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Check your strategy limits") from None
    state = "started" if request.enabled else "paused"
    await api.state.notifier.send(
        int(user["id"]),
        f"🧪 Demo automation {state}\nStrategy: {request.strategy}\n"
        f"Symbol: {request.symbol}\nMax order: {request.max_order_notional} USDT\n"
        f"Max trades/day: {request.max_trades_per_day}",
    )
    return database.automation_profile(int(user["id"]))


@api.post("/api/automation/kill-switch")
async def kill_switch(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    database.set_kill_switch(int(user["id"]))
    await api.state.notifier.send(
        int(user["id"]),
        "🛑 Automation stopped. No future scans will submit orders. "
        "Review already-open orders at your exchange separately.",
    )
    return {"enabled": False, "message": "Automation stopped"}


@api.delete("/api/exchange/connection")
async def disconnect_exchange(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    database.disconnect_bybit(int(user["id"]))
    _instruments_cache["data"] = None
    _instruments_cache["ts"] = 0.0
    _portfolio_cache.pop(int(user["id"]), None)
    invalidate_okx_client(int(user["id"]))
    await api.state.notifier.send(
        int(user["id"]),
        "🔒 OKX connection removed and automation stopped.",
    )
    return {"connected": False}


@api.get("/api/history")
async def history(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    await sync_okx_order_history(int(user["id"]))
    return database.history(int(user["id"]))


async def sync_okx_order_history(user_id: int, *, parallel: bool = True) -> None:
    if database.connection_provider(user_id) != "okx":
        return
    try:
        client = await okx_client_for_cached(user_id)
    except HTTPException:
        return
    rows = list(database.orders_for_sync(user_id))
    pending = [
        r for r in rows
        if str(r.get("status", "")).upper() not in {"FILLED", "CANCELED", "CANCELLED"}
    ]
    if not pending:
        return

    async def fetch_one(row):
        try:
            detail = await client.order_detail(row["symbol"], row["order_id"])
        except Exception:
            return None
        return (row, detail)

    if parallel:
        results = await asyncio.gather(*(fetch_one(r) for r in pending))
    else:
        results = [await fetch_one(r) for r in pending]

    for item in results:
        if item is None:
            continue
        row, detail = item
        if not detail:
            continue
        price = Decimal(detail.get("avgPx") or detail.get("fillPx") or "0")
        quantity = Decimal(detail.get("accFillSz") or "0")
        pnl = Decimal(detail.get("pnl") or "0")
        database.reconcile_exchange_order(
            row["idempotency_key"],
            detail.get("state", "unknown").upper(),
            price, quantity, pnl,
        )


async def _fetch_portfolio(user_id: int) -> dict:
    """Return the user's portfolio snapshot.

    Reconciliation runs fire-and-forget so this endpoint responds in a few
    hundred milliseconds instead of blocking on OKX for up to 15 seconds.

    Position `side` is a *semantic* direction ("long"/"short") derived from
    posSide + sign of `pos` — required for correct display in net mode, where
    OKX returns posSide="net" for every position regardless of direction.
    The raw OKX posSide is preserved separately as `pos_side` because
    close requests must match it exactly.
    """
    client = await okx_client_for_cached(user_id)

    if user_id not in _portfolio_sync_in_flight:
        _portfolio_sync_in_flight.add(user_id)

        async def _bg_sync():
            try:
                await asyncio.wait_for(
                    sync_okx_order_history(user_id), timeout=20.0,
                )
            except asyncio.TimeoutError:
                log.warning("Background portfolio sync timed out for %s", user_id)
            except Exception as exc:
                log.warning("Background portfolio sync failed for %s — %s: %s",
                            user_id, type(exc).__name__, exc)
            finally:
                _portfolio_sync_in_flight.discard(user_id)

        asyncio.create_task(_bg_sync())

    positions = await client.swap_positions()

    active = []
    for row in positions:
        pos_side_raw = row.get("posSide") or "net"
        try:
            pos_amt = Decimal(row.get("pos") or "0")
        except (InvalidOperation, ValueError):
            pos_amt = Decimal("0")

        # Semantic direction for display. In net mode, derive from sign.
        if pos_side_raw == "long":
            direction = "long"
        elif pos_side_raw == "short":
            direction = "short"
        else:  # "net"
            direction = "long" if pos_amt >= 0 else "short"

        active.append({
            "instrument": row.get("instId"),
            "side": direction,                   # display: "long" | "short"
            "pos_side": pos_side_raw,             # close requests: "net" | "long" | "short"
            "contracts": str(abs(pos_amt)),       # always positive for display
            "entry_price": row.get("avgPx"),
            "mark_price": row.get("markPx"),
            "unrealized_pnl": row.get("upl"),
            "realized_pnl": row.get("realizedPnl"),
            "leverage": row.get("lever"),
            "margin_mode": row.get("mgnMode"),
            "liquidation_price": row.get("liqPx") or None,
        })

    return {
        "exchange": "okx",
        "environment": database.connection_environment(user_id) or "demo",
        "active_positions": active,
        "bot_trade_history": database.history(user_id),
    }


async def _refresh_portfolio_background(user_id: int) -> None:
    try:
        response = await _fetch_portfolio(user_id)
        _portfolio_cache[user_id] = (time.monotonic(), response)
    except Exception as exc:
        log.debug("Background portfolio refresh failed for %s: %s: %s",
                  user_id, type(exc).__name__, exc)


@api.get("/api/portfolio")
async def portfolio(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    now = time.monotonic()
    cached = _portfolio_cache.get(user_id)
    if cached and (now - cached[0]) < PORTFOLIO_CACHE_TTL:
        return cached[1]

    if cached is not None:
        asyncio.create_task(_refresh_portfolio_background(user_id))
        return cached[1]

    try:
        response = await _fetch_portfolio(user_id)
        _portfolio_cache[user_id] = (time.monotonic(), response)
        return response
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation) as exc:
        raise HTTPException(502, f"OKX portfolio sync unavailable: {exc}") from None


@api.post("/api/backtest")
async def backtest(request: BacktestRequest,
                   x_telegram_init_data: str | None = Header(default=None)) -> dict:
    telegram_user(x_telegram_init_data)
    try:
        candles = await PublicBybitMarket().candles(request.symbol, request.interval)
        return run_rsi_reversion(
            candles,
            rsi_threshold=request.rsi_threshold,
            stop_loss_pct=request.stop_loss_pct,
            take_profit_pct=request.take_profit_pct,
            order_notional=request.order_notional,
        )
    except (MarketDataError, ValueError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Backtest market data unavailable: {exc}") from None


@api.post("/api/futures/leverage-options")
async def leverage_options(request: LeverageRequest,
                           x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    environment = database.connection_environment(int(user["id"]))
    provider = database.connection_provider(int(user["id"]))
    if provider != "okx":
        raise HTTPException(400, "Connect an OKX account before checking futures rules.")
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    client = OKXDemoClient("", "", "")
    try:
        instrument = await client.swap_instrument(inst_id)
        return {
            "symbol": request.symbol,
            "instrument": inst_id,
            "min_leverage": "1",
            "max_leverage": instrument.get("lever", "1"),
            "leverage_step": "1",
            "tick_size": instrument["tickSz"],
            "min_contracts": instrument["minSz"],
            "contract_value": instrument["ctVal"],
            "contract_currency": instrument["ctValCcy"],
            "state": instrument["state"],
        }
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Futures rules unavailable: {exc}") from None
    finally:
        await client.close()


@api.post("/api/futures/preview")
async def futures_preview(request: FuturesPreviewRequest,
                          x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = await okx_client_for_cached(user_id)
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    try:
        instrument, price, available_balance = await asyncio.gather(
            client.swap_instrument(inst_id),
            client.last_price(inst_id),
            client.account_balance(),
        )
        maximum_leverage = Decimal(instrument.get("lever") or "1")
        leverage = Decimal(request.leverage)
        if leverage > maximum_leverage:
            raise HTTPException(400, f"{inst_id} supports up to {maximum_leverage}× leverage on OKX.")
        
        ct_val = Decimal(instrument["ctVal"])
        ct_mult = Decimal(instrument.get("ctMult") or "1")
        lot_sz = Decimal(instrument.get("lotSz") or "1")
        min_sz = Decimal(instrument.get("minSz") or "1")

        requested_notional = request.margin_usdt * leverage
        contract_notional = ct_val * ct_mult * price
        
        # Calculate exact fractional contracts
        target_contracts = requested_notional / contract_notional
        steps = (target_contracts / lot_sz).to_integral_value(rounding=ROUND_DOWN)
        contracts = steps * lot_sz

        if contracts < min_sz:
            contracts = min_sz

        actual_notional = contracts * contract_notional
        actual_margin = actual_notional / leverage
        base_size = contracts * ct_val * ct_mult

        permitted_margin = available_balance * settings.max_demo_margin_pct / Decimal("100")
        if actual_margin > permitted_margin:
            raise HTTPException(
                400,
                f"Balance safety check: about {actual_margin:.2f} USDT margin is needed. "
                f"This ticket permits up to {permitted_margin:.2f} USDT "
                f"({settings.max_demo_margin_pct}% of your {available_balance:.2f} USDT balance).",
            )
            
        return {
            "preview_only": True,
            "exchange": "okx",
            "environment": database.connection_environment(user_id) or "demo",
            "instrument": inst_id,
            "side": request.side,
            "mark_price": str(price),
            "leverage": str(leverage),
            "contracts": str(base_size),  # Returning base coin size (e.g. 0.0011 BTC)
            "estimated_notional_usdt": str(actual_notional),
            "estimated_initial_margin_usdt": str(actual_margin),
            "available_usdt": str(available_balance),
            "maximum_recommended_margin_usdt": str(permitted_margin),
            "remaining_usdt_before_fees": str(available_balance - actual_margin),
            "position_mode": "Not changed by this preview",
            "liquidation_price": (
                "Not estimated: it depends on OKX risk tiers, maintenance margin, "
                "account mode, and other positions."
            ),
            "next_step": "No order was placed. Review and confirm to place.",
        }
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        raise HTTPException(502, f"OKX futures preview unavailable: {exc}") from None


@api.get("/api/futures/account")
async def futures_account(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    client = await okx_client_for_cached(int(user["id"]))
    try:
        account = await client.account_config()
        levels = {"1": "Spot", "2": "Futures", "3": "Multi-currency margin", "4": "Portfolio margin"}
        return {
            "exchange": "okx",
            "environment": database.connection_environment(int(user["id"])) or "demo",
            "account_mode": levels.get(account.get("acctLv"), "Unknown"),
            "account_mode_code": account.get("acctLv"),
            "position_mode": account.get("posMode"),
            "can_change_position_mode": account.get("acctLv") != "4",
            "note": (
                "Account mode is controlled in OKX's website/app. "
                "Position mode changes require no open positions or pending orders."
            ),
        }
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"OKX account configuration unavailable: {exc}") from None


@api.post("/api/futures/position-mode")
async def set_futures_position_mode(request: PositionModeRequest,
                                    x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    client = await okx_client_for_cached(int(user["id"]))
    try:
        account = await client.account_config()
        if account.get("acctLv") == "4" and request.position_mode != "net_mode":
            raise HTTPException(400, "OKX Portfolio Margin only supports net position mode.")
        changed = await client.set_position_mode(request.position_mode)
        return {
            "position_mode": changed.get("posMode", request.position_mode),
            "message": "OKX position mode updated. No order was placed.",
        }
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise HTTPException(400, f"OKX position mode was not changed: {exc}") from None


@api.post("/api/futures/demo-order")
async def place_futures_demo_order(request: FuturesDemoOrderRequest,
                                   x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = await okx_client_for_cached(user_id)
    inst_id = resolve_okx_swap_inst_id(request.symbol)
    try:
        account, instrument, price, available_balance = await asyncio.gather(
            client.account_config(),
            client.swap_instrument(inst_id),
            client.last_price(inst_id),
            client.account_balance(),
        )
        if account.get("acctLv") not in {"2", "3", "4"}:
            raise HTTPException(
                400,
                "OKX account is in Spot mode. Switch Account mode to Futures or "
                "Multi-currency margin mode before submitting a perpetual order.",
            )
        leverage = Decimal(request.leverage)
        if leverage > Decimal(instrument.get("lever") or "1"):
            raise HTTPException(400, f"{inst_id} supports up to {instrument.get('lever')}× leverage.")
        if request.side == "long" and not (request.stop_loss < price < request.take_profit):
            raise HTTPException(400, "For a long order, stop loss must be below and take profit above the current price.")
        if request.side == "short" and not (request.take_profit < price < request.stop_loss):
            raise HTTPException(400, "For a short order, take profit must be below and stop loss above the current price.")

        ct_val = Decimal(instrument["ctVal"])
        ct_mult = Decimal(instrument.get("ctMult") or "1")
        lot_sz = Decimal(instrument.get("lotSz") or "1")
        min_sz = Decimal(instrument.get("minSz") or "1")

        contract_notional = ct_val * ct_mult * price
        requested_notional = request.margin_usdt * leverage

        # Calculate exact fractional contracts
        target_contracts = requested_notional / contract_notional
        steps = (target_contracts / lot_sz).to_integral_value(rounding=ROUND_DOWN)
        contracts = steps * lot_sz

        if contracts < min_sz:
            contracts = min_sz

        actual_notional = contracts * contract_notional
        initial_margin = actual_notional / leverage
        
        permitted_margin = available_balance * settings.max_demo_margin_pct / Decimal("100")
        if initial_margin > permitted_margin:
            raise HTTPException(
                400,
                f"Balance safety check refused {initial_margin:.2f} USDT margin; "
                f"maximum is {permitted_margin:.2f} USDT.",
            )
            
        if account.get("posMode") == "long_short_mode":
            position_side = request.side
        else:
            position_side = "net"
        exchange_side = "buy" if request.side == "long" else "sell"
        
        intent = OrderIntent(
            user_id, inst_id,
            Side.BUY if exchange_side == "buy" else Side.SELL,
            actual_notional, request.client_order_id,
        )
        if not database.reserve(intent):
            existing = database.existing_order(request.client_order_id)
            if existing:
                return {
                    "accepted": True, "replayed": True,
                    "order_id": existing.order_id, "status": existing.status,
                }
            raise HTTPException(409, "This order is already being processed. Do not submit it again.")
        
        submission_started = False
        try:
            await client.set_leverage(inst_id, leverage, position_side)
            submission_started = True
            accepted = await client.place_market_swap(
                inst_id=inst_id, side=exchange_side, position_side=position_side,
                contracts=contracts, client_order_id=request.client_order_id,
                take_profit=request.take_profit, stop_loss=request.stop_loss,
            )
            order_id = accepted.get("ordId")
            if not order_id:
                raise ExchangeError("OKX accepted the request without an order ID; check order history before retrying.")
            detail = await client.order_detail(inst_id, order_id)
            status = (detail or {}).get("state", "live").upper()
            filled_price = Decimal((detail or {}).get("avgPx") or str(price))
            filled_contracts = Decimal((detail or {}).get("accFillSz") or str(contracts))
            database.finalize(
                request.client_order_id,
                OrderResult(order_id, status, filled_price, filled_contracts),
            )
        except OrderRejected:
            database.cancel_reservation(request.client_order_id)
            raise
        except Exception:
            if not submission_started:
                database.cancel_reservation(request.client_order_id)
            raise
        
        _portfolio_cache.pop(user_id, None)
        await api.state.notifier.send(
            user_id,
            f"🧪 OKX order accepted\n{request.side.upper()} {inst_id}\n"
            f"Leverage: {leverage}× · Contracts: {contracts}\nOrder: {order_id}\n"
            f"Status: {status}\nTP: {request.take_profit} · SL: {request.stop_loss}",
        )
        return {
            "accepted": True,
            "replayed": False,
            "exchange": "okx",
            "environment": database.connection_environment(user_id) or "demo",
            "order_id": order_id,
            "status": status,
            "contracts": str(filled_contracts),
            "estimated_initial_margin_usdt": str(initial_margin),
            "take_profit": str(request.take_profit),
            "stop_loss": str(request.stop_loss),
            "message": "Order accepted and reconciled. Check OKX's order/position screen as the source of truth.",
        }
    except HTTPException as exc:
        log.info(
            "OKX order rejected: instrument=%s status=%s reason=%s",
            inst_id, exc.status_code, exc.detail,
        )
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        log.warning("OKX order exchange rejection: instrument=%s reason=%s", inst_id, exc)
        raise HTTPException(400, f"OKX order was not placed: {exc}") from None


@api.post("/api/futures/close-position")
async def close_futures_demo_position(request: ClosePositionRequest,
                                      x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    client = await okx_client_for_cached(user_id)
    close_key = f"close{uuid.uuid4().hex[:23]}"
    try:
        positions = await client.swap_positions()
        position = next(
            (row for row in positions
             if row.get("instId") == request.instrument
             and row.get("posSide") == request.position_side),
            None,
        )
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
        intent = OrderIntent(
            user_id, request.instrument,
            Side.SELL if close_side == "sell" else Side.BUY,
            Decimal("0"), close_key,
        )
        if not database.reserve(intent):
            raise HTTPException(409, "Could not reserve the close request. Refresh and retry once.")
        submission_started = False
        try:
            submission_started = True
            accepted = await client.close_market_swap(
                inst_id=request.instrument, side=close_side,
                position_side=request.position_side, contracts=contracts,
                client_order_id=close_key,
            )
            order_id = accepted.get("ordId")
            if not order_id:
                raise ExchangeError("OKX accepted the close request without an order ID. Check OKX before retrying.")
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
        _portfolio_cache.pop(user_id, None)
        await api.state.notifier.send(
            user_id,
            f"🧪 OKX close accepted\n{request.instrument} · {request.position_side}\n"
            f"Order: {order_id}\nStatus: {status}\n"
            f"Realized P&L reported by order: {pnl} USDT",
        )
        return {
            "closed": True,
            "order_id": order_id,
            "status": status,
            "realized_pnl_usdt": str(pnl),
            "fill_price": str(price),
            "contracts": str(filled or contracts),
            "message": (
                "Close order accepted. The realized P&L is from OKX's order response "
                "and may update after final exchange settlement."
            ),
        }
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError, InvalidOperation, ValueError) as exc:
        raise HTTPException(400, f"OKX position was not closed: {exc}") from None


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
        "environment": s.config.environment,
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
        "risk_per_trade_usdt": str(s.config.risk_per_trade_usdt),
        "risk_unit": s.config.risk_unit,
        "safety_floor_pct": str(s.config.safety_floor_pct),
        "safety_floor_value": str(floor),
        "leverage": s.config.leverage,
        "trades_count": s.trades_count,
        "wins_count": s.wins_count,
        "losses_count": s.losses_count,
        "loss_streak": s.loss_streak,
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

    # Determine which environment we're trading on. This comes from the
    # user's stored connection, not the request — the request cannot
    # switch environments on the fly.
    environment = database.connection_environment(user_id)
    provider = database.connection_provider(user_id)
    if provider != "okx" or environment is None:
        raise HTTPException(400, "Connect an OKX account first.")

    if environment == "live" and not settings.allow_live_trading:
        raise HTTPException(
            503,
            "Live trading is disabled on this server. Contact the operator.",
        )

    client = await okx_client_for_cached(user_id)
    try:
        balance = await client.account_balance()
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Could not read account balance: {exc}") from None

    # Minimum balance gate: $5. Applies to both demo and live.
    if balance < MIN_ACCOUNT_BALANCE:
        raise HTTPException(
            400,
            f"Balance ${balance:.2f} is below the ${MIN_ACCOUNT_BALANCE} minimum. "
            f"Fund the account before starting a session.",
        )

    if request.risk_unit == "pct" and request.risk_per_trade_pct <= 0:
        raise HTTPException(400, "Percentage risk must be greater than zero.")
    if request.risk_unit == "usdt" and request.risk_per_trade_usdt <= 0:
        raise HTTPException(400, "USDT risk must be greater than zero.")

    # Validate strategy list — must contain at least one known strategy name.
    valid_strategies = {"daily_breakout", "daily_pullback"}
    requested = [s.strip() for s in (request.strategy or "").split(",") if s.strip()]
    if not requested:
        raise HTTPException(400, "Pick at least one strategy.")
    unknown = [s for s in requested if s not in valid_strategies]
    if unknown:
        raise HTTPException(400, f"Unknown strategy: {', '.join(unknown)}")
    if len(requested) > 2:
        raise HTTPException(400, "At most 2 strategies per session.")

    config = SessionConfig(
        mode=request.mode,
        strategy=request.strategy,
        duration_hours=request.duration_hours,
        target_profit=request.target_profit,
        max_loss=request.max_loss,
        risk_per_trade_pct=request.risk_per_trade_pct,
        risk_per_trade_usdt=request.risk_per_trade_usdt,
        risk_unit=request.risk_unit,
        safety_floor_pct=request.safety_floor_pct,
        leverage=request.leverage,
        markets=request.markets,
        environment=environment,
    )
    session_id = session_manager.create_session(user_id, config, balance)
    session = session_manager.get_by_id(session_id)

    risk_label = (
        f"{request.risk_per_trade_pct}% of balance"
        if request.risk_unit == "pct"
        else f"{request.risk_per_trade_usdt} USDT fixed"
    )

    log.info(
        "Session %s STARTED by user %s: env=%s strategy=%s mode=%s duration=%sh "
        "target=+%s max_loss=-%s risk=%s lev=%sx markets=%s balance=%s",
        session_id, user_id, environment, request.strategy, request.mode,
        request.duration_hours, request.target_profit, request.max_loss,
        risk_label, request.leverage, request.markets, balance,
    )

    env_label = "LIVE" if environment == "live" else "DEMO"
    try:
        await api.state.notifier.send(
            user_id,
            f"🚀 Auto trading started — session #{session_id}\n"
            f"Environment: {env_label}\n"
            f"Mode: {request.mode}\nDuration: {request.duration_hours}h\n"
            f"Strategies: {request.strategy}\n"
            f"Target: +{request.target_profit} USDT · Max loss: -{request.max_loss} USDT\n"
            f"Risk/trade: {risk_label}\n"
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

    client = await okx_client_for_cached(user_id)
    try:
        try:
            tracked = session_manager.list_open_positions(session.id)
        except Exception:
            tracked = []

        try:
            live = await client.swap_positions()
        except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Manual stop: swap_positions failed: %s: %s",
                        type(exc).__name__, exc)
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
                    inst_id=tp.inst_id, side=close_side,
                    position_side=tp.pos_side, contracts=contracts,
                    client_order_id=close_key,
                )
                detail = await client.order_detail(tp.inst_id, accepted.get("ordId", ""))
                status = (detail or {}).get("state", "live").upper()
                fill = Decimal((detail or {}).get("avgPx") or live_pos.get("markPx") or "0")
                filled = Decimal((detail or {}).get("accFillSz") or "0")
                pnl = Decimal((detail or {}).get("pnl") or "0")
                database.reconcile_exchange_order(close_key, status, fill, filled, pnl)
                session_manager.mark_position_closed(tp.client_order_id, pnl)
            except Exception as exc:
                log.warning("Manual stop close failed for %s: %s: %s",
                            tp.inst_id, type(exc).__name__, exc)

        await asyncio.sleep(1.5)
        try:
            balance = await client.account_balance()
        except Exception:
            balance = session.current_balance
    finally:
        pass

    session_manager.end_session(session.id, EndReason.USER_STOP, balance)
    updated = session_manager.get_by_id(session.id)

    try:
        session_pnl = session_manager.session_realized_pnl(session.id)
    except Exception:
        session_pnl = Decimal("0")

    account_change = balance - session.starting_balance
    sp_sign = "+" if session_pnl >= 0 else "-"
    ac_sign = "+" if account_change >= 0 else "-"

    _portfolio_cache.pop(user_id, None)

    log.info(
        "Session %s STOPPED by user %s: env=%s pnl=%s (%d trades, W %d / L %d) "
        "account_change=%s balance=%s",
        session.id, user_id, session.config.environment, session_pnl,
        session.trades_count, session.wins_count, session.losses_count,
        account_change, balance,
    )

    env_label = "LIVE" if session.config.environment == "live" else "DEMO"
    try:
        await api.state.notifier.send(
            user_id,
            f"⏹️ Auto trading stopped — session #{session.id} ({env_label})\n\n"
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


@api.get("/api/auto/recommend")
async def auto_recommend(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    """Return the auto-risk recommendation for the user's current balance."""
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    client = await okx_client_for_cached(user_id)
    try:
        balance = await client.account_balance()
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"Could not read balance: {exc}") from None

    tier = classify(balance)
    rules = rules_for(balance)
    tier_labels = {
        "micro":    "Micro ($5 – $50)",
        "small":    "Small ($50 – $500)",
        "standard": "Standard ($500 – $5,000)",
        "large":    "Large ($5,000+)",
    }

    recommended_risk = {
        "micro":    "2.0",
        "small":    "1.5",
        "standard": "1.0",
        "large":    "0.75",
    }

    return {
        "balance": str(balance),
        "tier": tier.value,
        "tier_label": tier_labels.get(tier.value, "Unknown"),
        "risk_per_trade_pct": recommended_risk.get(tier.value, "1.0"),
        "max_concurrent": rules.max_concurrent,
        "max_daily_loss_pct": str(rules.max_daily_loss_pct),
        "concentration_cap_pct": str(rules.concentration_cap_pct),
        "max_total_exposure_pct": str(rules.max_total_exposure_pct),
        "safety_floor_pct": "80",
        "min_account_balance": str(MIN_ACCOUNT_BALANCE),
        "below_minimum": balance < MIN_ACCOUNT_BALANCE,
    }


@api.get("/api/auto/status")
async def auto_status(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])

    session = session_manager.get_active(user_id)
    if not session:
        return {"active": False, "session": None}

    try:
        client = await okx_client_for_cached(user_id)
        balance = await client.account_balance()
        session_manager.update_balance(session.id, balance)
        session = session_manager.get_by_id(session.id)
    except asyncio.CancelledError:
        return {"active": True, "session": _session_to_dict(session)}
    except Exception as exc:
        log.debug("auto_status balance refresh failed for %s: %s: %s",
                  user_id, type(exc).__name__, exc)

    return {"active": True, "session": _session_to_dict(session)}


@api.get("/api/auto/history")
async def auto_history(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    rows = session_manager.list_history(int(user["id"]), limit=20)
    return [_session_to_dict(s) for s in rows]


# ---------------------------------------------------------------------------
# Signals — read-only analysis of any OKX USDT perpetual.
# Reuses the same strategies the auto-trader uses. No orders, no sessions.
# ---------------------------------------------------------------------------

def _check_signal_rate_limit(user_id: int) -> None:
    """Per-user sliding-window rate limit. Raises 429 when over limit."""
    now = time.monotonic()
    hits = [t for t in _signal_rate_limit.get(user_id, []) if now - t < SIGNAL_RATE_WINDOW]
    if len(hits) >= SIGNAL_RATE_MAX:
        retry = int(SIGNAL_RATE_WINDOW - (now - hits[0])) + 1
        raise HTTPException(429, f"Too many signal requests. Retry in {retry}s.")
    hits.append(now)
    _signal_rate_limit[user_id] = hits


async def _persist_signal_async(user_id: int, token_input: str, result: dict) -> None:
    """Fire-and-forget persistence — never blocks the endpoint response."""
    try:
        database.insert_signal(
            user_id=user_id,
            inst_id=result["inst_id"],
            token_input=token_input,
            strategy=result["signal"].get("strategy") or "none",
            direction=result["signal"]["direction"],
            overall_confidence=int(result["signal"]["confidence"]),
            analysis=result,
        )
    except Exception as exc:
        log.warning("Failed to persist signal for %s: %s: %s",
                    user_id, type(exc).__name__, exc)


@api.post("/api/signals/analyze")
async def signals_analyze(
    request: SignalAnalyzeRequest,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    """Analyze a token and return a signal + 1:2..1:5 TP ladder with confidence.

    Public market data only — no API key required. The client is created
    per-request (matching the pattern used by /api/futures/leverage-options)
    and always closed in the finally block.
    """
    # Local imports keep api.py's module-level import graph untouched and
    # avoid importing signals/ before it exists on disk.
    from .signals import SignalNotSupported, analyze_token
    from .signals.analyzer import InsufficientHistory
    from .signals.resolver import resolve_token_strict

    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    _check_signal_rate_limit(user_id)

    client = OKXDemoClient("", "", "")
    try:
        # Resolve first so a cache hit doesn't fetch candles needlessly.
        try:
            inst_id, _ = await resolve_token_strict(request.token, client)
        except SignalNotSupported as exc:
            raise HTTPException(404, str(exc)) from None

        now = time.monotonic()
        cached = _signal_cache.get(inst_id)
        if cached and (now - cached[0]) < SIGNAL_CACHE_TTL:
            return cached[1]

        try:
            result = await analyze_token(request.token, client)
        except InsufficientHistory as exc:
            raise HTTPException(422, str(exc)) from None
        except SignalNotSupported as exc:
            raise HTTPException(404, str(exc)) from None

        _signal_cache[inst_id] = (time.monotonic(), result)
        asyncio.create_task(_persist_signal_async(user_id, request.token, result))
        return result
    except HTTPException:
        raise
    except (ExchangeError, aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(502, f"OKX market data unavailable: {exc}") from None
    finally:
        await client.close()


@api.get("/api/signals/history")
async def signals_history(
    limit: int = 50,
    x_telegram_init_data: str | None = Header(default=None),
) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    limit = max(1, min(limit, 200))
    return database.signals_history(int(user["id"]), limit=limit)
