"""FastAPI router for the voice assistant."""
from __future__ import annotations

import logging
import time
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..exchange import ExchangeError, OKXClient, OKXDemoClient, OKXLiveClient
from ..security import CredentialVault
from . import get_context
from .handler import (
    build_confirmation_message,
    execute_auto_start,
    execute_close_all,
    execute_close_partial,
    execute_query,
    execute_stop,
    execute_trade_close,
    execute_trade_open,
    execute_update_sl,
    execute_update_tp,
)
from .parser import parse_voice_command

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voice", tags=["voice"])

_pending: dict[str, tuple[float, dict]] = {}
PENDING_TTL = 60.0

QUERY_INTENTS = {
    "query_pnl", "query_positions", "query_balance", "signal", "help",
    "session_status", "session_history", "signal_history", "order_history",
    "price_check", "leverage_check", "environment", "daily_summary",
    "win_rate", "best_trade", "worst_trade", "portfolio_breakdown",
    "period_summary",
}
CLIENT_REQUIRED_INTENTS = {
    "query_pnl", "query_positions", "query_balance", "signal",
    "price_check", "leverage_check", "portfolio_breakdown",
}
CONFIRMABLE_INTENTS = {
    "trade_open", "trade_close", "auto_start", "stop",
    "update_sl", "update_tp", "close_partial", "close_all",
}


class VoiceCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class VoiceCommandResponse(BaseModel):
    say: str
    pending: bool = False
    action: str | None = None


def _make_client(user_id: int, env: str, database, settings) -> OKXClient:
    creds = database.exchange_credentials(user_id)
    if not creds or creds.get("provider") != "okx":
        raise HTTPException(400, "No OKX connection")
    if not settings.encryption_key:
        raise HTTPException(503, "Vault not configured")
    vault = CredentialVault(settings.encryption_key)
    api_key = vault.decrypt(creds["encrypted_api_key"])
    api_secret = vault.decrypt(creds["encrypted_api_secret"])
    passphrase = vault.decrypt(creds["encrypted_passphrase"])
    if env == "live":
        if not getattr(settings, "allow_live_trading", False):
            raise HTTPException(503, "Live trading disabled")
        return OKXLiveClient(api_key, api_secret, passphrase)
    return OKXDemoClient(api_key, api_secret, passphrase)


async def _dispatch_confirmed(inner, client, user_id, database, session_manager, settings) -> str:
    if inner.intent_type == "trade_open":
        return await execute_trade_open(inner, client, user_id, database)
    if inner.intent_type == "trade_close":
        return await execute_trade_close(inner, client)
    if inner.intent_type == "auto_start":
        return await execute_auto_start(inner, client, user_id, session_manager, settings)
    if inner.intent_type == "stop":
        return await execute_stop(client, user_id, session_manager)
    if inner.intent_type == "update_sl":
        return await execute_update_sl(inner, client)
    if inner.intent_type == "update_tp":
        return await execute_update_tp(inner, client)
    if inner.intent_type == "close_partial":
        return await execute_close_partial(inner, client)
    if inner.intent_type == "close_all":
        return await execute_close_all(client, user_id)
    return "Nothing to execute."


@router.post("/command", response_model=VoiceCommandResponse)
async def voice_command(
    request: VoiceCommandRequest,
    authorization: str | None = Header(default=None),
) -> VoiceCommandResponse:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing device token")

    database, session_manager, settings = get_context()
    user_id, env = database.voice_device_lookup(token)
    if user_id is None:
        raise HTTPException(401, "Invalid device token")

    database.touch_voice_device(token)
    intent = parse_voice_command(request.text)

    now = time.monotonic()
    pending = _pending.pop(token, None)
    if pending and now - pending[0] > PENDING_TTL:
        pending = None

    if intent.intent_type == "confirm":
        if not pending:
            return VoiceCommandResponse(say="Nothing to confirm.")
        stored = pending[1]
        client = _make_client(stored["user_id"], stored["env"], database, settings)
        try:
            reply = await _dispatch_confirmed(
                stored["intent"], client, stored["user_id"],
                database, session_manager, settings,
            )
        except Exception as exc:
            log.warning("Voice execution failed: %s: %s", type(exc).__name__, exc)
            reply = f"Something went wrong: {exc}"
        finally:
            await client.close()
        return VoiceCommandResponse(say=reply)

    if intent.intent_type == "cancel":
        return VoiceCommandResponse(say="Cancelled." if pending else "Nothing to cancel.")

    if intent.intent_type in QUERY_INTENTS:
        client = None
        if intent.intent_type in CLIENT_REQUIRED_INTENTS:
            try:
                client = _make_client(user_id, env, database, settings)
            except HTTPException as exc:
                return VoiceCommandResponse(say=str(exc.detail))
        else:
            try:
                client = _make_client(user_id, env, database, settings)
            except HTTPException:
                client = None

        try:
            reply = await execute_query(
                intent, client, user_id, session_manager, database, env
            )
        finally:
            if client:
                await client.close()
        return VoiceCommandResponse(say=reply)

    if intent.intent_type in CONFIRMABLE_INTENTS:
        _pending[token] = (now, {"intent": intent, "user_id": user_id, "env": env})
        return VoiceCommandResponse(
            say=build_confirmation_message(intent),
            pending=True,
            action=intent.intent_type,
        )

    if intent.intent_type == "unknown":
        if intent.best_guess:
            return VoiceCommandResponse(
                say=(
                    f"I heard '{request.text}' — did you mean "
                    f"'{intent.best_guess}'? Try again or say help for options."
                )
            )
        return VoiceCommandResponse(
            say=f"I didn't understand '{request.text}'. Say help for options."
        )

    return VoiceCommandResponse(
        say=f"I didn't understand '{request.text}'. Say help for options."
    )


# ---- Device management (called from the Telegram Mini App) ----

class CreateDeviceRequest(BaseModel):
    device_name: str = Field(default="My Phone", max_length=64)


@router.post("/devices")
async def create_device(
    request: CreateDeviceRequest,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    from ..web_auth import TelegramAuthError, validate_init_data

    if not x_telegram_init_data:
        raise HTTPException(401, "Missing Telegram authorization")
    database, _, settings = get_context()
    try:
        user = validate_init_data(x_telegram_init_data, settings.telegram_token)
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None

    token = secrets.token_urlsafe(32)
    database.create_voice_device(token, int(user["id"]), request.device_name)
    return {"device_token": token, "device_name": request.device_name}


@router.get("/devices")
async def list_devices(
    x_telegram_init_data: str | None = Header(default=None),
) -> list[dict]:
    from ..web_auth import TelegramAuthError, validate_init_data

    if not x_telegram_init_data:
        raise HTTPException(401, "Missing Telegram authorization")
    database, _, settings = get_context()
    try:
        user = validate_init_data(x_telegram_init_data, settings.telegram_token)
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None

    rows = database.list_voice_devices(int(user["id"]))
    for r in rows:
        r["device_token"] = r["device_token"][:8] + "…"
    return rows


@router.delete("/devices/{token_prefix}")
async def revoke_device(
    token_prefix: str,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict:
    from ..web_auth import TelegramAuthError, validate_init_data

    if not x_telegram_init_data:
        raise HTTPException(401, "Missing Telegram authorization")
    database, _, settings = get_context()
    try:
        user = validate_init_data(x_telegram_init_data, settings.telegram_token)
    except (TelegramAuthError, KeyError, ValueError):
        raise HTTPException(401, "Invalid Telegram authorization") from None

    ok = database.revoke_voice_device(int(user["id"]), token_prefix)
    return {"revoked": ok}
