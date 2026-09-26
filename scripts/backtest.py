#!/usr/bin/env python3
"""Backtest for the DailyPullback strategy on OKX public candles."""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

sys.path.insert(0, ".")

from app.strategies.base import Candle
from app.strategies.daily_pullback import DailyPullbackStrategy


OKX_BASE = "https://www.okx.com"


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


def simulate(market, c4h, c1d, fee_rate=0.0005, slippage=0.0002,
             start_capital=1000.0, risk_pct=0.01, time_stop_bars=30,
             log_trades=False):
    strat = DailyPullbackStrategy()
    strat.TIME_STOP_BARS = time_stop_bars
    rr_win = strat.ATR_TP_MULT / strat.ATR_SL_MULT

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
                    hit = ("loss", open_trade.sl)
                elif bar.high >= open_trade.tp:
                    hit = ("win", open_trade.tp)
            else:
                if bar.high >= open_trade.sl:
                    hit = ("loss", open_trade.sl)
                elif bar.low <= open_trade.tp:
                    hit = ("win", open_trade.tp)
            if hit is None and open_trade.bars_held >= time_stop_bars:
                hit = ("timeout", bar.close)

            if hit is not None:
                open_trade.exit_idx = i
                open_trade.exit_ts = ts
                open_trade.exit_price = hit[1]
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

    if open_trade is not None:
        last = c4h[-1]
        open_trade.exit_idx = len(c4h) - 1
        open_trade.exit_ts = last.timestamp
        open_trade.exit_price = last.close
        open_trade.outcome = "open"
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

    return {
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


def _log(t):
    ts_in = datetime.fromtimestamp(t.entry_ts/1000, UTC).strftime("%m-%d %H:%M")
    ts_out = datetime.fromtimestamp(t.exit_ts/1000, UTC).strftime("%m-%d %H:%M")
    tag = "WIN " if t.outcome == "win" else "LOSS"
    print(f"  {tag} {t.side:>5}  {ts_in} -> {ts_out}  "
          f"entry {t.entry:.2f} sl {t.sl:.2f} tp {t.tp:.2f} "
          f"exit {t.exit_price:.2f} ({t.bars_held} bars)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market")
    ap.add_argument("--bars", type=int, default=2000)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0002)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--log-trades", action="store_true")
    args = ap.parse_args()

    d1_limit = args.bars // 6 + 100

    timeout = aiohttp.ClientTimeout(total=240)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        print(f"Fetching {args.bars} 4h bars + {d1_limit} 1D bars for {args.market}...")
        c4h = await fetch_candles(session, args.market, "4H", args.bars)
        c1d = await fetch_candles(session, args.market, "1Dutc", d1_limit)

    print(f"Got {len(c4h)} 4h bars, {len(c1d)} 1D bars.")

    if len(c4h) < 100 or len(c1d) < 60:
        print("Not enough data.")
        return

    result = simulate(
        args.market, c4h, c1d,
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


if __name__ == "__main__":
    asyncio.run(main())
