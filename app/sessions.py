
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EndReason(str, Enum):
    TARGET = "target"
    MAX_LOSS = "max_loss"
    SAFETY_STOP = "safety_stop"
    EXPIRED = "expired"
    USER_STOP = "user_stop"
    ERROR = "error"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SessionConfig:
    mode: Literal["scalping", "day_trading"]
    strategy: str
    duration_hours: float
    target_profit: Decimal
    max_loss: Decimal
    risk_per_trade_pct: Decimal
    safety_floor_pct: Decimal
    leverage: int
    markets: str  # 'auto' or comma-separated instIds


@dataclass
class SessionState:
    id: int
    user_id: int
    config: SessionConfig
    started_at: datetime
    ends_at: datetime
    starting_balance: Decimal
    current_balance: Decimal
    target_profit: Decimal
    max_loss: Decimal
    status: SessionStatus
    trades_count: int
    wins_count: int
    losses_count: int
    end_reason: Optional[str] = None
    ending_balance: Optional[Decimal] = None

    @property
    def pnl(self) -> Decimal:
        return self.current_balance - self.starting_balance

    @property
    def target_reached(self) -> bool:
        return self.pnl >= self.target_profit

    @property
    def max_loss_reached(self) -> bool:
        return self.pnl <= -abs(self.max_loss)

    @property
    def seconds_remaining(self) -> int:
        now = datetime.now(UTC)
        ends = self.ends_at
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)
        delta = ends - now
        return max(0, int(delta.total_seconds()))


# ---------------------------------------------------------------------------
# Backend adapter
# ---------------------------------------------------------------------------

class _Backend:
    """Minimal dual-backend adapter. `%s` placeholders in callers; translated
    to `?` automatically for SQLite."""

    def __init__(self, database_url: Optional[str], database_path: str):
        self.is_postgres = bool(database_url)
        self.database_url = database_url
        self.database_path = database_path

    @contextmanager
    def cursor(self):
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            try:
                with conn.cursor() as cur:
                    yield cur
                conn.commit()
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self.database_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                yield cur
                conn.commit()
            finally:
                conn.close()

    def run(self, cur, sql: str, params: tuple = ()) -> None:
        if not self.is_postgres:
            sql = sql.replace("%s", "?")
        cur.execute(sql, params)

    def fetchone(self, cur, sql: str, params: tuple = ()) -> Optional[dict]:
        self.run(cur, sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row) if not self.is_postgres else row

    def fetchall(self, cur, sql: str, params: tuple = ()) -> list[dict]:
        self.run(cur, sql, params)
        rows = cur.fetchall()
        return [dict(r) if not self.is_postgres else r for r in rows]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS auto_sessions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    markets TEXT NOT NULL DEFAULT 'auto',
    started_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    starting_balance NUMERIC NOT NULL,
    current_balance NUMERIC NOT NULL,
    ending_balance NUMERIC,
    target_profit NUMERIC NOT NULL,
    max_loss NUMERIC NOT NULL,
    risk_per_trade_pct NUMERIC NOT NULL,
    safety_floor_pct NUMERIC NOT NULL,
    leverage INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    end_reason TEXT,
    trades_count INTEGER NOT NULL DEFAULT 0,
    wins_count INTEGER NOT NULL DEFAULT 0,
    losses_count INTEGER NOT NULL DEFAULT 0,
    last_scan_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_sessions_active
    ON auto_sessions(user_id, status);
"""

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS auto_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    markets TEXT NOT NULL DEFAULT 'auto',
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    starting_balance TEXT NOT NULL,
    current_balance TEXT NOT NULL,
    ending_balance TEXT,
    target_profit TEXT NOT NULL,
    max_loss TEXT NOT NULL,
    risk_per_trade_pct TEXT NOT NULL,
    safety_floor_pct TEXT NOT NULL,
    leverage INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    end_reason TEXT,
    trades_count INTEGER NOT NULL DEFAULT 0,
    wins_count INTEGER NOT NULL DEFAULT 0,
    losses_count INTEGER NOT NULL DEFAULT 0,
    last_scan_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_sessions_active
    ON auto_sessions(user_id, status);
"""


