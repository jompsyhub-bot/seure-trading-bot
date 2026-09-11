from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 600) -> dict:
    """Validate Telegram Mini App init data server-side; never trust browser user IDs."""
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash or "auth_date" not in values or "user" not in values:
        raise TelegramAuthError("Incomplete Telegram authorization data")
    try:
        if abs(time.time() - int(values["auth_date"])) > max_age_seconds:
            raise TelegramAuthError("Telegram authorization expired")
    except ValueError as exc:
        raise TelegramAuthError("Invalid Telegram authorization time") from exc
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise TelegramAuthError("Invalid Telegram authorization signature")
    try:
        return json.loads(values["user"])
    except json.JSONDecodeError as exc:
        raise TelegramAuthError("Invalid Telegram user data") from exc
