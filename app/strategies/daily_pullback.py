from __future__ import annotations

from decimal import Decimal

from .base import Candle, Signal, Strategy


def _ema(values, period):
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(values, period=14):
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss <= 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


class DailyPullbackStrategy(Strategy):
    name = "daily_pullback"
    description = "Daily-biased 4h pullback continuation"
    required_timeframes = ("4h", "1d")

    EMA_FAST_1D = 20
    EMA_SLOW_1D = 50
    EMA_21_4H = 20
    PULLBACK_TOL = 0.01
    PULLBACK_WINDOW = 4
    RSI_LONG_MIN = 35.0
    RSI_LONG_MAX = 55.0
    RSI_SHORT_MIN = 45.0
    RSI_SHORT_MAX = 65.0
    ATR_SL_MULT = 2.0
    ATR_TP_MULT = 5.0
    TIME_STOP_BARS = 30
    MIN_ATR_PCT = 0.005
    MAX_ATR_PCT = 0.06

    def _skip(self, market, reason, **indicators):
        return Signal(action="skip", market=market, reason=reason, indicators=indicators)

    @staticmethod
    def _closes(candles):
        return [c.close for c in candles]

    def evaluate(self, market, candles):
        c4h = candles.get("4h") or []
        c1d = candles.get("1d") or []

        if len(c4h) < 60 or len(c1d) < 60:
            return self._skip(market, "insufficient history")

        closes_4h = self._closes(c4h)
        closes_1d = self._closes(c1d)
        last = c4h[-1]
        entry = last.close

        ema20_1d = _ema(closes_1d, self.EMA_FAST_1D)
        ema50_1d = _ema(closes_1d, self.EMA_SLOW_1D)
        if not ema20_1d or not ema50_1d:
            return self._skip(market, "1D EMA unavailable")
        daily_up = ema20_1d[-1] > ema50_1d[-1]
        daily_ema20_now = ema20_1d[-1]

        is_long = daily_up

        ema20_4h = _ema(closes_4h, self.EMA_21_4H)
        if not ema20_4h:
            return self._skip(market, "4h EMA unavailable")
        ema20_now = ema20_4h[-1]
        window = c4h[-self.PULLBACK_WINDOW:]
        tolerance = ema20_now * self.PULLBACK_TOL

        if is_long:
            pulled = any(c.low <= ema20_now + tolerance for c in window)
        else:
            pulled = any(c.high >= ema20_now - tolerance for c in window)
        if not pulled:
            return self._skip(market, "no pullback to 4h EMA20")

        rsi_series = _rsi(closes_4h, 14)
        if not rsi_series:
            return self._skip(market, "RSI unavailable")
        rsi = rsi_series[-1]
        if is_long and not (self.RSI_LONG_MIN <= rsi <= self.RSI_LONG_MAX):
            return self._skip(market, "RSI %.1f outside long zone" % rsi)
        if (not is_long) and not (self.RSI_SHORT_MIN <= rsi <= self.RSI_SHORT_MAX):
            return self._skip(market, "RSI %.1f outside short zone" % rsi)

        if is_long and entry < daily_ema20_now * 0.98:
            return self._skip(market, "broke below daily EMA20 by >2%")
        if (not is_long) and entry > daily_ema20_now * 1.02:
            return self._skip(market, "broke above daily EMA20 by >2%")

        if is_long and last.close <= last.open:
            return self._skip(market, "last 4h candle not green")
        if (not is_long) and last.close >= last.open:
            return self._skip(market, "last 4h candle not red")

        atr_series = _atr(c4h, 14)
        if not atr_series:
            return self._skip(market, "ATR unavailable")
        atr = atr_series[-1]
        atr_pct = atr / entry if entry > 0 else 0
        if atr_pct < self.MIN_ATR_PCT or atr_pct > self.MAX_ATR_PCT:
            return self._skip(market, "ATR %.2f%% out of range" % (atr_pct * 100))

        sl_dist = atr * self.ATR_SL_MULT
        tp_dist = atr * self.ATR_TP_MULT

        if is_long:
            sl = entry - sl_dist
            tp = entry + tp_dist
            action = "long"
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist
            action = "short"

        return Signal(
            action=action,
            market=market,
            reason="%s on 1D, 4h pullback to EMA20, RSI %.1f" % (
                "uptrend" if is_long else "downtrend", rsi),
            entry_price=Decimal(str(round(entry, 8))),
            stop_loss=Decimal(str(round(sl, 8))),
            take_profit=Decimal(str(round(tp, 8))),
            atr=Decimal(str(round(atr, 8))),
            risk_reward=self.ATR_TP_MULT / self.ATR_SL_MULT,
            indicators={
                "rsi": round(rsi, 2),
                "atr_pct": round(atr_pct, 5),
                "ema20_1d": round(ema20_1d[-1], 8),
                "ema50_1d": round(ema50_1d[-1], 8),
                "ema20_4h": round(ema20_now, 8),
            },
        )
