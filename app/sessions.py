

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


@dataclass
class SessionConfig:
    mode: Literal["day_trading"]
    strategy: str
    duration_hours: float
    target_profit: Decimal
    max_loss: Decimal
    risk_per_trade_pct: Decimal
    safety_floor_pct: Decimal
    leverage: int
    markets: str


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
    loss_streak: int = 0
    last_loss_at: Optional[datetime] = None
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
        return max(0, int((ends - now).total_seconds()))

    @property
    def hours_since_last_loss(self) -> float:
        if not self.last_loss_at:
            return 999.0
        ts = self.last_loss_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (datetime.now(UTC) - ts).total_seconds() / 3600.0


@dataclass
class TrackedPosition:
    id: int
    session_id: int
    inst_id: str
    pos_side: str
    entry_price: Decimal
    contracts: Decimal
    opened_at: datetime
    client_order_id: str
    order_id: Optional[str] = None
    initial_sl: Optional[Decimal] = None
    current_sl: Optional[Decimal] = None
    algo_clordid: Optional[str] = None
    last_action_candle_ts: Optional[int] = None
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[Decimal] = None


class _Backend:
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


_DDL_POSTGRES = [
    """
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
        loss_streak INTEGER NOT NULL DEFAULT 0,
        last_loss_at TIMESTAMPTZ,
        last_scan_at TIMESTAMPTZ,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_positions (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL,
        inst_id TEXT NOT NULL,
        pos_side TEXT NOT NULL,
        entry_price NUMERIC,
        contracts NUMERIC,
        opened_at TIMESTAMPTZ NOT NULL,
        closed_at TIMESTAMPTZ,
        realized_pnl NUMERIC,
        client_order_id TEXT UNIQUE,
        order_id TEXT,
        initial_sl NUMERIC,
        current_sl NUMERIC,
        algo_clordid TEXT,
        last_action_candle_ts BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auto_sessions_active ON auto_sessions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_session_positions_open ON session_positions(session_id, closed_at)",
]

_DDL_SQLITE = [
    """
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
        loss_streak INTEGER NOT NULL DEFAULT 0,
        last_loss_at TEXT,
        last_scan_at TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        inst_id TEXT NOT NULL,
        pos_side TEXT NOT NULL,
        entry_price TEXT,
        contracts TEXT,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        realized_pnl TEXT,
        client_order_id TEXT UNIQUE,
        order_id TEXT,
        initial_sl TEXT,
        current_sl TEXT,
        algo_clordid TEXT,
        last_action_candle_ts INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auto_sessions_active ON auto_sessions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_session_positions_open ON session_positions(session_id, closed_at)",
]

# Each migration is attempted independently so a failure never blocks the rest.
_MIGRATIONS = [
    "ALTER TABLE auto_sessions ADD COLUMN loss_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE auto_sessions ADD COLUMN last_loss_at TEXT",
    "ALTER TABLE session_positions ADD COLUMN order_id TEXT",
    "ALTER TABLE session_positions ADD COLUMN initial_sl TEXT",
    "ALTER TABLE session_positions ADD COLUMN current_sl TEXT",
    "ALTER TABLE session_positions ADD COLUMN algo_clordid TEXT",
    "ALTER TABLE session_positions ADD COLUMN last_action_candle_ts INTEGER",
]


