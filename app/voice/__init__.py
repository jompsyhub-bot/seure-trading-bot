"""Voice control for the trading bot.

Shared state is injected by api.py at startup to avoid circular imports.
"""
from __future__ import annotations

_ctx: dict = {}


def configure(database, session_manager, settings) -> None:
    _ctx["database"] = database
    _ctx["session_manager"] = session_manager
    _ctx["settings"] = settings


def get_context() -> tuple:
    if "database" not in _ctx:
        raise RuntimeError("voice.configure() not called at startup")
    return _ctx["database"], _ctx["session_manager"], _ctx["settings"]
