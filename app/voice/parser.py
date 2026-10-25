"""Convert a transcribed voice command into a structured intent."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional


IntentType = Literal[
    "trade_open", "trade_close",
    "query_pnl", "query_positions", "query_balance", "signal",
    "auto_start", "stop",
    "confirm", "cancel", "help", "unknown",
]


TOKEN_MAP = {
    "btc": "BTC-USDT-SWAP", "bitcoin": "BTC-USDT-SWAP",
    "eth": "ETH-USDT-SWAP", "ethereum": "ETH-USDT-SWAP", "ether": "ETH-USDT-SWAP",
    "sol": "SOL-USDT-SWAP", "solana": "SOL-USDT-SWAP",
    "doge": "DOGE-USDT-SWAP", "dogecoin": "DOGE-USDT-SWAP",
    "bnb": "BNB-USDT-SWAP",
    "xrp": "XRP-USDT-SWAP", "ripple": "XRP-USDT-SWAP",
    "ada": "ADA-USDT-SWAP", "cardano": "ADA-USDT-SWAP",
    "ltc": "LTC-USDT-SWAP", "litecoin": "LTC-USDT-SWAP",
    "link": "LINK-USDT-SWAP", "chainlink": "LINK-USDT-SWAP",
}

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}


@dataclass
class VoiceIntent:
    intent_type: IntentType
    raw_text: str = ""
    confidence: float = 1.0
    side: Optional[str] = None
    inst_id: Optional[str] = None
    leverage: Optional[int] = None
    margin_usdt: Optional[Decimal] = None
    rr: Optional[str] = None
    strategy: Optional[str] = None


def _word_to_int(w: str) -> Optional[int]:
    w = w.strip().lower()
    if w.isdigit():
        return int(w)
    if w in WORD_NUMBERS:
        return WORD_NUMBERS[w]
    parts = w.split()
    if len(parts) == 2:
        a, b = WORD_NUMBERS.get(parts[0]), WORD_NUMBERS.get(parts[1])
        if a and b:
            return a * b
    return None


def _extract_token(text: str) -> Optional[str]:
    words = re.findall(r"\b[a-z]+(?:[0-9]+[a-z]+)?\b", text.lower())
    for w in words:
        if w in TOKEN_MAP:
            return TOKEN_MAP[w]
        m = re.match(r"^(\d+)([a-z]+)$", w)
        if m and m.group(2) in TOKEN_MAP:
            return f"{m.group(1)}{m.group(2).upper()}-USDT-SWAP"
    return None


def _extract_leverage(text: str) -> Optional[int]:
    m = re.search(r"\b(\d+)\s*(?:x|×)\b", text.lower())
    if m:
        n = int(m.group(1))
        if 1 <= n <= 100:
            return n
    m = re.search(r"\b(one hundred|hundred|fifty|twenty|ten|five)\s*(?:x|×)\b", text.lower())
    if m:
        return _word_to_int(m.group(1))
    return None


def _extract_margin(text: str) -> Optional[Decimal]:
    for pat in (
        r"\$\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:dollars?|bucks?|usdt)",
        r"with\s+(\d+(?:\.\d+)?)",
    ):
        m = re.search(pat, text.lower())
        if m:
            try:
                return Decimal(m.group(1))
            except Exception:
                pass
    return None


def _extract_rr(text: str) -> Optional[str]:
    m = re.search(r"\b1\s*(?:to|:)\s*([2-5])\b", text.lower())
    if m:
        return f"1:{m.group(1)}"
    m = re.search(r"\bone\s+to\s+(two|three|four|five)\b", text.lower())
    if m:
        n = _word_to_int(m.group(1))
        if n:
            return f"1:{n}"
    return None


def _extract_strategy(text: str) -> Optional[str]:
    t = text.lower()
    if "both" in t or "all strateg" in t:
        return "both"
    if "breakout" in t or "break out" in t:
        return "daily_breakout"
    if "pullback" in t or "pull back" in t:
        return "daily_pullback"
    return None


def parse_voice_command(text: str) -> VoiceIntent:
    original = text
    t = text.lower().strip()
    t = re.sub(r"[^\w\s\$%:.\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    if t in {"confirm", "yes", "yeah", "yep", "go", "go ahead", "execute", "do it", "place it"}:
        return VoiceIntent(intent_type="confirm", raw_text=original)
    if t in {"cancel", "no", "nope", "abort", "nevermind", "never mind", "forget it"}:
        return VoiceIntent(intent_type="cancel", raw_text=original)
    if t in {"help", "commands", "what can you do", "what can i say"}:
        return VoiceIntent(intent_type="help", raw_text=original)

    if (re.search(r"\b(stop|kill)\b.*\b(everything|auto|all|trading|session)\b", t)
            or t in {"stop", "kill switch", "emergency stop"}):
        return VoiceIntent(intent_type="stop", raw_text=original)

    if re.search(r"\b(pnl|p&l|profit|how much am i (up|down)|am i (up|down)|my earnings)\b", t):
        return VoiceIntent(intent_type="query_pnl", raw_text=original)
    if re.search(r"\b(positions|open trades|my trades|what do i have|holdings|what am i holding)\b", t):
        return VoiceIntent(intent_type="query_positions", raw_text=original)
    if re.search(r"\b(balance|account value|how much (money|usdt)|how much is in)\b", t):
        return VoiceIntent(intent_type="query_balance", raw_text=original)

    m = re.search(r"\b(?:signal|analyze|analysis|check|look at)\s+(\w+)", t)
    if m:
        inst = _extract_token(m.group(1))
        if inst:
            return VoiceIntent(intent_type="signal", inst_id=inst, raw_text=original)

    if re.search(r"\b(start|begin|launch)\b.*\b(auto|automat|session)\b", t) or "auto trade" in t:
        return VoiceIntent(
            intent_type="auto_start",
            leverage=_extract_leverage(t),
            strategy=_extract_strategy(t),
            rr=_extract_rr(t),
            raw_text=original,
        )

    if re.search(r"\b(close|exit|shut|dump)\b", t):
        inst = _extract_token(t)
        if inst:
            return VoiceIntent(intent_type="trade_close", inst_id=inst, raw_text=original)

    side = None
    if re.search(r"\b(long|buy|bull)\b", t):
        side = "long"
    elif re.search(r"\b(short|sell|bear)\b", t):
        side = "short"
    if side:
        inst = _extract_token(t)
        if inst:
            return VoiceIntent(
                intent_type="trade_open",
                side=side,
                inst_id=inst,
                leverage=_extract_leverage(t),
                margin_usdt=_extract_margin(t),
                rr=_extract_rr(t),
                raw_text=original,
            )

    return VoiceIntent(intent_type="unknown", raw_text=original)
