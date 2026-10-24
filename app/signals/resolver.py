"""Strict token → OKX perpetual instId resolver.

Rules (per user decision):
  * Futures (SWAP) only. USDT-quoted. No spot, no USDC.
  * "BTC/USDT"           → BTC-USDT-SWAP
  * "BTC-USDT-SWAP"      → BTC-USDT-SWAP
  * "BTC"                → BTC-USDT-SWAP (USDT assumed)
  * "1000PEPE/USDT"      → matches OKX 1000PEPE-USDT-SWAP
  * "PEPE/USDT"          → 404 not_supported (OKX has no PEPE-USDT-SWAP)
"""
from __future__ import annotations

from ..exchange import ExchangeError, OKXClient


class SignalNotSupported(RuntimeError):
    """Raised when the token isn't listed as a USDT perpetual on OKX."""


def _normalize_inst_id(token_input: str) -> str:
    raw = (token_input or "").strip().upper()
    if not raw:
        raise SignalNotSupported("Enter a token, e.g. BTC/USDT.")

    if raw.endswith("-SWAP"):
        return raw

    # Accept "BTC/USDT", "BTC-USDT", "BTCUSDT", or bare "BTC".
    if "/" in raw:
        base, _, quote = raw.partition("/")
        if quote and quote != "USDT":
            raise SignalNotSupported(
                f"Only USDT perpetuals are supported (got {raw}). Try {base}/USDT."
            )
        return f"{base.strip()}-USDT-SWAP"

    if "-" in raw:
        parts = raw.split("-")
        if len(parts) >= 2 and parts[-1] not in {"USDT", "SWAP"}:
            raise SignalNotSupported(
                f"Only USDT perpetuals are supported (got {raw}). Try {parts[0]}/USDT."
            )
        if parts[-1] == "USDT":
            return f"{parts[0]}-USDT-SWAP"
        return f"{parts[0]}-USDT-SWAP"

    if raw.endswith("USDT") and len(raw) > 4:
        return f"{raw[:-4]}-USDT-SWAP"

    return f"{raw}-USDT-SWAP"


async def resolve_token_strict(
    token_input: str, client: OKXClient
) -> tuple[str, dict]:
    """Return (inst_id, instrument_dict). Raises SignalNotSupported if missing."""
    inst_id = _normalize_inst_id(token_input)
    try:
        instrument = await client.swap_instrument(inst_id)
    except ExchangeError as exc:
        raise SignalNotSupported(
            f"{token_input.strip()} is not available for USDT futures trading on OKX."
        ) from exc
    if instrument.get("state") != "live":
        raise SignalNotSupported(
            f"{inst_id} is listed on OKX but is not currently in the live state."
        )
    return inst_id, instrument