class SessionManager:
    def __init__(self, database_url: Optional[str] = None,
                 database_path: str = "trading_bot.db"):
        self.backend = _Backend(database_url, database_path)

    # -- schema ---------------------------------------------------------

    def ensure_schema(self) -> None:
        ddl = _DDL_POSTGRES if self.backend.is_postgres else _DDL_SQLITE
        with self.backend.cursor() as cur:
            for statement in [s.strip() for s in ddl.split(";") if s.strip()]:
                cur.execute(statement)
        log.info("auto_sessions schema ensured (%s)",
                 "postgres" if self.backend.is_postgres else "sqlite")

    # -- writes ---------------------------------------------------------

    def create_session(self, user_id: int, config: SessionConfig,
                       starting_balance: Decimal) -> int:
        ends_at = datetime.now(UTC) + timedelta(hours=config.duration_hours)
        with self.backend.cursor() as cur:
            self.backend.run(cur, """
                INSERT INTO auto_sessions
                  (user_id, mode, strategy, markets,
                   started_at, ends_at,
                   starting_balance, current_balance,
                   target_profit, max_loss, risk_per_trade_pct,
                   safety_floor_pct, leverage, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            """, (
                user_id, config.mode, config.strategy, config.markets,
                datetime.now(UTC), ends_at,
                str(starting_balance), str(starting_balance),
                str(config.target_profit), str(config.max_loss),
                str(config.risk_per_trade_pct), str(config.safety_floor_pct),
                config.leverage,
            ))
            if self.backend.is_postgres:
                cur.execute("SELECT currval(pg_get_serial_sequence('auto_sessions','id')) AS id")
                row = cur.fetchone()
                return int(row["id"])
            return int(cur.lastrowid)

    def update_balance(self, session_id: int, balance: Decimal) -> None:
        with self.backend.cursor() as cur:
            self.backend.run(cur,
                "UPDATE auto_sessions SET current_balance = %s WHERE id = %s",
                (str(balance), session_id))

    def record_trade_result(self, session_id: int, is_win: bool,
                            new_balance: Decimal) -> None:
        col = "wins_count" if is_win else "losses_count"
        with self.backend.cursor() as cur:
            self.backend.run(cur, f"""
                UPDATE auto_sessions
                SET trades_count = trades_count + 1,
                    {col} = {col} + 1,
                    current_balance = %s,
                    last_scan_at = %s
                WHERE id = %s
            """, (str(new_balance), datetime.now(UTC), session_id))

    def touch_scan(self, session_id: int) -> None:
        with self.backend.cursor() as cur:
            self.backend.run(cur,
                "UPDATE auto_sessions SET last_scan_at = %s WHERE id = %s",
                (datetime.now(UTC), session_id))

    def end_session(self, session_id: int, reason: EndReason,
                    ending_balance: Decimal) -> None:
        with self.backend.cursor() as cur:
            self.backend.run(cur, """
                UPDATE auto_sessions
                SET status = 'complete',
                    end_reason = %s,
                    ending_balance = %s,
                    current_balance = %s
                WHERE id = %s
            """, (reason.value, str(ending_balance), str(ending_balance), session_id))

    def record_error(self, session_id: int, error: str) -> None:
        with self.backend.cursor() as cur:
            self.backend.run(cur,
                "UPDATE auto_sessions SET last_error = %s WHERE id = %s",
                (error[:500], session_id))

    # -- reads ----------------------------------------------------------

    def get_active(self, user_id: int) -> Optional[SessionState]:
        with self.backend.cursor() as cur:
            row = self.backend.fetchone(cur, """
                SELECT * FROM auto_sessions
                WHERE user_id = %s AND status = 'active'
                ORDER BY id DESC LIMIT 1
            """, (user_id,))
        return self._to_state(row) if row else None

    def get_by_id(self, session_id: int) -> Optional[SessionState]:
        with self.backend.cursor() as cur:
            row = self.backend.fetchone(cur,
                "SELECT * FROM auto_sessions WHERE id = %s", (session_id,))
        return self._to_state(row) if row else None

    def list_history(self, user_id: int, limit: int = 20) -> list[SessionState]:
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur, """
                SELECT * FROM auto_sessions
                WHERE user_id = %s AND status = 'complete'
                ORDER BY id DESC LIMIT %s
            """, (user_id, limit))
        return [self._to_state(r) for r in rows]
     
    def list_all_active(self) -> list[SessionState]:
        """Every active session across every user. Used by the worker."""
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur, """
                SELECT * FROM auto_sessions WHERE status = 'active'
            """, ())
        return [self._to_state(r) for r in rows]

    # -- translation ----------------------------------------------------

    @staticmethod
    def _parse_dt(value) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _dec(value) -> Decimal:
        if value is None:
            return Decimal("0")
        return Decimal(str(value))

    def _to_state(self, row: dict) -> SessionState:
        cfg = SessionConfig(
            mode=row["mode"],
            strategy=row["strategy"],
            duration_hours=0,
            target_profit=self._dec(row["target_profit"]),
            max_loss=self._dec(row["max_loss"]),
            risk_per_trade_pct=self._dec(row["risk_per_trade_pct"]),
            safety_floor_pct=self._dec(row["safety_floor_pct"]),
            leverage=int(row["leverage"]),
            markets=row["markets"],
        )
        return SessionState(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            config=cfg,
            started_at=self._parse_dt(row["started_at"]),
            ends_at=self._parse_dt(row["ends_at"]),
            starting_balance=self._dec(row["starting_balance"]),
            current_balance=self._dec(row["current_balance"]),
            target_profit=self._dec(row["target_profit"]),
            max_loss=self._dec(row["max_loss"]),
            status=SessionStatus(row["status"]),
            trades_count=int(row["trades_count"] or 0),
            wins_count=int(row["wins_count"] or 0),
            losses_count=int(row["losses_count"] or 0),
            end_reason=row["end_reason"],
            ending_balance=self._dec(row["ending_balance"]) if row["ending_balance"] is not None else None,
        )


