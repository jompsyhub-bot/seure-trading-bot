"""Trade-signal analysis for the Signals tab.

Read-only: runs the same strategies the auto-trader uses against an
arbitrary OKX perpetual and returns a signal + per-RR confidence ladder.
Does not touch orders, sessions, or risk management.
"""
from .resolver import SignalNotSupported, resolve_token_strict
from .analyzer import analyze_token

__all__ = ["SignalNotSupported", "resolve_token_strict", "analyze_token"]