class SessionManager:
    def __init__(self, database_url: Optional[str] = None,
                 database_path: str = "trading_bot.db"):
        self.backend = _Backend(database_url, database_path)

    def ensure_schema(self) -> None:
        ddl = _DDL_POSTGRES if self.backend.is_postgres else _DDL_SQLITE
        failures = 0
        for i, stmt in enumerate(ddl):
            try:
                with self.backend.cursor() as cur:
                    cur.execute(stmt)
            except Exception as exc:
                failures += 1
                log.warning("ensure_schema DDL %d failed: %s | %s",
                            i, exc, stmt.strip()[:80])

        for stmt in _MIGRATIONS:
            try:
                with self.backend.cursor() as cur:
                    cur.execute(stmt)
            except Exception:
                # Column already exists — normal on repeat startups
                pass

        log.info("session schema ensured (%s) - %d DDL, %d failures",
                 "postgres" if self.backend.is_postgres else "sqlite",
                 len(ddl), failures)

    # --------------------------------------------------------------
    # Session lifecycle
    # --------------------------------------------------------------

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
                   safety_floor_pct, leverage, status, loss_streak)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 0)
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

    def record_trade_result(self, session_id: int, is_win: Optional[bool],
                            new_balance: Decimal) -> None:
        """Update counters. On win: reset loss_streak. On loss: increment streak."""
        if is_win is True:
            with self.backend.cursor() as cur:
                self.backend.run(cur, """
                    UPDATE auto_sessions
                    SET trades_count = trades_count + 1,
                        wins_count = wins_count + 1,
                        loss_streak = 0,
                        last_loss_at = NULL,
                        current_balance = %s,
                        last_scan_at = %s
                    WHERE id = %s
                """, (str(new_balance), datetime.now(UTC), session_id))
        elif is_win is False:
            with self.backend.cursor() as cur:
                self.backend.run(cur, """
                    UPDATE auto_sessions
                    SET trades_count = trades_count + 1,
                        losses_count = losses_count + 1,
                        loss_streak = loss_streak + 1,
                        last_loss_at = %s,
                        current_balance = %s,
                        last_scan_at = %s
                    WHERE id = %s
                """, (datetime.now(UTC), str(new_balance), datetime.now(UTC), session_id))
        else:
            with self.backend.cursor() as cur:
                self.backend.run(cur, """
                    UPDATE auto_sessions
                    SET trades_count = trades_count + 1,
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

    # --------------------------------------------------------------
    # Position tracking
    # --------------------------------------------------------------

    def track_position(self, session_id: int, inst_id: str, pos_side: str,
                       entry_price: Decimal, contracts: Decimal,
                       client_order_id: str, order_id: Optional[str] = None,
                       initial_sl: Optional[Decimal] = None,
                       algo_clordid: Optional[str] = None) -> None:
        with self.backend.cursor() as cur:
            self.backend.run(cur, """
                INSERT INTO session_positions
                  (session_id, inst_id, pos_side, entry_price, contracts,
                   opened_at, client_order_id, order_id,
                   initial_sl, current_sl, algo_clordid)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session_id, inst_id, pos_side,
                str(entry_price), str(contracts),
                datetime.now(UTC), client_order_id, order_id,
                str(initial_sl) if initial_sl is not None else None,
                str(initial_sl) if initial_sl is not None else None,
                algo_clordid,
            ))

    def list_open_positions(self, session_id: int) -> list[TrackedPosition]:
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur, """
                SELECT * FROM session_positions
                WHERE session_id = %s AND closed_at IS NULL
            """, (session_id,))
        return [self._to_position(r) for r in rows]

    def update_position_sl(self, client_order_id: str,
                           new_sl: Decimal,
                           candle_ts: int) -> None:
        """Record the new SL and the candle we acted on."""
        with self.backend.cursor() as cur:
            self.backend.run(cur, """
                UPDATE session_positions
                SET current_sl = %s, last_action_candle_ts = %s
                WHERE client_order_id = %s AND closed_at IS NULL
            """, (str(new_sl), int(candle_ts), client_order_id))

    def mark_position_closed(self, client_order_id: str,
                             realized_pnl: Optional[Decimal] = None) -> bool:
        with self.backend.cursor() as cur:
            self.backend.run(cur, """
                UPDATE session_positions
                SET closed_at = %s, realized_pnl = %s
                WHERE client_order_id = %s AND closed_at IS NULL
            """, (
                datetime.now(UTC),
                str(realized_pnl) if realized_pnl is not None else None,
                client_order_id,
            ))
            return cur.rowcount > 0

    def session_realized_pnl(self, session_id: int) -> Decimal:
        with self.backend.cursor() as cur:
            row = self.backend.fetchone(cur, """
                SELECT COALESCE(SUM(CAST(realized_pnl AS NUMERIC)), 0) AS total
                FROM session_positions
                WHERE session_id = %s
            """, (session_id,))
        if not row:
            return Decimal("0")
        return Decimal(str(row["total"] or 0))

    def session_open_exposure(self, session_id: int) -> Decimal:
        """Sum of initial margin across all open positions for this session."""
        try:
            with self.backend.cursor() as cur:
                rows = self.backend.fetchall(cur, """
                    SELECT entry_price, contracts FROM session_positions
                    WHERE session_id = %s AND closed_at IS NULL
                """, (session_id,))
            total = Decimal("0")
            for r in rows:
                entry = Decimal(str(r["entry_price"] or 0))
                contracts = Decimal(str(r["contracts"] or 0))
                total += entry * contracts
            return total
        except Exception:
            return Decimal("0")

    # --------------------------------------------------------------
    # Startup repair helpers
    # --------------------------------------------------------------

    def list_sessions_with_open_positions(self) -> list[tuple[int, int]]:
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur, """
                SELECT DISTINCT sp.session_id AS session_id, s.user_id AS user_id
                FROM session_positions sp
                JOIN auto_sessions s ON s.id = sp.session_id
                WHERE sp.closed_at IS NULL
            """, ())
        return [(int(r["session_id"]), int(r["user_id"])) for r in rows]

    def recount_session_from_positions(self, session_id: int) -> tuple[int, int, int]:
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur, """
                SELECT realized_pnl FROM session_positions
                WHERE session_id = %s AND closed_at IS NOT NULL
            """, (session_id,))
            total = len(rows)
            wins = 0
            losses = 0
            for r in rows:
                raw = r["realized_pnl"]
                if raw is None:
                    continue
                v = Decimal(str(raw))
                if v > 0:
                    wins += 1
                else:
                    losses += 1
            self.backend.run(cur, """
                UPDATE auto_sessions
                SET trades_count = %s, wins_count = %s, losses_count = %s
                WHERE id = %s
            """, (total, wins, losses, session_id))
        return total, wins, losses

    # --------------------------------------------------------------
    # Reads
    # --------------------------------------------------------------

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
        with self.backend.cursor() as cur:
            rows = self.backend.fetchall(cur,
                "SELECT * FROM auto_sessions WHERE status = 'active'", ())
        return [self._to_state(r) for r in rows]

    # --------------------------------------------------------------
    # Translation
    # --------------------------------------------------------------

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _dec(value) -> Decimal:
        if value is None:
            return Decimal("0")
        return Decimal(str(value))

    @staticmethod
    def _dec_opt(value) -> Optional[Decimal]:
        if value is None:
            return None
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
            loss_streak=int(row.get("loss_streak") or 0),
            last_loss_at=self._parse_dt(row.get("last_loss_at")),
            end_reason=row["end_reason"],
            ending_balance=self._dec(row["ending_balance"]) if row["ending_balance"] is not None else None,
        )

    def _to_position(self, row: dict) -> TrackedPosition:
        return TrackedPosition(
            id=int(row["id"]),
            session_id=int(row["session_id"]),
            inst_id=row["inst_id"],
            pos_side=row["pos_side"],
            entry_price=self._dec(row["entry_price"]),
            contracts=self._dec(row["contracts"]),
            opened_at=self._parse_dt(row["opened_at"]),
            client_order_id=row["client_order_id"],
            order_id=row.get("order_id") if isinstance(row, dict) else None,
            initial_sl=self._dec_opt(row.get("initial_sl")),
            current_sl=self._dec_opt(row.get("current_sl")),
            algo_clordid=row.get("algo_clordid"),
            last_action_candle_ts=(int(row["last_action_candle_ts"])
                                   if row.get("last_action_candle_ts") is not None
                                   else None),
            closed_at=self._parse_dt(row["closed_at"]) if row["closed_at"] else None,
            realized_pnl=self._dec_opt(row["realized_pnl"]),
        )


# ---------------------------------------------------------------------------
# Legacy risk guard — kept for backward compatibility.
# The worker uses app.risk.evaluate_entry instead.
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
    if balance <= 0:
        return RiskCheck(False, "balance is zero", Decimal("0"))
    risk = compute_risk_amount(balance, session.config.risk_per_trade_pct)
    cap = balance * concentration_cap_pct / Decimal("100")
    if risk > cap:
        return RiskCheck(False,
                         f"risk {risk} would exceed {concentration_cap_pct}% of balance {balance:.2f}",
                         Decimal("0"))
    if open_positions_count >= max_concurrent:
        return RiskCheck(False,
                         f"already {open_positions_count} open positions (max {max_concurrent})",
                         Decimal("0"))
    return RiskCheck(True, "ok", risk)


def should_end_session(session: SessionState, balance: Decimal) -> Optional[EndReason]:
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
