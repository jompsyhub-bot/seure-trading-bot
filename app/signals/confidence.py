"""Confidence scoring for signals.

Two backends, blended per-RR level:
  * calibrated — historical win-rate of the setup bucket, from calibration.json
                 (populated offline by backtest scripts; absent file → all heuristic)
  * heuristic  — computed from live indicators; used when the bucket has
                 fewer than MIN_SAMPLES backtest trades.

Every confidence returned carries a "basis" label so the UI can be honest
about which one it's showing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

Basis = Literal["calibrated", "heuristic"]

MIN_SAMPLES = 20
_CALIBRATION_PATH = Path(__file__).parent / "calibration.json"

# Per-RR confidence decay — applied to the raw 0..100 heuristic before rounding.
# Higher RR = historically lower hit probability.
_RR_DECAY = {2: 1.00, 3: 0.85, 4: 0.70, 5: 0.55}


def _load_calibration() -> dict:
    if not _CALIBRATION_PATH.exists():
        return {}
    try:
        with open(_CALIBRATION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


_CALIBRATION = _load_calibration()


# ---------------------------------------------------------------------------
# Bucket keys — must match the keys written into calibration.json
# ---------------------------------------------------------------------------

def _breakout_bucket(indicators: dict) -> str:
    vol = indicators.get("volume_ratio", 0)
    atr = indicators.get("atr_pct", 0)
    strength = indicators.get("breakout_strength", 0)
    vol_b = "1.5-2.0" if vol < 2.0 else ">=2.0"
    atr_b = "0.5-2.0%" if atr < 0.02 else ">=2.0%"
    str_b = "<1%" if strength < 0.01 else ">=1%"
    return f"vol:{vol_b}|atr:{atr_b}|str:{str_b}"


def _pullback_bucket(indicators: dict, direction: str) -> str:
    rsi = indicators.get("rsi", 50)
    atr = indicators.get("atr_pct", 0)
    center = 50.0
    rsi_b = "center" if abs(rsi - center) < 8 else "edge"
    atr_b = "0.5-2.0%" if atr < 0.02 else ">=2.0%"
    return f"{direction}|rsi:{rsi_b}|atr:{atr_b}"


def _bucket_key(strategy: str, indicators: dict, direction: str) -> str:
    if strategy == "daily_breakout":
        return _breakout_bucket(indicators)
    if strategy == "daily_pullback":
        return _pullback_bucket(indicators, direction)
    return "default"


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------

def _breakout_heuristic(indicators: dict) -> float:
    """0..100, base confidence from breakout strength."""
    vol = min(indicators.get("volume_ratio", 1.5) / 3.0, 1.0)      # 1.5→0.5, 3.0→1.0
    strength = min(indicators.get("breakout_strength", 0) / 0.03, 1.0)  # 3% → 1.0
    atr = indicators.get("atr_pct", 0.02)
    atr_fit = 1.0 - abs(atr - 0.02) / 0.04                          # best at 2% ATR
    atr_fit = max(0.0, min(atr_fit, 1.0))
    return 30.0 + 35.0 * vol + 25.0 * strength + 10.0 * atr_fit


def _pullback_heuristic(indicators: dict) -> float:
    """0..100, base confidence from pullback quality."""
    rsi = indicators.get("rsi", 50)
    rsi_fit = max(0.0, 1.0 - abs(rsi - 50) / 15.0)                  # best at 50
    atr = indicators.get("atr_pct", 0.02)
    atr_fit = max(0.0, 1.0 - abs(atr - 0.02) / 0.04)
    return 35.0 + 40.0 * rsi_fit + 25.0 * atr_fit


def _heuristic(strategy: str, indicators: dict, rr: int) -> int:
    raw = (
        _breakout_heuristic(indicators)
        if strategy == "daily_breakout"
        else _pullback_heuristic(indicators)
    )
    return max(1, min(99, round(raw * _RR_DECAY.get(rr, 0.5))))


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def confidence_for(
    strategy: str,
    indicators: dict,
    direction: str,
    rr: int,
) -> tuple[int, Basis]:
    bucket = _bucket_key(strategy, indicators, direction)
    entry = _CALIBRATION.get(strategy, {}).get(bucket, {}).get(f"rr_{rr}")
    if entry and entry.get("n", 0) >= MIN_SAMPLES:
        return round(entry["win_rate"] * 100), "calibrated"
    return _heuristic(strategy, indicators, rr), "heuristic"


def has_calibration() -> bool:
    """True if any calibration data is loaded (drives the UI 'calibrated' badge)."""
    return bool(_CALIBRATION)
