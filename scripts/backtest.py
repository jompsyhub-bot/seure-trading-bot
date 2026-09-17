#!/usr/bin/env python3
"""Honest backtest for the Day Trading strategy.

Fetches OKX public 1h + 4h candles for one market, walks forward one bar at a
time, evaluates the strategy on closed candles only, and simulates entries and
exits. Reports win rate, R:R, expectancy, and max drawdown AFTER fees.

Usage:
    python -m scripts.backtest BTC-USDT-SWAP
    python -m scripts.backtest ETH-USDT-SWAP --months 6 --fee 0.0005
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp

# Make the app package importable when running from the repo root
sys.path.insert(0, ".")

from app.strategies.base import Candle
from app.strategies.day_trading import DayTradingStrategy


OKX_BASE = "https://www.okx.com"
BAR_MS = {"1H": 3600_000, "4H": 14400_000}


async def fetch_candles(session: aiohttp.ClientSession, inst_id: str, bar: str, limit: int = 300) -> list[Candle]:
    """Fetch closed candles from OKX public REST, oldest first."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    rows: list[list[str]] = []
    after = None
    remaining = limit
    while remaining > 0:
        batch = min(remaining, 300)
        params = {"instId": inst_id, "bar": bar, "limit": str(batch)}
        if after is not None:
            params["after"] = str(after)
        async with session.get(url, params=params) as r:
            data = await r.json(content_type=None)
        if data.get("code") != "0":
            raise RuntimeError(f"OKX candle fetch failed: {data.get('msg')}")
        chunk = data.get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        after = int(chunk[-1][0])
        remaining -= len(chunk)
        if len(chunk) < batch:
            break

    rows.sort(key=lambda r: int(r[0]))   # oldest first
    out = []
    for r in rows:
        if r[8] != "1":
            continue  # skip the still-forming candle
        out.append(Candle(
            timestamp=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
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
    exit_idx: int = -1
    exit_ts: int = 0
    exit_price: float = 0.0
    outcome: str = ""     # "win" | "loss" | "open"


def simulate(
    market: str,
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    fee_rate: float = 0.0005,     # 0.05% taker, per side
    slippage: float = 0.0002,     # 0.02% each side
    start_capital: float = 1000.0,
    risk_pct: float = 0.01,       # 1% of balance per trade
) -> dict:
    """Walk forward, one 1h bar at a time. At each closed bar, ask the strategy
    for a signal. If we get an entry, open a trade and simulate forward until
    TP or SL is hit. Track balance, fees, and stats."""
    strat = DayTradingStrategy()

    # Map 4h candles to the 4h window that would have been visible at each
    # 1h bar's close. Real code does this with a live feed; here we slice.
    balance = start_capital
    peak_balance = start_capital
    max_dd_pct = 0.0
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for i in range(210, len(candles_1h) - 1):
        bar = candles_1h[i]
        ts = bar.timestamp

        # --- manage an open trade -----------------------------------
        if open_trade is not None:
            hi, lo = bar.high, bar.low
            if open_trade.side == "long":
                if lo <= open_trade.sl:
                    open_trade.exit_idx = i
                    open_trade.exit_ts = ts
                    open_trade.exit_price = open_trade.sl
                    open_trade.outcome = "loss"
                    balance -= (balance * risk_pct) * 1.0
                    balance -= (open_trade.entry + open_trade.exit_price) * 0  # fee modeled below
                    trades.append(open_trade)
                    open_trade = None
                elif hi >= open_trade.tp:
                    open_trade.exit_idx = i
                    open_trade.exit_ts = ts
                    open_trade.exit_price = open_trade.tp
                    open_trade.outcome = "win"
                    balance += (balance * risk_pct) * 2.0
                    trades.append(open_trade)
                    open_trade = None
            else:  # short
                if hi >= open_trade.sl:
                    open_trade.exit_idx = i
                    open_trade.exit_ts = ts
                    open_trade.exit_price = open_trade.sl
                    open_trade.outcome = "loss"
                    balance -= (balance * risk_pct) * 1.0
                    trades.append(open_trade)
                    open_trade = None
                elif lo <= open_trade.tp:
                    open_trade.exit_idx = i
                    open_trade.exit_ts = ts
                    open_trade.exit_price = open_trade.tp
                    open_trade.outcome = "win"
                    balance += (balance * risk_pct) * 2.0
                    trades.append(open_trade)
                    open_trade = None

            if open_trade is not None:
                continue  # still in a trade, don't look for new entries

        # --- ask the strategy --------------------------------------
        window_1h = candles_1h[: i + 1]
        window_4h = [c for c in candles_4h if c.timestamp <= ts]
        if len(window_4h) < 210:
            continue
        signal = strat.evaluate(market, {"1h": window_1h, "4h": window_4h})
        if not signal.is_entry or not signal.is_valid():
            continue

        entry = float(signal.entry_price)
        sl = float(signal.stop_loss)
        tp = float(signal.take_profit)

        # apply slippage on entry
        if signal.action == "long":
            entry *= (1 + slippage)
        else:
            entry *= (1 - slippage)

        # Fees: (fee_rate + slippage) * 2 sides as a fraction of notional,
        # converted to a fraction of risk (since risk = SL distance * size).
        sl_dist_pct = abs(entry - sl) / entry
        rt_cost_pct = (fee_rate + slippage) * 2
        fee_cost_as_r = rt_cost_pct / sl_dist_pct if sl_dist_pct > 0 else 0

        open_trade = Trade(
            side=signal.action,
            entry_idx=i,
            entry_ts=ts,
            entry=entry,
            sl=sl,
            tp=tp,
        )
        # stash the fee burden on the trade for accounting at close
        setattr(open_trade, "fee_as_r", fee_cost_as_r)

    # Close the very last open trade at market for accounting
    if open_trade is not None:
        last = candles_1h[-1]
        open_trade.exit_idx = len(candles_1h) - 1
        open_trade.exit_ts = last.timestamp
        open_trade.exit_price = last.close
        open_trade.outcome = "open"
        trades.append(open_trade)

    # --- report -----------------------------------------------------
    closed = [t for t in trades if t.outcome in ("win", "loss")]
    wins = [t for t in closed if t.outcome == "win"]
    losses = [t for t in closed if t.outcome == "loss"]

    # Recompute balance walking fee-as-R from each closed trade
    bal = start_capital
    peak = start_capital
    dd = 0.0
    r_multiples: list[float] = []
    for t in closed:
        risk = bal * risk_pct
        if t.outcome == "win":
            r = 2.0
        else:
            r = -1.0
        r -= getattr(t, "fee_as_r", 0.0)
        bal += risk * r
        r_multiples.append(r)
        if bal > peak:
            peak = bal
        dd_pct = (peak - bal) / peak * 100
        dd = max(dd, dd_pct)

    total = len(closed)
    win_rate = (len(wins) / total * 100) if total else 0.0
    avg_r = sum(r_multiples) / total if total else 0.0

    return {
        "market": market,
        "bars_tested": len(candles_1h),
        "date_from": datetime.fromtimestamp(candles_1h[0].timestamp / 1000, UTC).isoformat(),
        "date_to": datetime.fromtimestamp(candles_1h[-1].timestamp / 1000, UTC).isoformat(),
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "avg_r_per_trade": round(avg_r, 4),
        "expectancy_r": round(avg_r, 4),
        "start_balance": start_capital,
        "end_balance": round(bal, 2),
        "return_pct": round((bal / start_capital - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the Day Trading strategy on OKX public candles.")
    ap.add_argument("market", help="e.g. BTC-USDT-SWAP")
    ap.add_argument("--bars", type=int, default=1200, help="number of 1h bars of history (default 1200 ≈ 50 days)")
    ap.add_argument("--fee", type=float, default=0.0005, help="taker fee per side, default 0.0005 (0.05%%)")
    ap.add_argument("--slippage", type=float, default=0.0002, help="slippage per side, default 0.0002 (0.02%%)")
    ap.add_argument("--risk", type=float, default=0.01, help="fraction of balance risked per trade (default 0.01)")
    ap.add_argument("--capital", type=float, default=1000.0, help="starting balance (default 1000)")
    args = ap.parse_args()

    # Need 4h bars covering the same period as the 1h bars
    bars_4h = args.bars // 4 + 250

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print(f"Fetching {args.bars} 1h bars for {args.market}...")
        c1h = await fetch_candles(session, args.market, "1H", args.bars)
        print(f"Fetching {bars_4h} 4h bars...")
        c4h = await fetch_candles(session, args.market, "4H", bars_4h)

    if len(c1h) < 210 or len(c4h) < 210:
        print("Not enough data. Try a larger --bars value.")
        return

    result = simulate(
        args.market, c1h, c4h,
        fee_rate=args.fee, slippage=args.slippage,
        start_capital=args.capital, risk_pct=args.risk,
    )

    print()
    print("=" * 60)
    print(f"BACKTEST RESULTS — {result['market']}")
    print("=" * 60)
    for k in ("date_from", "date_to", "bars_tested", "trades",
              "wins", "losses", "win_rate_pct", "avg_r_per_trade",
              "expectancy_r", "start_balance", "end_balance",
              "return_pct", "max_drawdown_pct"):
        print(f"  {k:>20}: {result[k]}")
    print("=" * 60)
    print()
    print("Interpretation:")
    print(f"  - Break-even win rate at 1:2 R:R after fees is ~38%.")
    print(f"  - This run produced a {result['win_rate_pct']}% win rate")
    print(f"    with {result['expectancy_r']:+.3f}R expectancy per trade.")
    if result["expectancy_r"] > 0:
        print("  - ✅ Positive edge. Worth running on live Demo.")
    else:
        print("  - ❌ Negative edge. Do NOT run this on Demo yet.")


if __name__ == "__main__":
    asyncio.run(main())
