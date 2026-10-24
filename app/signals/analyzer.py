"""Signal analysis for a single token.

Runs both registered strategies against the token's candle history,
scans backwards up to SCAN_WINDOW closed 4h candles to find the most
recent signal, builds the 1:2..1:5 TP ladder, attaches per-RR confidence.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional

from ..exchange import ExchangeError, OKXClient
from ..strategies.base import Candle, Signal, Strategy
from ..strategies.daily_breakout import DailyBreakoutStrategy
from ..strategies.daily_pullback import DailyPullbackStrategy
from .confidence import confidence_for
from .resolver import SignalNotSupported, resolve_token_strict

SCAN_WINDOW = 60
OKX_BAR = {"4h": "4H", "1d": "1D"}
_FOUR_H_MS = 4 * 60 * 60 * 1000


def _to_candles(rows: list[dict]) -> list[Candle]:
    return [
        Candle(
            timestamp=int(r["timestamp"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
        if r.get("confirm")
    ]


def _scan_for_last(
    strategy: Strategy,
    inst_id: str,
    c4h: list[Candle],
    c1d: list[Candle],
    max_scan: int,
) -> Optional[dict]:
    """Backward-scan 4h candles looking for the most recent fired signal."""
    upper = min(max_scan, len(c4h) - 1)
    for candles_ago in range(upper + 1):
        end = len(c4h) - candles_ago
        slice_4h = c4h[:end]
        cutoff_ts = slice_4h[-1].timestamp
        slice_1d = [c for c in c1d if c.timestamp <= cutoff_ts]

        sig = strategy.evaluate(inst_id, {"4h": slice_4h, "1d": slice_1d})
        if sig.action in ("long", "short") and sig.is_valid():
            return {"strategy": strategy.name, "signal": sig, "candles_ago": candles_ago}
        if sig.action == "long" or sig.action == "short":
            # Fired but invalid structure — keep scanning, don't surface a broken signal.
            continue
    return None


def _build_targets(
    direction: str,
    entry: Decimal,
    sl: Decimal,
    strategy: str,
    indicators: dict,
    instrument: dict,
) -> tuple[list[dict], int, str]:
    """Return (targets, overall_confidence, overall_basis)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return [], 0, "heuristic"

    tick = Decimal(instrument.get("tickSz") or "0.00000001")
    sign = Decimal("1") if direction == "long" else Decimal("-1")

    targets: list[dict] = []
    best_conf = 0
    bases_seen: set[str] = set()
    for rr in (2, 3, 4, 5):
        tp = entry + sign * Decimal(rr) * risk
        # snap to tick grid
        tp = (tp / tick).to_integral_value() * tick
        conf, basis = confidence_for(strategy, indicators, direction, rr)
        targets.append({
            "rr": f"1:{rr}",
            "price": str(tp),
            "confidence": conf,
            "basis": basis,
        })
        best_conf = max(best_conf, conf)
        bases_seen.add(basis)

    overall_basis = "calibrated" if bases_seen == {"calibrated"} else (
        "mixed" if len(bases_seen) > 1 else "heuristic"
    )
    return targets, best_conf, overall_basis


def _since_signal(
    direction: str,
    entry: Decimal,
    sl: Decimal,
    price_now: Decimal,
    first_target: Optional[Decimal],
) -> dict:
    risk = abs(entry - sl) if entry != sl else Decimal("0")
    if direction == "long":
        favorable = price_now - entry
        hit_sl = price_now <= sl
        hit_tp = first_target is not None and price_now >= first_target
    else:
        favorable = entry - price_now
        hit_sl = price_now >= sl
        hit_tp = first_target is not None and price_now <= first_target

    still_actionable = (
        risk > 0
        and not hit_sl
        and favorable < risk  # not chasing >1R favorable move
    )

    pct = ((price_now - entry) / entry * Decimal("100")) if entry > 0 else Decimal("0")
    return {
        "price_now": str(price_now),
        "pct_move": f"{pct:+.2f}%",
        "hit_tp": hit_tp,
        "hit_sl": hit_sl,
        "still_actionable": still_actionable,
    }


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _other_summary(other: Optional[dict], strategy_name: str) -> dict:
    if not other:
        return {"strategy": strategy_name, "direction": "none", "reason": "no signal in scan window"}
    return {
        "strategy": other["strategy"],
        "direction": other["signal"].action,
        "reason": other["signal"].reason,
        "candles_ago": other["candles_ago"],
    }


