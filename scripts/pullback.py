#!/usr/bin/env python3
"""Backtest for the DailyPullback strategy on OKX public candles.

Modes:
  python pullback.py BTC-USDT-SWAP              # single-market backtest (original)
  python pullback.py BTC-USDT-SWAP --log-trades # with per-trade log
  python pullback.py --calibrate                # sweep 9 markets × 4 RR levels,
                                                # merge into app/signals/calibration.json

The calibration mode uses the SAME strategy instance production uses — no
reimplementation, no drift. Overrides only the TP distance for the RR sweep;
SL stays at the strategy's structural 2×ATR.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

sys.path.insert(0, ".")

from app.strategies.base import Candle
from app.strategies.daily_pullback import DailyPullbackStrategy


OKX_BASE = "https://www.okx.com"

CALIBRATION_OUTPUT = Path(__file__).resolve().parent.parent / "app" / "signals" / "calibration.json"

# Sweep levels: (TP multiple of risk, key used in calibration.json)
CALIBRATION_LEVELS = [(2.0, "rr_2"), (3.0, "rr_3"), (4.0, "rr_4"), (5.0, "rr_5")]

# Same 9 markets the breakout calibration uses, in OKX instId form.
CALIBRATION_MARKETS = [
    ("BTC-USDT-SWAP", "BTC"),
    ("ETH-USDT-SWAP", "ETH"),
    ("DOGE-USDT-SWAP", "DOGE"),
    ("SOL-USDT-SWAP", "SOL"),
    ("BNB-USDT-SWAP", "BNB"),
    ("XRP-USDT-SWAP", "XRP"),
    ("ADA-USDT-SWAP", "ADA"),
    ("LTC-USDT-SWAP", "LTC"),
    ("LINK-USDT-SWAP", "LINK"),
]

# Calibration fetches ~4.6 years of 4h bars per market (OKX returns what's available).
CALIBRATION_BARS_4H = 10000


async def fetch_candles(session, inst_id, bar, limit):
    url = f"{OKX_BASE}/api/v5/market/history-candles"
    rows = []
    after = None
    remaining = limit
    while remaining > 0:
        batch = min(remaining, 100)
        params = {"instId": inst_id, "bar": bar, "limit": str(batch)}
        if after is not None:
            params["after"] = str(after)

        data = None
        last_err = None
        for attempt in range(5):
            try:
                async with session.get(url, params=params) as r:
                    data = await r.json(content_type=None)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_err = exc
                wait = 2 ** attempt
                print(f"  network error ({attempt+1}/5), retrying in {wait}s: {exc}")
                await asyncio.sleep(wait)

        if data is None:
            raise RuntimeError(f"OKX unreachable after 5 retries: {last_err}")

        if data.get("code") != "0":
            raise RuntimeError(f"OKX fetch failed: {data.get('msg')}")
        chunk = data.get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        after = int(chunk[-1][0])
        remaining -= len(chunk)
        if len(chunk) < batch:
            break
        await asyncio.sleep(0.15)

    rows.sort(key=lambda r: int(r[0]))
    out = []
    seen = set()
    for r in rows:
        if len(r) < 9 or r[8] != "1":
            continue
        ts = int(r[0])
        if ts in seen:
            continue
        seen.add(ts)
        out.append(Candle(
            timestamp=ts,
            open=float(r[1]), high=float(r[2]), low=float(r[3]),
            close=float(r[4]), volume=float(r[5]),
        ))
    return out


@dataclass
class Trade:
    side: str
    entry_idx: int
    entry_ts: int
    entry: float
    sl: float
    tp: float
    fee_as_r: float = 0.0
    exit_idx: int = -1
    exit_ts: int = 0
    exit_price: float = 0.0
    outcome: str = ""
    bars_held: int = 0
    exit_kind: str = ""      # "tp" | "sl" | "timeout" | "open"
    bucket: str = ""


def _bucket_key(rsi, atr_pct, direction):
    """Must match app/signals/confidence.py::_pullback_bucket EXACTLY.

    The strategy stores round(rsi, 2) and round(atr_pct, 5) in its
    indicators dict, so re-rounding here is a no-op — included for symmetry
    with the boundary check, which is sensitive to the rounding.
    """
    r = round(float(rsi), 2)
    a = round(float(atr_pct), 5)
    rsi_b = "center" if abs(r - 50) < 8 else "edge"
    atr_b = "0.5-2.0%" if a < 0.02 else ">=2.0%"
    return f"{direction}|rsi:{rsi_b}|atr:{atr_b}"


def simulate(market, c4h, c1d, fee_rate=0.0005, slippage=0.0002,
             start_capital=1000.0, risk_pct=0.01, time_stop_bars=30,
             log_trades=False, tp_mult=None, capture_buckets=False):
    """Run the strategy over the given candle history.

    tp_mult: when set, overrides the strategy's native 2.5R take-profit
             with `tp_mult × risk`. SL is unchanged (structural 2×ATR).
    capture_buckets: attach the calibration bucket key + exit kind to every
             trade and return the trades list in the result.
    """
    strat = DailyPullbackStrategy()
    strat.TIME_STOP_BARS = time_stop_bars
    native_rr = strat.ATR_TP_MULT / strat.ATR_SL_MULT
    rr_win = tp_mult if tp_mult is not None else native_rr

    trades = []
    open_trade = None

    for i in range(60, len(c4h) - 1):
        bar = c4h[i]
        ts = bar.timestamp

        if open_trade is not None:
            open_trade.bars_held += 1
            hit = None
            if open_trade.side == "long":
                if bar.low <= open_trade.sl:
                    hit = ("loss", open_trade.sl, "sl")
                elif bar.high >= open_trade.tp:
                    hit = ("win", open_trade.tp, "tp")
            else:
                if bar.high >= open_trade.sl:
                    hit = ("loss", open_trade.sl, "sl")
                elif bar.low <= open_trade.tp:
                    hit = ("win", open_trade.tp, "tp")
            if hit is None and open_trade.bars_held >= time_stop_bars:
                hit = ("timeout", bar.close, "timeout")

            if hit is not None:
                open_trade.exit_idx = i
                open_trade.exit_ts = ts
                open_trade.exit_price = hit[1]
                open_trade.exit_kind = hit[2]
                if hit[0] == "timeout":
                    if open_trade.side == "long":
                        r = (bar.close - open_trade.entry) / (open_trade.entry - open_trade.sl)
                    else:
                        r = (open_trade.entry - bar.close) / (open_trade.sl - open_trade.entry)
                    open_trade.outcome = "win" if r > 0 else "loss"
                else:
                    open_trade.outcome = hit[0]
                trades.append(open_trade)
                if log_trades:
                    _log(open_trade)
                open_trade = None

            if open_trade is not None:
                continue

        window_4h = c4h[:i + 1]
        window_1d = [c for c in c1d if c.timestamp <= ts]
        if len(window_1d) < 60:
            continue

        signal = strat.evaluate(market, {"4h": window_4h, "1d": window_1d})
        if not signal.is_entry or not signal.is_valid():
            continue

        entry = float(signal.entry_price)
        sl = float(signal.stop_loss)

        # Native TP or RR-sweep override — SL is always the strategy's.
        if tp_mult is not None:
            risk = abs(entry - sl)
            if signal.action == "long":
                tp = entry + tp_mult * risk
            else:
                tp = entry - tp_mult * risk
        else:
            tp = float(signal.take_profit)

        if signal.action == "long":
            entry *= (1 + slippage)
        else:
            entry *= (1 - slippage)

        sl_pct = abs(entry - sl) / entry if entry > 0 else 0
        rt_cost = (fee_rate + slippage) * 2
        fee_r = rt_cost / sl_pct if sl_pct > 0 else 0

        open_trade = Trade(
            side=signal.action, entry_idx=i, entry_ts=ts,
            entry=entry, sl=sl, tp=tp, fee_as_r=fee_r,
        )
        if capture_buckets:
            open_trade.bucket = _bucket_key(
                signal.indicators.get("rsi", 50),
                signal.indicators.get("atr_pct", 0),
                signal.action,
            )

    if open_trade is not None:
        last = c4h[-1]
        open_trade.exit_idx = len(c4h) - 1
        open_trade.exit_ts = last.timestamp
        open_trade.exit_price = last.close
        open_trade.outcome = "open"
        open_trade.exit_kind = "open"
        trades.append(open_trade)

    closed = [t for t in trades if t.outcome in ("win", "loss")]
    wins = [t for t in closed if t.outcome == "win"]
    losses = [t for t in closed if t.outcome == "loss"]

    bal = start_capital
    peak = start_capital
    dd = 0.0
    r_multiples = []
    for t in closed:
        risk = bal * risk_pct
        r = rr_win if t.outcome == "win" else -1.0
        r -= t.fee_as_r
        bal += risk * r
        r_multiples.append(r)
        if bal > peak:
            peak = bal
        ddp = (peak - bal) / peak * 100 if peak > 0 else 0
        if ddp > dd:
            dd = ddp

    total = len(closed)
    win_rate = (len(wins) / total * 100) if total else 0.0
    avg_r = sum(r_multiples) / total if total else 0.0

    result = {
        "market": market,
        "bars": len(c4h),
        "date_from": datetime.fromtimestamp(c4h[0].timestamp/1000, UTC).strftime("%Y-%m-%d"),
        "date_to": datetime.fromtimestamp(c4h[-1].timestamp/1000, UTC).strftime("%Y-%m-%d"),
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "expectancy_r": round(avg_r, 4),
        "start_balance": start_capital,
        "end_balance": round(bal, 2),
        "return_pct": round((bal / start_capital - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
    }
    if capture_buckets:
        result["trades_detail"] = trades
    return result


def _log(t):
    ts_in = datetime.fromtimestamp(t.entry_ts/1000, UTC).strftime("%m-%d %H:%M")
    ts_out = datetime.fromtimestamp(t.exit_ts/1000, UTC).strftime("%m-%d %H:%M")
    tag = "WIN " if t.outcome == "win" else ("OPEN" if t.outcome == "open" else "LOSS")
    print(f"  {tag} {t.side:>5}  {ts_in} -> {ts_out}  "
          f"entry {t.entry:.2f} sl {t.sl:.2f} tp {t.tp:.2f} "
          f"exit {t.exit_price:.2f} ({t.bars_held} bars) [{t.exit_kind}]")


# ---------------------------------------------------------------------------
# Single-market mode (original behavior — unchanged output)
# ---------------------------------------------------------------------------

async def run_single(market, args):
    d1_limit = args.bars // 6 + 100

    timeout = aiohttp.ClientTimeout(total=240)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        print(f"Fetching {args.bars} 4h bars + {d1_limit} 1D bars for {market}...")
        c4h = await fetch_candles(session, market, "4H", args.bars)
        c1d = await fetch_candles(session, market, "1Dutc", d1_limit)

    print(f"Got {len(c4h)} 4h bars, {len(c1d)} 1D bars.")

    if len(c4h) < 100 or len(c1d) < 60:
        print("Not enough data.")
        return

    result = simulate(
        market, c4h, c1d,
        fee_rate=args.fee, slippage=args.slippage,
        start_capital=args.capital, risk_pct=args.risk,
        log_trades=args.log_trades,
    )

    print()
    print("=" * 62)
    print(f"BACKTEST — {result['market']}  [daily_pullback]")
    print("=" * 62)
    for k in ("date_from", "date_to", "bars", "trades", "wins", "losses",
              "win_rate_pct", "expectancy_r", "start_balance", "end_balance",
              "return_pct", "max_drawdown_pct"):
        print(f"  {k:>18}: {result[k]}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# Calibration mode
# ---------------------------------------------------------------------------

async def run_calibration(args):
    print(f"Calibration mode — {len(CALIBRATION_MARKETS)} markets × "
          f"{len(CALIBRATION_LEVELS)} RR levels")
    print(f"Bars per market (4h): {CALIBRATION_BARS_4H}")
    print(f"Output: {CALIBRATION_OUTPUT}")
    print()

    d1_limit = CALIBRATION_BARS_4H // 6 + 100

    timeout = aiohttp.ClientTimeout(total=600)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())

    # agg[bucket][rr_key] = {"wins": int, "n": int}
    agg: dict[str, dict[str, dict[str, int]]] = {}
    per_market: dict[str, dict] = {}

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for inst_id, label in CALIBRATION_MARKETS:
            print(f"  fetching {label} ({inst_id})...", end=" ", flush=True)
            try:
                c4h = await fetch_candles(session, inst_id, "4H", CALIBRATION_BARS_4H)
                c1d = await fetch_candles(session, inst_id, "1Dutc", d1_limit)
            except Exception as exc:
                print(f"FAILED: {type(exc).__name__}: {exc}")
                continue

            if len(c4h) < 500 or len(c1d) < 60:
                print(f"not enough history (4h={len(c4h)}, 1d={len(c1d)}), skipping")
                continue

            print(f"ok (4h={len(c4h)}, 1d={len(c1d)})")

            per_market[label] = {}
            for tp_mult, rr_key in CALIBRATION_LEVELS:
                r = simulate(
                    inst_id, c4h, c1d,
                    fee_rate=args.fee, slippage=args.slippage,
                    start_capital=args.capital, risk_pct=args.risk,
                    time_stop_bars=30,
                    tp_mult=tp_mult,
                    capture_buckets=True,
                )
                per_market[label][rr_key] = {
                    "trades": r["trades"],
                    "wins": r["wins"],
                    "win_rate_pct": r["win_rate_pct"],
                }
                for t in r["trades_detail"]:
                    if t.outcome == "open":
                        continue
                    bucket = t.bucket
                    if not bucket:
                        continue
                    is_win = (t.exit_kind == "tp")
                    agg.setdefault(bucket, {}).setdefault(rr_key, {"wins": 0, "n": 0})
                    agg[bucket][rr_key]["n"] += 1
                    if is_win:
                        agg[bucket][rr_key]["wins"] += 1

    # Convert to win_rate + n per bucket
    out_buckets: dict[str, dict] = {}
    for bucket, rrs in sorted(agg.items()):
        bucket_out: dict[str, dict] = {}
        for rr_key, counts in rrs.items():
            if counts["n"] > 0:
                bucket_out[rr_key] = {
                    "win_rate": round(counts["wins"] / counts["n"], 4),
                    "n": counts["n"],
                }
        if bucket_out:
            out_buckets[bucket] = bucket_out

    # Merge into existing calibration.json — preserves daily_breakout if present.
    existing: dict = {}
    if CALIBRATION_OUTPUT.exists():
        try:
            with open(CALIBRATION_OUTPUT, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["daily_pullback"] = out_buckets
    existing.setdefault("_meta", {})
    existing["_meta"]["pullback"] = {
        "built_at": datetime.now(UTC).isoformat(),
        "markets": [label for _, label in CALIBRATION_MARKETS],
        "rr_levels": [k for _, k in CALIBRATION_LEVELS],
        "data_source": "OKX USDT perpetuals",
        "bars_4h_fetched": CALIBRATION_BARS_4H,
        "time_stop_bars": 30,
        "per_market_summary": per_market,
    }

    CALIBRATION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    # Summary
    total_buckets = len(out_buckets)
    total_trades_all_rr = sum(
        sum(c["n"] for c in b.values()) for b in out_buckets.values()
    )
    well_sampled = sum(
        1 for b in out_buckets.values()
        if len(b) == 4 and all(c.get("n", 0) >= 20 for c in b.values())
    )

    print()
    print("=" * 108)
    print(f"Merged into {CALIBRATION_OUTPUT}")
    print(f"  daily_pullback buckets: {total_buckets} "
          f"({well_sampled} fully sampled: n≥20 at all 4 RR levels)")
    print(f"  Total trades across all RR sweeps: {total_trades_all_rr}")
    print()
    print("  Bucket                                                  n@2R  n@3R  n@4R  n@5R   win@2R  win@3R  win@4R  win@5R")
    print("  " + "-" * 108)
    for bucket in sorted(out_buckets):
        row = out_buckets[bucket]
        ns = "".join(f"{row.get(k, {}).get('n', 0):>5} " for k in ("rr_2", "rr_3", "rr_4", "rr_5"))
        wr = "".join(
            f"{row[k]['win_rate']*100:>6.1f}% " if k in row else "     —  "
            for k in ("rr_2", "rr_3", "rr_4", "rr_5")
        )
        print(f"  {bucket:<55} {ns} {wr}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main_async(args):
    if args.calibrate:
        await run_calibration(args)
    else:
        if not args.market:
            print("ERROR: pass a market (e.g. BTC-USDT-SWAP) or use --calibrate",
                  file=sys.stderr)
            sys.exit(2)
        await run_single(args.market, args)


def main():
    ap = argparse.ArgumentParser(
        description="Backtest the DailyPullback strategy on OKX candles.",
    )
    ap.add_argument("market", nargs="?",
                    help="OKX instId, e.g. BTC-USDT-SWAP (required unless --calibrate)")
    ap.add_argument("--bars", type=int, default=2000,
                    help="4h bars for single-market mode (default 2000)")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0002)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--log-trades", action="store_true")
    ap.add_argument("--calibrate", action="store_true",
                    help="Sweep 9 markets × 4 RR levels and merge into calibration.json")
    args = ap.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
