from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.database import Database
from app.models import OrderIntent, Side
from app.risk import RiskManager, RiskRejected


def settings(path: Path) -> Settings:
    return Settings("token", 1, path, None, "paper", False, None, None, True, None, None, 3, Decimal("1"), Decimal("50"), Decimal("20"), frozenset({"BTCUSDT"}))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name) / "risk-test.db"
        self.db = Database(self.tmp); self.db.initialize()
        self.manager = RiskManager(settings(self.tmp), self.db)

    def tearDown(self):
        self.directory.cleanup()

    def test_rejects_disallowed_symbol(self):
        with self.assertRaisesRegex(RiskRejected, "allow-list"):
            self.manager.validate(OrderIntent(1, "DOGEUSDT", Side.BUY, Decimal("10"), "key"))

    def test_rejects_oversized_order(self):
        with self.assertRaisesRegex(RiskRejected, "≤"):
            self.manager.validate(OrderIntent(1, "BTCUSDT", Side.BUY, Decimal("51"), "key"))

    def test_kill_switch_disables_profile(self):
        self.db.upsert_user(1, "ada", "Ada")
        self.db.save_automation_profile(1, True, "rsi_reversion_demo", "BTCUSDT", Decimal("10"), 1, Decimal("2"), Decimal("4"))
        self.db.set_kill_switch(1)
        self.assertFalse(self.db.automation_profile(1)["enabled"])
