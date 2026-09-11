import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from app.web_auth import TelegramAuthError, validate_init_data


def signed_data(token: str) -> str:
    values = {"auth_date": str(int(time.time())), "query_id": "abc", "user": json.dumps({"id": 42, "first_name": "Ada"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class WebAuthTests(unittest.TestCase):
    def test_accepts_valid_telegram_data(self):
        self.assertEqual(validate_init_data(signed_data("bot-token"), "bot-token")["id"], 42)

    def test_rejects_tampered_data(self):
        with self.assertRaises(TelegramAuthError):
            validate_init_data(signed_data("bot-token") + "tampered", "bot-token")
