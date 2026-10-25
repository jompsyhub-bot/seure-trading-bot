"""Voice control for the trading bot.

Shared state is injected by api.py at startup to avoid circular imports.
The router is re-exported at the bottom AFTER configure/get_context are
defined — otherwise router.py's `from . import get_context` would try to
import a name that hasn't been defined yet (circular import).
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


# Re-export so `from .voice import router` returns the APIRouter instance,
# not the app.voice.router submodule. Import order matters — see docstring.
from .router import router  # noqa: E402, F401