# ---------------------------------------------------------------------------
# Risk guard — decides when to skip a trade and when to end the session
# ---------------------------------------------------------------------------

@dataclass
class RiskCheck:
    allowed: bool
    reason: str
    risk_amount: Decimal = Decimal("0")


def compute_risk_amount(balance: Decimal, risk_pct: Decimal) -> Decimal:
    return (balance * risk_pct / Decimal("100")).quantize(Decimal("0.01"))


def check_can_open(session: SessionState, balance: Decimal,
                   open_positions_count: int, max_concurrent: int = 3,
                   concentration_cap_pct: Decimal = Decimal("20")) -> RiskCheck:
    """Whether the bot may open a NEW position right now.

    Returns allowed=False to skip an opportunity. Does NOT end the session.
    """
    if balance <= 0:
        return RiskCheck(False, "balance is zero", Decimal("0"))

    risk = compute_risk_amount(balance, session.config.risk_per_trade_pct)

    cap = balance * concentration_cap_pct / Decimal("100")
    if risk > cap:
        return RiskCheck(
            False,
            f"risk {risk} would exceed {concentration_cap_pct}% of balance {balance:.2f}",
            Decimal("0"),
        )

    if open_positions_count >= max_concurrent:
        return RiskCheck(
            False,
            f"already {open_positions_count} open positions (max {max_concurrent})",
            Decimal("0"),
        )

    return RiskCheck(True, "ok", risk)


def should_end_session(session: SessionState, balance: Decimal) -> Optional[EndReason]:
    """The 4 stop conditions that end the entire session.

    Returns None to keep running, or an EndReason to close now.
    """
    if balance - session.starting_balance >= session.target_profit:
        return EndReason.TARGET

    if session.starting_balance - balance >= abs(session.max_loss):
        return EndReason.MAX_LOSS

    floor = session.starting_balance * session.config.safety_floor_pct / Decimal("100")
    if balance <= floor:
        return EndReason.SAFETY_STOP

    if session.seconds_remaining <= 0:
        return EndReason.EXPIRED

    return None
