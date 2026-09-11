from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Literal
from contextlib import asynccontextmanager
import asyncio

import aiohttp

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .database import Database
from .exchange import BybitClient, ExchangeError, OKXDemoClient
from .security import CredentialVault
from .web_auth import TelegramAuthError, validate_init_data
from .automation import DemoAutomationWorker
from .notifications import TelegramNotifier
from .backtest import MarketDataError, PublicBybitMarket, run_rsi_reversion

settings = Settings.from_env()
database = Database(settings.database_path, settings.database_url)
database.initialize()
static_dir = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the experimental worker only when credential encryption is available."""
    task = None
    worker = None
    notifier = TelegramNotifier(settings.telegram_token)
    app.state.notifier = notifier
    if settings.encryption_key:
        worker = DemoAutomationWorker(database, CredentialVault(settings.encryption_key), notifier)
        task = asyncio.create_task(worker.run(), name="demo-automation-worker")
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
    symbol: Literal["BTCUSDT", "ETHUSDT"]
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
    symbol: Literal["BTCUSDT", "ETHUSDT"]


class FuturesPreviewRequest(BaseModel):
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    side: Literal["long", "short"]
    leverage: int = Field(ge=1, le=100)
    margin_usdt: Decimal = Field(gt=0)


def telegram_user(init_data: str | None) -> dict:
    if not init_data:
        raise HTTPException(401, "Missing Telegram authorization")
    try:
        user = validate_init_data(init_data, settings.telegram_token)
        database.upsert_user(int(user["id"]), user.get("username"), user.get("first_name"))
        return user
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None


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
    return FileResponse(static_dir / "index.html")


@api.get("/futures")
async def futures_ticket() -> FileResponse:
    return FileResponse(static_dir / "futures.html")


@api.get("/health")
async def health() -> dict:
    return {"ok": True, "mode": settings.trading_mode, "automation_environment": "demo-only"}


@api.get("/health/okx")
async def okx_connectivity() -> dict:
    """Credential-free Render-to-OKX reachability check for the next adapter."""
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
    dashboard = database.dashboard(int(user["id"]))
    if request.enabled and (dashboard.get("environment") != "demo" or dashboard.get("provider") != "bybit"):
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
    await api.state.notifier.send(int(user["id"]), "🔒 Exchange Demo connection removed and automation stopped.")
    return {"connected": False}


@api.get("/api/history")
async def history(x_telegram_init_data: str | None = Header(default=None)) -> list[dict]:
    user = telegram_user(x_telegram_init_data)
    return database.history(int(user["id"]))


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
    inst_id = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP"}[request.symbol]
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
    """Calculate an OKX Demo perpetual order safely; this endpoint never submits it."""
    user = telegram_user(x_telegram_init_data)
    user_id = int(user["id"])
    credentials = database.exchange_credentials(user_id)
    if not credentials or credentials.get("provider") != "okx" or credentials.get("environment") != "demo":
        raise HTTPException(400, "Connect an OKX Demo account first.")
    if not settings.encryption_key or not credentials.get("encrypted_passphrase"):
        raise HTTPException(503, "OKX Demo credentials are incomplete. Reconnect the account.")
    vault = CredentialVault(settings.encryption_key)
    client = OKXDemoClient(vault.decrypt(credentials["encrypted_api_key"]), vault.decrypt(credentials["encrypted_api_secret"]), vault.decrypt(credentials["encrypted_passphrase"]))
    inst_id = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP"}[request.symbol]
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
