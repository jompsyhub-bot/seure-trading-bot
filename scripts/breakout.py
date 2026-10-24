"""Year-by-year breakdown + calibration for the daily_breakout strategy.

Modes:
  python breakout.py                # year-by-year analysis (default, unchanged)
  python breakout.py --calibrate    # merges daily_breakout into app/signals/calibration.json

The calibration mode runs 36 backtests (9 markets × 4 RR levels) using the
production lookback (20d) and produces per-bucket win rates at each RR level
(2R, 3R, 4R, 5R) in the exact schema that app/signals/confidence.py expects.

The --calibrate write merges into the existing calibration.json rather than
overwriting it, so running backtest_pullback.py --calibrate before or after
this preserves both strategies' buckets.

Fees: 0.05% per side. Same parameters as the strategy.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import time
from pathlib import Path

import aiohttp


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def _ema(values, period):
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - prev["close"]),
                       abs(c["low"] - prev["close"])))
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

MIN_ATR_PCT = 0.005
MAX_ATR_PCT = 0.06
FEE_RATE = 0.0005
TIME_STOP_BARS = 30
BARS_PER_DAY = 6

VOLUME_LOOKBACK = 20
VOLUME_MULT = 1.5
SL_BUFFER_ATR = 0.3
MIN_SL_ATR = 1.5
R_MULT = 3.0
EMA_FAST_1D = 20
EMA_SLOW_1D = 50

LOOKBACKS = [5, 10, 15, 20, 30, 40, 50]
PRODUCTION_LOOKBACK = 20

CALIBRATION_LEVELS = [(2.0, "rr_2"), (3.0, "rr_3"), (4.0, "rr_4"), (5.0, "rr_5")]

MARKETS = [
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("DOGEUSDT", "DOGE"),
    ("SOLUSDT", "SOL"),
    ("BNBUSDT", "BNB"),
    ("XRPUSDT", "XRP"),
    ("ADAUSDT", "ADA"),
    ("LTCUSDT", "LTC"),
    ("LINKUSDT", "LINK"),
]


def precompute(c4h, c1d):
    n = len(c4h)
    daily_up_arr = [None] * n
    atr_arr = [None] * n

    c1d_sorted = sorted(c1d, key=lambda x: x["timestamp"])
    running_closes = []
    daily_idx = 0

    for i, bar in enumerate(c4h):
        ts = bar["timestamp"]
        while daily_idx < len(c1d_sorted) and c1d_sorted[daily_idx]["timestamp"] <= ts:
            running_closes.append(c1d_sorted[daily_idx]["close"])
            daily_idx += 1
        if len(running_closes) >= EMA_SLOW_1D:
            e20 = _ema(running_closes, EMA_FAST_1D)
            e50 = _ema(running_closes, EMA_SLOW_1D)
            if e20 and e50:
                daily_up_arr[i] = e20[-1] > e50[-1]

    atr_series = _atr(c4h, 14)
    offset = len(c4h) - len(atr_series)
    for k, val in enumerate(atr_series):
        atr_arr[k + offset] = val

    return daily_up_arr, atr_arr


def _bucket_key(vol_ratio, atr_pct, strength):
    """Must match app/signals/confidence.py::_breakout_bucket EXACTLY.

    The strategy stores round(vol_ratio, 2) and round(atr_pct, 5) in its
    indicators dict, so the bucketing rounds identically here.
    """
    v = round(vol_ratio, 2)
    a = round(atr_pct, 5)
    vol_b = "1.5-2.0" if v < 2.0 else ">=2.0"
    atr_b = "0.5-2.0%" if a < 0.02 else ">=2.0%"
    str_b = "<1%" if strength < 0.01 else ">=1%"
    return f"vol:{vol_b}|atr:{atr_b}|str:{str_b}"


def run_backtest(c4h, daily_up_arr, atr_arr, lookback_days,
                 starting_equity=1000.0, risk_pct=1.0,
                 tp_mult=R_MULT, capture_buckets=False):
    """Backtest one market.

    `tp_mult` overrides the strategy's native 3R take-profit. `capture_buckets`
    attaches the signal's bucket key and exit reason to every trade — needed
    for calibration mode, harmless otherwise.
    """
    lookback_bars = lookback_days * BARS_PER_DAY

    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    trades = []
    position = None
    total_fees = 0.0

    n = len(c4h)
    start_i = max(60, lookback_bars + VOLUME_LOOKBACK + 20)

    for i in range(start_i, n):
        bar = c4h[i]

        if position:
            exit_price = reason = None
            if bar["low"] <= position["sl"]:
                exit_price, reason = position["sl"], "SL"
            elif bar["high"] >= position["tp"]:
                exit_price, reason = position["tp"], "TP"

            if exit_price is None and i - position["entry_bar"] >= TIME_STOP_BARS:
                exit_price, reason = bar["close"], "time"

            if exit_price is not None:
                sl_dist = abs(position["entry"] - position["sl"])
                if sl_dist > 0:
                    units = (equity * risk_pct / 100) / sl_dist
                    gross = (exit_price - position["entry"]) * units
                    notional = position["entry"] * units
                    fees = notional * FEE_RATE * 2
                    total_fees += fees
                    pnl = gross - fees
                else:
                    pnl = 0
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100)

                exit_ts = bar["timestamp"] / 1000
                exit_year = dt.datetime.fromtimestamp(exit_ts, dt.UTC).year

                trade = {"pnl": pnl, "exit_year": exit_year}
                if capture_buckets:
                    trade["bucket"] = position["bucket"]
                    trade["win"] = (reason == "TP")
                    trade["reason"] = reason
                trades.append(trade)
                position = None

        if position is None:
            daily_up = daily_up_arr[i]
            if not daily_up:
                continue

            atr = atr_arr[i]
            if atr is None:
                continue
            entry = bar["close"]
            if entry <= 0:
                continue
            atr_pct = atr / entry
            if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
                continue

            prior = c4h[i - lookback_bars : i]
            if len(prior) < lookback_bars:
                continue
            breakout_level = max(c["high"] for c in prior)
            if entry <= breakout_level:
                continue

            vols = [c["volume"] for c in c4h[i - VOLUME_LOOKBACK : i]]
            avg_vol = sum(vols) / len(vols) if vols else 0
            if avg_vol <= 0 or bar["volume"] < avg_vol * VOLUME_MULT:
                continue
            vol_ratio = bar["volume"] / avg_vol

            sl_from_breakout = breakout_level - SL_BUFFER_ATR * atr
            sl_from_entry = entry - MIN_SL_ATR * atr
            sl = min(sl_from_breakout, sl_from_entry)
            risk = entry - sl
            if risk <= 0:
                continue
            tp = entry + tp_mult * risk

            bucket = _bucket_key(
                vol_ratio, atr_pct, (entry - breakout_level) / breakout_level
            ) if capture_buckets else None

            position = {
                "entry": entry, "sl": sl, "tp": tp, "entry_bar": i,
                "bucket": bucket,
            }

    wins = [t for t in trades if t["pnl"] > 0]

    by_year = {}
    for t in trades:
        y = t["exit_year"]
        by_year.setdefault(y, {"trades": 0, "pnl": 0.0, "wins": 0})
        by_year[y]["trades"] += 1
        by_year[y]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_year[y]["wins"] += 1

    return {
        "return_pct": round((equity / starting_equity - 1) * 100, 2),
        "max_dd_pct": round(max_dd, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "by_year": by_year,
        "trades_detail": trades,
    }


# ---------------------------------------------------------------------------
# Binance fetch
# ---------------------------------------------------------------------------

async def fetch_binance_klines(session, symbol, interval, total_candles):
    url = "https://fapi.binance.com/fapi/v1/klines"

    if interval.endswith("h"):
        interval_ms = int(interval[:-1]) * 3600 * 1000
    elif interval.endswith("d"):
        interval_ms = int(interval[:-1]) * 86400 * 1000
    else:
        raise ValueError(f"Unsupported interval: {interval}")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - total_candles * interval_ms - interval_ms * 10
    all_rows = []

    while len(all_rows) < total_candles:
        params = {"symbol": symbol, "interval": interval,
                  "limit": "1500", "startTime": str(start_ms)}
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Binance HTTP {resp.status}: {text[:200]}")
            rows = await resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        start_ms = rows[-1][0] + interval_ms
        if len(rows) < 1500:
            break

    all_rows = all_rows[-total_candles:]

    out = []
    for r in all_rows:
        out.append({
            "timestamp": int(r[0]),
            "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]),
            "volume": float(r[5]),
        })
    return out


# ---------------------------------------------------------------------------
# Year-by-year mode (original behavior)
# ---------------------------------------------------------------------------

async def main_yearly():
    total_4h = 13500
    total_1d = 2300

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    timeout = aiohttp.ClientTimeout(total=120)

    agg = {lb: {} for lb in LOOKBACKS}
    agg_total = {lb: {"returns": [], "dds": [], "trades": [], "wrs": []} for lb in LOOKBACKS}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for symbol, label in MARKETS:
            print(f"  fetching {label}...", end=" ", flush=True)

            try:
                c4h = await fetch_binance_klines(session, symbol, "4h", total_4h)
                c1d = await fetch_binance_klines(session, symbol, "1d", total_1d)
            except Exception as exc:
                print(f"FAILED: {type(exc).__name__}: {exc}")
                continue

            if len(c4h) < 800:
                print(f"not enough history ({len(c4h)}), skipping")
                continue

            daily_up_arr, atr_arr = precompute(c4h, c1d)
            print(f"ok ({len(c4h)} bars)")

            for lb in LOOKBACKS:
                r = run_backtest(c4h, daily_up_arr, atr_arr, lb)
                agg_total[lb]["returns"].append(r["return_pct"])
                agg_total[lb]["dds"].append(r["max_dd_pct"])
                agg_total[lb]["trades"].append(r["trades"])
                agg_total[lb]["wrs"].append(r["win_rate"])
                for y, yd in r["by_year"].items():
                    agg[lb].setdefault(y, {"pnl": 0.0, "trades": 0, "wins": 0})
                    agg[lb][y]["pnl"] += yd["pnl"]
                    agg[lb][y]["trades"] += yd["trades"]
                    agg[lb][y]["wins"] += yd["wins"]

    all_years = sorted({y for lb in agg for y in agg[lb]})

    print()
    print("=" * 120)
    print("YEAR-BY-YEAR AGGREGATE PnL (sum across 9 markets, net of fees)")
    print("=" * 120)
    header = f"  {'Lookback':>8}  " + "  ".join(f"{y:>9}" for y in all_years) + f"  {'Total':>10}  {'PosYears':>9}"
    print(header)
    print("  " + "-" * 116)

    summary = []
    for lb in LOOKBACKS:
        row = f"  {lb:>6}d  "
        total = 0.0
        pos_years = 0
        counted_years = 0
        for y in all_years:
            pnl = agg[lb].get(y, {}).get("pnl", 0.0)
            total += pnl
            if y not in (2020, 2026):
                counted_years += 1
                if pnl > 0:
                    pos_years += 1
            row += f"  ${pnl:>8.2f}"
        row += f"  ${total:>9.2f}"
        row += f"  {pos_years}/{counted_years}"
        print(row)
        summary.append((lb, total, pos_years, counted_years))

    print()
    print("=" * 120)
    print("SUMMARY — sorted by consistency (positive full years 2021-2025)")
    print("=" * 120)
    print(f"  {'Lookback':>8}  {'PosYears':>9}  {'TotalPnL':>12}  {'AvgRet':>9}  "
          f"{'AvgMaxDD':>10}  {'TotalTrades':>12}  {'AvgWinR':>9}")
    print("  " + "-" * 90)

    summary.sort(key=lambda x: (-x[2], -x[1]))
    for lb, total, pos_years, counted_years in summary:
        returns = agg_total[lb]["returns"]
        dds = agg_total[lb]["dds"]
        trades = agg_total[lb]["trades"]
        wrs = agg_total[lb]["wrs"]
        avg_ret = sum(returns) / len(returns) if returns else 0
        avg_dd = sum(dds) / len(dds) if dds else 0
        total_trades = sum(trades)
        avg_wr = sum(wrs) / len(wrs) if wrs else 0

        print(f"  {lb:>6}d  "
              f"{pos_years}/{counted_years:<7} "
              f"${total:>11.2f} "
              f"{avg_ret:>8.2f}% "
              f"{avg_dd:>9.2f}% "
              f"{total_trades:>12} "
              f"{avg_wr:>8.1f}%")


# ---------------------------------------------------------------------------
# Calibration mode — merges daily_breakout into app/signals/calibration.json
# ---------------------------------------------------------------------------

CALIBRATION_OUTPUT = Path(__file__).resolve().parent.parent / "app" / "signals" / "calibration.json"


async def main_calibrate():
    print(f"Calibration mode — lookback={PRODUCTION_LOOKBACK}d, "
          f"RR levels={[k for _, k in CALIBRATION_LEVELS]}")
    print(f"Output: {CALIBRATION_OUTPUT}")
    print()

    total_4h = 13500
    total_1d = 2300

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    timeout = aiohttp.ClientTimeout(total=120)

    # agg[bucket][rr_key] = {"wins": int, "n": int}
    agg: dict[str, dict[str, dict[str, int]]] = {}
    per_market: dict[str, dict] = {}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for symbol, label in MARKETS:
            print(f"  fetching {label}...", end=" ", flush=True)
            try:
                c4h = await fetch_binance_klines(session, symbol, "4h", total_4h)
                c1d = await fetch_binance_klines(session, symbol, "1d", total_1d)
            except Exception as exc:
                print(f"FAILED: {type(exc).__name__}: {exc}")
                continue

            if len(c4h) < 800:
                print(f"not enough history ({len(c4h)}), skipping")
                continue

            daily_up_arr, atr_arr = precompute(c4h, c1d)
            print(f"ok ({len(c4h)} bars)")

            per_market[label] = {}
            for tp_mult, rr_key in CALIBRATION_LEVELS:
                r = run_backtest(
                    c4h, daily_up_arr, atr_arr,
                    PRODUCTION_LOOKBACK,
                    tp_mult=tp_mult,
                    capture_buckets=True,
                )
                per_market[label][rr_key] = {
                    "trades": r["trades"], "wins": r["wins"],
                    "win_rate_pct": r["win_rate"],
                }
                for t in r["trades_detail"]:
                    bucket = t["bucket"]
                    agg.setdefault(bucket, {}).setdefault(rr_key, {"wins": 0, "n": 0})
                    agg[bucket][rr_key]["n"] += 1
                    if t["win"]:
                        agg[bucket][rr_key]["wins"] += 1

    # Convert to win_rate + keep counts
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

    # Merge into existing calibration.json — preserves daily_pullback if present.
    existing: dict = {}
    if CALIBRATION_OUTPUT.exists():
        try:
            with open(CALIBRATION_OUTPUT, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["daily_breakout"] = out_buckets
    existing.setdefault("_meta", {})
    existing["_meta"]["breakout"] = {
        "built_at": dt.datetime.now(dt.UTC).isoformat(),
        "markets": [label for _, label in MARKETS],
        "lookback_days": PRODUCTION_LOOKBACK,
        "rr_levels": [k for _, k in CALIBRATION_LEVELS],
        "data_source": "Binance USDT perpetuals",
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
        if all(c.get("n", 0) >= 20 for c in b.values()) and len(b) == 4
    )

    print()
    print("=" * 90)
    print(f"Merged into {CALIBRATION_OUTPUT}")
    print(f"  daily_breakout buckets: {total_buckets} "
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

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", action="store_true",
                        help="Run per-RR calibration and merge into app/signals/calibration.json")
    args = parser.parse_args()

    if args.calibrate:
        asyncio.run(main_calibrate())
    else:
        asyncio.run(main_yearly())


if __name__ == "__main__":
    main()
