"""Convert a transcribed voice command into a structured intent.

Matching pipeline (4 tiers, in order):
  1. Exact regex match — handles 95% of commands
  2. Fuzzy word match — "bitcon" → "bitcoin", "clsoe" → "close"
  3. Phonetic match — "bit-coin" → "bitcoin", "eeth" → "eth"
  4. Unknown with "did you mean" echo — clear rejection, not silent failure

The parser never guesses for trade-open/close intent types. If it's not
confident, it returns unknown. This is deliberate: a misfire that places a
real order is worse than asking the user to repeat themselves.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional


IntentType = Literal[
    "trade_open", "trade_close",
    "query_pnl", "query_positions", "query_balance", "signal",
    "session_status", "session_history", "signal_history", "order_history",
    "price_check", "leverage_check", "environment", "daily_summary",
    "win_rate", "best_trade", "worst_trade", "portfolio_breakdown",
    "period_summary",
    "update_sl", "update_tp", "close_partial", "close_all",
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

STOP_WORDS = {
    "the", "a", "an", "to", "for", "at", "in", "on", "of",
    "and", "or", "but", "with", "from", "is", "are", "was", "were",
    "i", "me", "my", "it", "this", "that", "please", "can", "you",
    "just", "so", "very", "really", "now",
}


# ---------------------------------------------------------------------------
# Fuzzy helpers
# ---------------------------------------------------------------------------

def _phonetic(word: str) -> str:
    """Simplified Metaphone-style hash for pronunciation-based matching.

    Handles common ASR confusions: "bit-coin" and "bitcoin" → "btkn",
    "eeth" and "eth" → "e0", "short" and "shawt" → "xrt".
    """
    w = word.lower()
    w = "".join(c for c in w if c.isalpha())
    if not w:
        return ""
    subs = [
        ("ph", "f"), ("th", "0"), ("sh", "x"), ("ch", "x"),
        ("ck", "k"), ("qu", "k"), ("wh", "w"), ("oo", "u"),
        ("ee", "i"), ("ea", "i"), ("ai", "i"),
    ]
    for a, b in subs:
        w = w.replace(a, b)
    out = [w[0]]
    for c in w[1:]:
        if c not in "aeiou0":
            out.append(c)
    result = ""
    for c in out:
        if not result or result[-1] != c:
            result += c
    return result


def _fuzzy_word(word: str, candidates: list[str], cutoff: float = 0.78) -> Optional[str]:
    """Return closest match from candidates, or None if no confident match."""
    if not word or not candidates:
        return None
    if word in candidates:
        return word
    # Strip leading/trailing punctuation that ASR sometimes adds
    w = word.strip(".,!?;:")
    if not w:
        return None
    # difflib word match
    matches = difflib.get_close_matches(w, candidates, n=1, cutoff=cutoff)
    if matches:
        return matches[0]
    # Phonetic fallback
    ph = _phonetic(w)
    if ph:
        for c in candidates:
            if _phonetic(c) == ph:
                return c
    return None


def _extract_token(text: str) -> Optional[str]:
    words = re.findall(r"\b[a-z]+(?:[0-9]+[a-z]+)?\b", text.lower())
    # Tier 1: exact match
    for w in words:
        if w in TOKEN_MAP:
            return TOKEN_MAP[w]
        m = re.match(r"^(\d+)([a-z]+)$", w)
        if m and m.group(2) in TOKEN_MAP:
            return f"{m.group(1)}{m.group(2).upper()}-USDT-SWAP"
    # Tier 2: fuzzy + phonetic
    candidates = list(TOKEN_MAP.keys())
    for w in words:
        if w in STOP_WORDS or len(w) < 2:
            continue
        match = _fuzzy_word(w, candidates, cutoff=0.82)
        if match:
            return TOKEN_MAP[match]
    return None


# Phrase-level triggers for fuzzy fallback at the end of parsing
PHRASE_TRIGGERS: dict[str, list[str]] = {
    "query_pnl": ["what's my pnl", "how much am i up", "how much am i down",
                  "show my pnl", "my pnl", "pnl"],
    "query_positions": ["show positions", "my positions", "open positions",
                        "what am i holding", "positions"],
    "query_balance": ["balance", "my balance", "account balance",
                      "how much money do i have"],
    "session_status": ["session status", "session info", "how's my session",
                       "is my session running", "active session"],
    "session_history": ["session history", "recent sessions", "past sessions",
                        "show me my sessions"],
    "signal_history": ["signal history", "recent signals", "past signals",
                       "show me my signals"],
    "order_history": ["order history", "recent orders", "recent trades",
                      "trade history", "show me my trades"],
    "environment": ["am i live", "am i demo", "what mode am i in",
                    "live or demo", "what environment"],
    "daily_summary": ["how did i do today", "today summary", "today's summary",
                      "today pnl", "my day", "how was my day"],
    "win_rate": ["win rate", "what's my win rate", "how often do i win",
                 "what's my success rate"],
    "best_trade": ["best trade", "biggest win", "best win", "best trade today"],
    "worst_trade": ["worst trade", "biggest loss", "worst loss"],
    "portfolio_breakdown": ["portfolio breakdown", "breakdown by market",
                            "how much per market", "spread across markets"],
    "help": ["help", "commands", "what can you do", "what can i say"],
    "stop": ["stop everything", "stop all automation", "emergency stop",
             "kill switch", "stop trading"],
    "auto_start": ["start auto trade", "start auto trading", "start session",
                   "begin auto trade"],
}


def _fuzzy_phrase_match(text: str, cutoff: float = 0.68) -> Optional[tuple[str, str]]:
    """Return (intent_type, matched_phrase) if text is close to a trigger."""
    t = text.lower().strip()
    if not t:
        return None
    best_intent: Optional[str] = None
    best_phrase: Optional[str] = None
    best_score = cutoff
    for intent, phrases in PHRASE_TRIGGERS.items():
        for phrase in phrases:
            score = difflib.SequenceMatcher(None, t, phrase).ratio()
            # Also compare phonetic forms to catch "clos my bt" → "close my btc"
            ph_t = _phonetic(t.replace(" ", ""))
            ph_p = _phonetic(phrase.replace(" ", ""))
            if ph_t and ph_p:
                ph_score = difflib.SequenceMatcher(None, ph_t, ph_p).ratio()
                score = max(score, ph_score * 0.95)  # phon match slightly discounted
            if score > best_score:
                best_score = score
                best_intent = intent
                best_phrase = phrase
    if best_intent:
        return (best_intent, best_phrase)
    return None


# ---------------------------------------------------------------------------
# Intent dataclass
# ---------------------------------------------------------------------------

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
    price_target: Optional[Decimal] = None     # for update_sl, update_tp
    close_pct: Optional[int] = None            # for close_partial (0-100)
    period: Optional[str] = None               # for period_summary
    best_guess: Optional[str] = None           # for "did you mean" echo


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

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


def _extract_leverage(text: str) -> Optional[int]:
    m = re.search(r"\b(\d+)\s*(?:x|×|times?)\b", text.lower())
    if m:
        n = int(m.group(1))
        if 1 <= n <= 100:
            return n
    m = re.search(r"\b(one hundred|hundred|fifty|twenty|ten|five)\s*(?:x|×|times?)\b",
                  text.lower())
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


def _extract_price(text: str) -> Optional[Decimal]:
    """Extract a price target for update_sl / update_tp."""
    for pat in (
        r"\b(?:to|at)\s+\$?(\d{2,}(?:[,\s]\d{3})*(?:\.\d+)?)",
        r"\b(?:to|at)\s+\$?(\d+(?:\.\d+)?)",
        r"\$(\d{2,}(?:[,\s]\d{3})*(?:\.\d+)?)",
    ):
        m = re.search(pat, text.lower())
        if m:
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                return Decimal(raw)
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


def _extract_pct(text: str) -> Optional[int]:
    """Extract a percentage for close_partial."""
    m = re.search(r"(\d+)\s*%", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(half|quarter|third|all)\b", text.lower())
    if m:
        return {"half": 50, "quarter": 25, "third": 33, "all": 100}[m.group(1)]
    return None


def _extract_period(text: str) -> Optional[str]:
    t = text.lower()
    if "today" in t or "this day" in t:
        return "today"
    if "this week" in t or "last week" in t or "past week" in t:
        return "week"
    if "this month" in t or "last month" in t or "past month" in t:
        return "month"
    return None


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_voice_command(text: str) -> VoiceIntent:
    original = text
    t = text.lower().strip()
    t = re.sub(r"[^\w\s\$%:.\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    # --- Confirm / cancel / help ----------------------------------------
    if t in {"confirm", "yes", "yeah", "yep", "go", "go ahead", "execute",
             "do it", "place it", "ok", "okay"}:
        return VoiceIntent(intent_type="confirm", raw_text=original)
    if t in {"cancel", "no", "nope", "abort", "nevermind", "never mind",
             "forget it", "stop that"}:
        return VoiceIntent(intent_type="cancel", raw_text=original)
    if t in {"help", "commands", "what can you do", "what can i say"}:
        return VoiceIntent(intent_type="help", raw_text=original)

    # --- Stop / kill (before anything else that might catch "session") ---
    if (re.search(r"\b(stop|kill)\b.*\b(everything|auto|all|trading|session)\b", t)
            or t in {"stop", "kill switch", "emergency stop"}):
        return VoiceIntent(intent_type="stop", raw_text=original)

    # --- Update SL / TP (before generic close) --------------------------
    sl_match = re.search(r"\b(move|set|tighten|adjust|update|change)\b.*"
                         r"\b(stop|sl|stop loss|stoploss)\b", t)
    if sl_match:
        inst = _extract_token(t)
        price = _extract_price(t)
        if inst and price:
            return VoiceIntent(
                intent_type="update_sl", inst_id=inst,
                price_target=price, raw_text=original,
            )

    tp_match = re.search(r"\b(move|set|adjust|update|change)\b.*"
                         r"\b(target|tp|take profit|takeprofit)\b", t)
    if tp_match:
        inst = _extract_token(t)
        price = _extract_price(t)
        if inst and price:
            return VoiceIntent(
                intent_type="update_tp", inst_id=inst,
                price_target=price, raw_text=original,
            )

    # --- Close partial / all (before generic close) ---------------------
    if re.search(r"\bclose\b.*\b(all|everything|every position)\b", t):
        return VoiceIntent(intent_type="close_all", raw_text=original)

    partial_match = re.search(r"\bclose\b.*\b(\d+%|half|quarter|third)\b", t)
    if partial_match:
        inst = _extract_token(t)
        pct = _extract_pct(t)
        if inst and pct:
            return VoiceIntent(
                intent_type="close_partial", inst_id=inst,
                close_pct=pct, raw_text=original,
            )

    # --- Period summary (before daily_summary) --------------------------
    if re.search(r"\b(this week|this month|last week|last month|past week|past month)\b", t):
        period = _extract_period(t)
        if period:
            return VoiceIntent(intent_type="period_summary",
                               period=period, raw_text=original)

    # --- Win rate / best / worst ----------------------------------------
    if re.search(r"\b(win rate|success rate|how often do i win)\b", t):
        return VoiceIntent(intent_type="win_rate", raw_text=original)
    if re.search(r"\b(best|biggest win|best win|best trade)\b", t) and "trade" in t or "win" in t:
        return VoiceIntent(intent_type="best_trade", raw_text=original)
    if re.search(r"\b(worst|biggest loss|worst loss|worst trade)\b", t):
        return VoiceIntent(intent_type="worst_trade", raw_text=original)

    # --- Portfolio breakdown --------------------------------------------
    if re.search(r"\b(breakdown|per market|by market|spread)\b", t):
        return VoiceIntent(intent_type="portfolio_breakdown", raw_text=original)

    # --- Session status / history ---------------------------------------
    if (re.search(r"\b(session|sess)\b.*\b(status|info|running|active|progress|doing)\b", t)
            or t in {"session status", "how's my session", "how is my session",
                     "active session", "am i running", "is my session running",
                     "session info", "session progress"}):
        return VoiceIntent(intent_type="session_status", raw_text=original)

    if (re.search(r"\b(session|sess)\b.*\b(history|recent|past|list|last|completed|finished)\b", t)
            or t in {"session history", "my sessions", "recent sessions",
                     "past sessions", "show sessions", "list sessions"}):
        return VoiceIntent(intent_type="session_history", raw_text=original)

    # --- Signal history -------------------------------------------------
    if (re.search(r"\b(signals?)\b.*\b(history|recent|past|list|last|completed)\b", t)
            or t in {"signal history", "my signals", "recent signals",
                     "past signals", "show signals", "list signals"}):
        return VoiceIntent(intent_type="signal_history", raw_text=original)

    # --- Order history --------------------------------------------------
    if (re.search(r"\b(orders?|trades?)\b.*\b(history|recent|past|list|last|completed|show)\b", t)
            or t in {"order history", "my orders", "recent orders", "recent trades",
                     "past trades", "trade history", "show orders", "show trades",
                     "last trades", "list trades", "list orders"}):
        return VoiceIntent(intent_type="order_history", raw_text=original)

    # --- Price check ----------------------------------------------------
    if re.search(r"\b(price|cost)\b", t) and not re.search(r"\b(long|short|buy|sell)\b", t):
        inst = _extract_token(t)
        if inst:
            return VoiceIntent(intent_type="price_check", inst_id=inst, raw_text=original)

    # --- Leverage check -------------------------------------------------
    if re.search(r"\bleverage\b", t) and re.search(r"\b(max|maximum|highest|what|how much)\b", t):
        inst = _extract_token(t)
        if inst:
            return VoiceIntent(intent_type="leverage_check", inst_id=inst, raw_text=original)

    # --- Environment ----------------------------------------------------
    if (re.search(r"\b(live|demo)\b.*\b(mode|or|account|environment)\b", t)
            or t in {"am i live", "am i on live", "am i on demo", "am i demo",
                     "what mode", "which mode", "what environment", "environment",
                     "am i live or demo", "live or demo"}):
        return VoiceIntent(intent_type="environment", raw_text=original)

    # --- Daily summary --------------------------------------------------
    if (re.search(r"\b(today|day)\b.*\b(summary|stats|pnl|performance|how did)\b", t)
            or t in {"how did i do today", "today summary", "today's summary",
                     "today pnl", "today's pnl", "day summary", "my day",
                     "today stats", "how was my day"}):
        return VoiceIntent(intent_type="daily_summary", raw_text=original)

    # --- Existing: PnL / positions / balance ----------------------------
    if re.search(r"\b(pnl|p&l|profit|how much am i (up|down)|am i (up|down)|my earnings)\b", t):
        return VoiceIntent(intent_type="query_pnl", raw_text=original)
    if re.search(r"\b(positions|open trades|what do i have|holdings|what am i holding)\b", t):
        return VoiceIntent(intent_type="query_positions", raw_text=original)
    if re.search(r"\b(balance|account value|how much (money|usdt)|how much is in)\b", t):
        return VoiceIntent(intent_type="query_balance", raw_text=original)

    # --- Existing: signal analyze ---------------------------------------
    m = re.search(r"\b(?:signal|analyze|analysis|check|look at)\s+(\w+)", t)
    if m:
        inst = _extract_token(m.group(1))
        if inst:
            return VoiceIntent(intent_type="signal", inst_id=inst, raw_text=original)

    # --- Existing: start auto trade -------------------------------------
    if re.search(r"\b(start|begin|launch)\b.*\b(auto|automat|session)\b", t) or "auto trade" in t:
        return VoiceIntent(
            intent_type="auto_start",
            leverage=_extract_leverage(t),
            strategy=_extract_strategy(t),
            rr=_extract_rr(t),
            raw_text=original,
        )

    # --- Existing: close position (single token) ------------------------
    if re.search(r"\b(close|exit|shut|dump)\b", t):
        inst = _extract_token(t)
        if inst:
            return VoiceIntent(intent_type="trade_close", inst_id=inst, raw_text=original)

    # --- Existing: open trade -------------------------------------------
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

    # --- Tier 2/3: fuzzy phrase matching --------------------------------
    fuzzy = _fuzzy_phrase_match(t, cutoff=0.68)
    if fuzzy:
        matched_intent, matched_phrase = fuzzy
        # Non-parameterized intents can be returned directly
        if matched_intent in {
            "query_pnl", "query_positions", "query_balance",
            "session_status", "session_history", "signal_history",
            "order_history", "environment", "daily_summary",
            "win_rate", "best_trade", "worst_trade", "portfolio_breakdown",
            "help", "stop",
        }:
            return VoiceIntent(
                intent_type=matched_intent,
                raw_text=original,
                confidence=0.8,
            )
        if matched_intent == "auto_start":
            return VoiceIntent(
                intent_type="auto_start",
                raw_text=original,
                confidence=0.7,
            )

    # --- Tier 4: unknown, with best guess if we have one ----------------
    best_guess = fuzzy[1] if fuzzy else None
    return VoiceIntent(
        intent_type="unknown",
        raw_text=original,
        confidence=0.3,
        best_guess=best_guess,
    )
