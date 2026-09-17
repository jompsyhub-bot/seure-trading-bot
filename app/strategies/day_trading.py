from __future__ import annotations

from decimal import Decimal

from .base import Candle, Signal, Strategy


# -------- indicator helpers (pure Python, no pandas needed) ----------

def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out: list[float] = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss <= 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def _atr(candles: list[Candle], period: int = 14) -> list[float]:
    if len(candles) < period + 1:
        return []
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


# -------- the strategy ------------------------------------------------

class DayTradingStrategy(Strategy):
    """1h pullback-to-EMA21 with a 4h trend filter.

    Entry (long):
      - 4h EMA50 > EMA200 (macro uptrend)
      - 1h EMA21 > EMA50 (aligned)
      - Price approached EMA21 within 1.0% in the last 3 candles (pullback)
      - 1h RSI(14) between 40 and 65 (not overbought, not collapsing)
      - Volatility sane: 0.3% <= ATR(14)/price <= 5.0%
      - Latest 1h candle closed green (confirmation)
      - Latest 1h volume > 1.2 * 20-candle average (conviction)

    Exit:
      - SL = entry - 1.5 * ATR(14)     (long)
      - TP = entry + 3.0 * ATR(14)     (long)
      - R:R = 1:2 on price distance

    Short is the mirror.

    If any condition fails, returns skip with a reason. No fabrication.
    """

    name = "day_trading"
    description = "1h pullback continuation with 4h trend filter"
    required_timeframes = ("1h", "4h")

    ATR_SL_MULT = 1.5
    ATR_TP_MULT = 3.0
    MIN_ATR_PCT = 0.003   # 0.3% of price
    MAX_ATR_PCT = 0.050   # 5.0% of price
    PULLBACK_WINDOW = 3
    PULLBACK_TOLERANCE = 0.01   # within 1.0% of EMA21
    VOLUME_LOOKBACK = 20
    VOLUME_MULT = 1.2
    RSI_LONG_MIN = 40.0
    RSI_LONG_MAX = 65.0
    RSI_SHORT_MIN = 35.0
    RSI_SHORT_MAX = 60.0

    def __init__(self) -> None:
        self._skip_streak: dict[str, int] = {}

    # -- helpers ------------------------------------------------------

    def _skip(self, market: str, reason: str, **indicators) -> Signal:
        return Signal(action="skip", market=market, reason=reason, indicators=indicators)

    @staticmethod
    def _closes(candles: list[Candle]) -> list[float]:
        return [c.close for c in candles]

    @staticmethod
    def _volumes(candles: list[Candle]) -> list[float]:
        return [c.volume for c in candles]

    def _trend_up_4h(self, candles: list[Candle]) -> bool | None:
        if len(candles) < 210:
            return None
        closes = self._closes(candles)
        ema50 = _ema(closes, 50)
        ema200 = _ema(closes, 200)
        if not ema50 or not ema200:
            return None
        return ema50[-1] > ema200[-1]

    def _trend_up_1h(self, candles: list[Candle]) -> bool | None:
        if len(candles) < 60:
            return None
        closes = self._closes(candles)
        ema21 = _ema(closes, 21)
        ema50 = _ema(closes, 50)
        if not ema21 or not ema50:
            return None
        return ema21[-1] > ema50[-1]

    def _pulled_back_to_ema21(self, candles: list[Candle], ema21_now: float, for_long: bool) -> bool:
        window = candles[-self.PULLBACK_WINDOW:]
        tolerance = ema21_now * self.PULLBACK_TOLERANCE
        if for_long:
            return any(c.low <= ema21_now + tolerance for c in window)
        return any(c.high >= ema21_now - tolerance for c in window)

    # -- main ---------------------------------------------------------

    def evaluate(self, market: str, candles: dict[str, list[Candle]]) -> Signal:
        c1h = candles.get("1h") or []
        c4h = candles.get("4h") or []

        if len(c1h) < 210 or len(c4h) < 210:
            return self._skip(market, "insufficient history (need 210+ candles)")

        closes_1h = self._closes(c1h)
        volumes_1h = self._volumes(c1h)
        last = c1h[-1]
        entry = last.close

        # --- trend ---------------------------------------------------
        trend_up_4h = self._trend_up_4h(c4h)
        if trend_up_4h is None:
            return self._skip(market, "4h trend unclear")
        trend_up_1h = self._trend_up_1h(c1h)
        if trend_up_1h is None:
            return self._skip(market, "1h trend unclear")

        if trend_up_4h and not trend_up_1h:
            return self._skip(market, "4h up but 1h not aligned — chop")
        if not trend_up_4h and trend_up_1h:
            return self._skip(market, "1h up against 4h down — countertrend, refuse")

        is_long = trend_up_4h  # and trend_up_1h by elimination above

        # --- ATR & volatility filter ---------------------------------
        atr_series = _atr(c1h, 14)
        if not atr_series:
            return self._skip(market, "ATR unavailable")
        atr = atr_series[-1]
        atr_pct = atr / entry if entry > 0 else 0
        if atr_pct < self.MIN_ATR_PCT:
            return self._skip(market, f"market too quiet (ATR {atr_pct*100:.2f}%)", atr_pct=atr_pct)
        if atr_pct > self.MAX_ATR_PCT:
            return self._skip(market, f"market too wild (ATR {atr_pct*100:.2f}%)", atr_pct=atr_pct)

        # --- RSI -----------------------------------------------------
        rsi_series = _rsi(closes_1h, 14)
        if not rsi_series:
            return self._skip(market, "RSI unavailable")
        rsi = rsi_series[-1]
        if is_long and not (self.RSI_LONG_MIN <= rsi <= self.RSI_LONG_MAX):
            return self._skip(market, f"RSI {rsi:.1f} outside long window", rsi=rsi)
        if not is_long and not (self.RSI_SHORT_MIN <= rsi <= self.RSI_SHORT_MAX):
            return self._skip(market, f"RSI {rsi:.1f} outside short window", rsi=rsi)

        # --- pullback to EMA21 ---------------------------------------
        ema21_series = _ema(closes_1h, 21)
        if not ema21_series:
            return self._skip(market, "EMA21 unavailable")
        ema21_now = ema21_series[-1]
        if not self._pulled_back_to_ema21(c1h, ema21_now, for_long=is_long):
            return self._skip(market, "no pullback to EMA21 in last 3 candles")

        # --- candle confirmation -------------------------------------
        if is_long and last.close <= last.open:
            return self._skip(market, "last 1h candle not green")
        if not is_long and last.close >= last.open:
            return self._skip(market, "last 1h candle not red")

        # --- volume conviction ---------------------------------------
        if len(volumes_1h) < self.VOLUME_LOOKBACK:
            return self._skip(market, "not enough volume history")
        avg_vol = sum(volumes_1h[-self.VOLUME_LOOKBACK:]) / self.VOLUME_LOOKBACK
        if avg_vol <= 0:
            return self._skip(market, "zero average volume")
        vol_ratio = last.volume / avg_vol
        if vol_ratio < self.VOLUME_MULT:
            return self._skip(market, f"weak volume ({vol_ratio:.2f}x average)")

        # --- build the signal ----------------------------------------
        entry_d = Decimal(str(round(entry, 8)))
        sl_distance = atr * self.ATR_SL_MULT
        tp_distance = atr * self.ATR_TP_MULT

        if is_long:
            sl_d = Decimal(str(round(entry - sl_distance, 8)))
            tp_d = Decimal(str(round(entry + tp_distance, 8)))
            action = "long"
        else:
            sl_d = Decimal(str(round(entry + sl_distance, 8)))
            tp_d = Decimal(str(round(entry - tp_distance, 8)))
            action = "short"

        return Signal(
            action=action,
            market=market,
            reason=(
                f"{'uptrend' if is_long else 'downtrend'} continuation, "
                f"pullback to EMA21, RSI {rsi:.1f}, vol {vol_ratio:.2f}x, "
                f"ATR {atr_pct*100:.2f}%"
            ),
            entry_price=entry_d,
            stop_loss=sl_d,
            take_profit=tp_d,
            atr=Decimal(str(round(atr, 8))),
            risk_reward=self.ATR_TP_MULT / self.ATR_SL_MULT,  # = 2.0
            indicators={
                "rsi": round(rsi, 2),
                "atr": round(atr, 8),
                "atr_pct": round(atr_pct, 5),
                "ema21": round(ema21_now, 8),
                "volume_ratio": round(vol_ratio, 2),
            },
        )
