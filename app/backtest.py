from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from time import time

@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float


class MarketDataError(RuntimeError):
    pass


class PublicBybitMarket:
    """Public market data only; no user credentials are ever used for backtests."""
    url = "https://api.bybit.com/v5/market/kline"

    async def candles(self, symbol: str, interval: str, limit: int = 1000) -> list[Candle]:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=20)
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(self.url, params={"category": "spot", "symbol": symbol, "interval": interval, "limit": str(limit)}) as response:
                payload = await response.json()
        rows = payload.get("result", {}).get("list", [])
        if payload.get("retCode") != 0 or len(rows) < 100:
            raise MarketDataError("Bybit did not return enough historical candles")
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in sorted(rows, key=lambda item: int(item[0]))]


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    values: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return values
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, period + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, period + 1)]
    average_gain, average_loss = sum(gains) / period, sum(losses) / period
    values[period] = 100.0 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)
    for index in range(period + 1, len(closes)):
        delta = closes[index] - closes[index - 1]
        average_gain = (average_gain * (period - 1) + max(delta, 0)) / period
        average_loss = (average_loss * (period - 1) + max(-delta, 0)) / period
        values[index] = 100.0 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)
    return values


def run_rsi_reversion(candles: list[Candle], *, rsi_threshold: float, stop_loss_pct: float, take_profit_pct: float, order_notional: float, fee_pct: float = 0.10, slippage_pct: float = 0.05, starting_equity: float = 1000.0) -> dict:
    """Long-only test. Uses next-bar entry and a pessimistic same-bar TP/SL rule."""
    if len(candles) < 30:
        raise ValueError("At least 30 candles are required")
    closes, indicators = [c.close for c in candles], rsi([c.close for c in candles])
    equity, peak, max_drawdown = starting_equity, starting_equity, 0.0
    points = [{"t": candles[0].timestamp, "v": round(equity, 2)}]
    trades, position = [], None
    total_fees = 0.0
    for i in range(15, len(candles)):
        bar = candles[i]
        if position:
            stop_hit, take_hit = bar.low <= position["stop"], bar.high >= position["take"]
            if stop_hit or take_hit:
                exit_price = position["stop"] if stop_hit else position["take"]
                gross = position["qty"] * (exit_price - position["entry"])
                exit_fee = position["qty"] * exit_price * fee_pct / 100
                total_fees += exit_fee
                pnl = gross - position["entry_fee"] - exit_fee
                equity += pnl
                trades.append({"entry_time": position["time"], "exit_time": bar.timestamp, "entry": round(position["entry"], 6), "exit": round(exit_price, 6), "pnl": round(pnl, 2), "reason": "stop" if stop_hit else "take-profit"})
                position = None
        if not position and i < len(candles) - 1 and indicators[i] is not None and indicators[i] <= rsi_threshold:
            entry = candles[i + 1].open * (1 + slippage_pct / 100)
            qty = order_notional / entry
            entry_fee = order_notional * fee_pct / 100
            total_fees += entry_fee
            position = {"time": candles[i + 1].timestamp, "entry": entry, "qty": qty, "entry_fee": entry_fee, "stop": entry * (1 - stop_loss_pct / 100), "take": entry * (1 + take_profit_pct / 100)}
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        points.append({"t": bar.timestamp, "v": round(equity, 2)})
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit, gross_loss = sum(t["pnl"] for t in wins), abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else (None if not gross_profit else "∞")
    return {"assumptions": {"fee_pct_each_side": fee_pct, "slippage_pct_entry": slippage_pct, "same_candle_rule": "stop-loss first", "entry_rule": "next candle open"}, "metrics": {"starting_equity": starting_equity, "ending_equity": round(equity, 2), "return_pct": round((equity / starting_equity - 1) * 100, 2), "trades": len(trades), "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0, "profit_factor": profit_factor, "max_drawdown_pct": round(max_drawdown, 2), "fees": round(total_fees, 2)}, "trades": trades[-50:], "equity": points[::max(1, len(points) // 160)]}
