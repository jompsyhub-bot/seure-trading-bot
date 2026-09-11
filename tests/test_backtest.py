import unittest

from app.backtest import Candle, run_rsi_reversion


def candles(prices):
    return [Candle(i * 900000, p, p * 1.001, p * 0.999, p) for i, p in enumerate(prices)]


class BacktestTests(unittest.TestCase):
    def test_no_signal_has_no_trades(self):
        result = run_rsi_reversion(candles([100 + i for i in range(40)]), rsi_threshold=30, stop_loss_pct=2, take_profit_pct=4, order_notional=10)
        self.assertEqual(result["metrics"]["trades"], 0)
        self.assertEqual(result["metrics"]["ending_equity"], 1000.0)

    def test_reports_explicit_assumptions(self):
        result = run_rsi_reversion(candles([100 - i for i in range(40)]), rsi_threshold=30, stop_loss_pct=2, take_profit_pct=4, order_notional=10)
        self.assertEqual(result["assumptions"]["same_candle_rule"], "stop-loss first")