async def analyze_token(
    token_input: str,
    client: OKXClient,
) -> dict:
    """Main entry point. Returns a dict matching the /api/signals/analyze contract.

    Raises SignalNotSupported (→ 404) or InsufficientHistory (→ 422).
    """
    inst_id, instrument = await resolve_token_strict(token_input, client)

    # Fetch 4h + 1d + last price in parallel. 300 is OKX's max per request.
    import asyncio
    try:
        c4h_raw, c1d_raw, price_now = await asyncio.gather(
            client.candles(inst_id, OKX_BAR["4h"], limit=300),
            client.candles(inst_id, OKX_BAR["1d"], limit=100),
            client.last_price(inst_id),
        )
    except ExchangeError as exc:
        raise SignalNotSupported(f"OKX returned no market data for {inst_id}: {exc}") from exc

    c4h = _to_candles(c4h_raw)
    c1d = _to_candles(c1d_raw)

    # Strategy minimums: breakout needs 125 4h + 55 1d; pullback needs 60 + 60.
    if len(c4h) < 130 or len(c1d) < 60:
        raise InsufficientHistory(
            f"{inst_id} has only {len(c4h)} 4h and {len(c1d)} 1d candles. "
            f"Need at least 130 / 60. Try again in a few days."
        )

    strategies = [DailyBreakoutStrategy(), DailyPullbackStrategy()]
    results: dict[str, Optional[dict]] = {}
    for strategy in strategies:
        results[strategy.name] = _scan_for_last(
            strategy, inst_id, c4h, c1d, max_scan=SCAN_WINDOW
        )

    # Pick winner: the most recent fired signal; ties → breakout (validated).
    fired = [(name, r) for name, r in results.items() if r is not None]
    fired.sort(key=lambda x: (x[1]["candles_ago"], 0 if x[0] == "daily_breakout" else 1))

    if not fired:
        return _empty_response(inst_id, token_input, results, price_now)

    winner_name, winner_payload = fired[0]
    sig: Signal = winner_payload["signal"]
    direction = sig.action
    entry = sig.entry_price
    sl = sig.stop_loss
    indicators = dict(sig.indicators)
    # Add breakout_strength for confidence bucketing if missing.
    if winner_name == "daily_breakout" and "breakout_level" in indicators:
        bl = Decimal(str(indicators["breakout_level"]))
        if bl > 0:
            indicators["breakout_strength"] = float((entry - bl) / bl)

    targets, overall_conf, basis = _build_targets(
        direction, entry, sl, winner_name, indicators, instrument
    )

    first_target = Decimal(targets[0]["price"]) if targets else None
    since = _since_signal(direction, entry, sl, price_now, first_target)

    reasons = [
        sig.reason,
        f"Volume {indicators.get('volume_ratio', '?')}× 20-bar avg"
        if winner_name == "daily_breakout" else
        f"RSI {indicators.get('rsi', '?')}, ATR {indicators.get('atr_pct', 0)*100:.2f}%",
        f"Fired {winner_payload['candles_ago']} closed 4h candle(s) ago "
        f"({_iso_from_ms(c4h[-1 - winner_payload['candles_ago']].timestamp)})",
    ]
    for t in targets:
        tag = "calibrated from 6y backtests" if t["basis"] == "calibrated" else "heuristic estimate"
        reasons.append(f"{t['rr']} target hit confidence {t['confidence']}% ({tag})")

    other_name = [s for s in results if s != winner_name][0]
    return {
        "ok": True,
        "token_input": token_input.strip(),
        "inst_id": inst_id,
        "as_of": _iso_from_ms(c4h[-1].timestamp),
        "signal": {
            "direction": direction,
            "strategy": winner_name,
            "fired_at": _iso_from_ms(c4h[-1 - winner_payload["candles_ago"]].timestamp),
            "candles_ago": winner_payload["candles_ago"],
            "entry_ref": str(entry),
            "stop_loss": str(sl),
            "risk_per_unit": str(abs(entry - sl)),
            "targets": targets,
            "confidence": overall_conf,
            "confidence_basis": basis,
            "reasons": reasons,
        },
        "since_signal": since,
        "other_strategy": _other_summary(results[other_name], other_name),
    }


def _empty_response(inst_id, token_input, results, price_now):
    return {
        "ok": True,
        "token_input": token_input.strip(),
        "inst_id": inst_id,
        "as_of": datetime.now(UTC).isoformat(),
        "signal": {
            "direction": "none",
            "strategy": None,
            "fired_at": None,
            "candles_ago": None,
            "entry_ref": None,
            "stop_loss": None,
            "risk_per_unit": None,
            "targets": [],
            "confidence": 0,
            "confidence_basis": "heuristic",
            "reasons": [
                f"No signal in the last {SCAN_WINDOW} 4h candles (~{SCAN_WINDOW*4//24} days).",
            ],
        },
        "since_signal": {"price_now": str(price_now), "pct_move": "0.00%",
                         "hit_tp": False, "hit_sl": False, "still_actionable": False},
        "other_strategy": _other_summary(
            next((r for r in results.values() if r), None),
            next((n for n, r in results.items() if r), "daily_breakout"),
        ),
    }


class InsufficientHistory(RuntimeError):
    """Raised when the instrument exists but has too little history."""
