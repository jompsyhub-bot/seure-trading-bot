from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .models import OrderIntent, OrderResult


class SQLiteDatabase:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
              user_id INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
              requested_notional TEXT NOT NULL, order_id TEXT, status TEXT NOT NULL,
              fill_price TEXT, quantity TEXT, realized_pnl TEXT NOT NULL DEFAULT '0',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);
            CREATE TABLE IF NOT EXISTS users (
              telegram_user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bybit_connections (
              telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
              encrypted_api_key BLOB NOT NULL, encrypted_api_secret BLOB NOT NULL,
              encrypted_passphrase BLOB, provider TEXT NOT NULL DEFAULT 'bybit',
              is_testnet INTEGER NOT NULL, environment TEXT NOT NULL DEFAULT 'testnet', permissions_json TEXT NOT NULL,
              connected_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_automation_settings (
              telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
              paper_automation_enabled INTEGER NOT NULL DEFAULT 0,
              max_order_notional TEXT NOT NULL DEFAULT '10',
              max_daily_loss TEXT NOT NULL DEFAULT '20', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_profiles (
              telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
              enabled INTEGER NOT NULL DEFAULT 0,
              strategy TEXT NOT NULL DEFAULT 'rsi_reversion_demo',
              symbol TEXT NOT NULL DEFAULT 'BTCUSDT',
              max_order_notional TEXT NOT NULL DEFAULT '10',
              max_trades_per_day INTEGER NOT NULL DEFAULT 1,
              stop_loss_pct TEXT NOT NULL DEFAULT '2',
              take_profit_pct TEXT NOT NULL DEFAULT '4',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              inst_id TEXT NOT NULL,
              token_input TEXT NOT NULL,
              strategy TEXT NOT NULL,
              direction TEXT NOT NULL,
              overall_confidence INTEGER NOT NULL DEFAULT 0,
              analysis_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_user_created ON signals(user_id, created_at);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bybit_connections)")}
            if "environment" not in columns:
                conn.execute("ALTER TABLE bybit_connections ADD COLUMN environment TEXT NOT NULL DEFAULT 'testnet'")
            if "provider" not in columns:
                conn.execute("ALTER TABLE bybit_connections ADD COLUMN provider TEXT NOT NULL DEFAULT 'bybit'")
            if "encrypted_passphrase" not in columns:
                conn.execute("ALTER TABLE bybit_connections ADD COLUMN encrypted_passphrase BLOB")

    def upsert_user(self, telegram_user_id: int, username: str | None, first_name: str | None) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO users (telegram_user_id,username,first_name,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,updated_at=excluded.updated_at",
                (telegram_user_id, username, first_name, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_automation_settings (telegram_user_id,updated_at) VALUES (?,?)",
                (telegram_user_id, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO automation_profiles (telegram_user_id,updated_at) VALUES (?,?)",
                (telegram_user_id, now),
            )

    def save_bybit_connection(self, telegram_user_id: int, api_key: bytes, api_secret: bytes, environment: str, permissions: dict) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO bybit_connections (telegram_user_id,encrypted_api_key,encrypted_api_secret,is_testnet,environment,permissions_json,connected_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET encrypted_api_key=excluded.encrypted_api_key,encrypted_api_secret=excluded.encrypted_api_secret,is_testnet=excluded.is_testnet,environment=excluded.environment,permissions_json=excluded.permissions_json,updated_at=excluded.updated_at",
                (telegram_user_id, api_key, api_secret, int(environment == "testnet"), environment, json.dumps(permissions), now, now),
            )

    def save_okx_connection(
        self,
        telegram_user_id: int,
        api_key: bytes,
        api_secret: bytes,
        passphrase: bytes,
        account: dict,
        *,
        environment: str = "demo",
    ) -> None:
        """Store an OKX connection.

        `environment` must be "demo" or "live". Existing call sites that
        don't pass it default to "demo", preserving old behavior.
        """
        if environment not in {"demo", "live"}:
            raise ValueError(f"Unsupported OKX environment: {environment!r}")
        now = datetime.now(UTC).isoformat()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO bybit_connections (telegram_user_id,encrypted_api_key,encrypted_api_secret,encrypted_passphrase,provider,is_testnet,environment,permissions_json,connected_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET encrypted_api_key=excluded.encrypted_api_key,encrypted_api_secret=excluded.encrypted_api_secret,encrypted_passphrase=excluded.encrypted_passphrase,provider=excluded.provider,is_testnet=excluded.is_testnet,environment=excluded.environment,permissions_json=excluded.permissions_json,updated_at=excluded.updated_at",
                (
                    telegram_user_id, api_key, api_secret, passphrase, "okx",
                    int(environment == "demo"),
                    environment,
                    json.dumps(account), now, now,
                ),
            )

    def dashboard(self, telegram_user_id: int) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT u.telegram_user_id,u.first_name,c.provider,c.environment,c.connected_at,s.paper_automation_enabled,s.max_order_notional,s.max_daily_loss FROM users u LEFT JOIN bybit_connections c ON c.telegram_user_id=u.telegram_user_id LEFT JOIN user_automation_settings s ON s.telegram_user_id=u.telegram_user_id WHERE u.telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
        if not row:
            return {"connected": False, "paper_automation_enabled": False}
        return {
            "name": row["first_name"],
            "connected": row["connected_at"] is not None,
            "provider": row["provider"],
            "environment": row["environment"],
            "paper_automation_enabled": bool(row["paper_automation_enabled"]),
            "max_order_notional": row["max_order_notional"],
            "max_daily_loss": row["max_daily_loss"],
        }

    def connection_environment(self, user_id: int) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT environment FROM bybit_connections WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        return row["environment"] if row else None

    def connection_provider(self, user_id: int) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT provider FROM bybit_connections WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        return row["provider"] if row else None

    def exchange_credentials(self, user_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT provider,environment,encrypted_api_key,encrypted_api_secret,encrypted_passphrase FROM bybit_connections WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_paper_settings(self, telegram_user_id: int, enabled: bool, max_order_notional: Decimal, max_daily_loss: Decimal) -> None:
        if max_order_notional <= 0 or max_daily_loss <= 0:
            raise ValueError("Limits must be positive")
        with self.connection() as conn:
            conn.execute(
                "UPDATE user_automation_settings SET paper_automation_enabled=?,max_order_notional=?,max_daily_loss=?,updated_at=? WHERE telegram_user_id=?",
                (int(enabled), str(max_order_notional), str(max_daily_loss), datetime.now(UTC).isoformat(), telegram_user_id),
            )

    def save_automation_profile(self, user_id: int, enabled: bool, strategy: str, symbol: str, max_order: Decimal, max_trades: int, stop_loss: Decimal, take_profit: Decimal) -> None:
        if strategy != "rsi_reversion_demo" or symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("Unsupported demo strategy or symbol")
        if not (Decimal("1") <= max_order <= Decimal("50") and 1 <= max_trades <= 3 and Decimal("0.5") <= stop_loss <= Decimal("5") and Decimal("1") <= take_profit <= Decimal("10")):
            raise ValueError("One or more limits are outside the demo safety range")
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=?,strategy=?,symbol=?,max_order_notional=?,max_trades_per_day=?,stop_loss_pct=?,take_profit_pct=?,updated_at=? WHERE telegram_user_id=?",
                (int(enabled), strategy, symbol, str(max_order), max_trades, str(stop_loss), str(take_profit), datetime.now(UTC).isoformat(), user_id),
            )

    def automation_profile(self, user_id: int) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT enabled,strategy,symbol,max_order_notional,max_trades_per_day,stop_loss_pct,take_profit_pct FROM automation_profiles WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else {}

    def set_kill_switch(self, user_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=0,updated_at=? WHERE telegram_user_id=?",
                (datetime.now(UTC).isoformat(), user_id),
            )

    def disconnect_bybit(self, user_id: int) -> None:
        """Remove encrypted credentials and stop future scans for this user."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=0,updated_at=? WHERE telegram_user_id=?",
                (datetime.now(UTC).isoformat(), user_id),
            )
            conn.execute("DELETE FROM bybit_connections WHERE telegram_user_id=?", (user_id,))

    def history(self, user_id: int, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT order_id,symbol,side,status,fill_price,quantity,realized_pnl,created_at,updated_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def orders_for_sync(self, user_id: int, limit: int = 30) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT idempotency_key,order_id,symbol FROM orders WHERE user_id=? AND order_id IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_exchange_order(self, key: str, status: str, price: Decimal, quantity: Decimal, realized_pnl: Decimal) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET status=?,fill_price=?,quantity=?,realized_pnl=?,updated_at=? WHERE idempotency_key=?",
                (status, str(price), str(quantity), str(realized_pnl), datetime.now(UTC).isoformat(), key),
            )

    def demo_automation_users(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.*,c.encrypted_api_key,c.encrypted_api_secret FROM automation_profiles p JOIN bybit_connections c ON c.telegram_user_id=p.telegram_user_id WHERE p.enabled=1 AND c.provider='bybit' AND c.environment='demo'"
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_order(self, key: str) -> OrderResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()
        if not row or not row["order_id"]:
            return None
        return OrderResult(row["order_id"], row["status"], Decimal(row["fill_price"]), Decimal(row["quantity"]))

    def reserve(self, intent: OrderIntent) -> bool:
        now = datetime.now(UTC).isoformat()
        try:
            with self.connection() as conn:
                conn.execute(
                    "INSERT INTO orders (idempotency_key,user_id,symbol,side,requested_notional,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (intent.idempotency_key, intent.user_id, intent.symbol, intent.side, str(intent.notional_usdt), "PENDING", now, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finalize(self, key: str, result: OrderResult) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET order_id=?,status=?,fill_price=?,quantity=?,updated_at=? WHERE idempotency_key=?",
                (result.order_id, result.status, str(result.price), str(result.quantity), datetime.now(UTC).isoformat(), key),
            )

    def cancel_reservation(self, key: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET status='REJECTED', updated_at=? WHERE idempotency_key=? AND status='PENDING'",
                (datetime.now(UTC).isoformat(), key),
            )

    def today_stats(self, user_id: int) -> tuple[int, Decimal]:
        day = datetime.now(UTC).date().isoformat()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS trades, COALESCE(SUM(CAST(realized_pnl AS REAL)),0) AS pnl FROM orders WHERE user_id=? AND created_at LIKE ? AND status IN ('FILLED','NEW')",
                (user_id, f"{day}%"),
            ).fetchone()
        return int(row["trades"]), Decimal(str(row["pnl"]))

    # --- Signals --------------------------------------------------------

    def insert_signal(
        self,
        user_id: int,
        inst_id: str,
        token_input: str,
        strategy: str,
        direction: str,
        overall_confidence: int,
        analysis: dict,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO signals (user_id,inst_id,token_input,strategy,direction,"
                "overall_confidence,analysis_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    user_id, inst_id, token_input, strategy, direction,
                    int(overall_confidence), json.dumps(analysis),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def signals_history(self, user_id: int, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT inst_id,token_input,strategy,direction,overall_confidence,"
                "analysis_json,created_at FROM signals "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            entry = {k: row[k] for k in row.keys() if k != "analysis_json"}
            try:
                entry["analysis"] = json.loads(row["analysis_json"])
            except (json.JSONDecodeError, TypeError):
                entry["analysis"] = None
            out.append(entry)
        return out


class PostgreSQLDatabase:
    """Supabase/Postgres implementation. psycopg is imported only when needed."""

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("DATABASE_URL requires psycopg. Run pip install -r requirements.txt") from exc
        self.url = url
        self.psycopg = psycopg
        self.dict_row = dict_row

    def connect(self):
        return self.psycopg.connect(self.url, row_factory=self.dict_row, sslmode="require")

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
          telegram_user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
          id BIGSERIAL PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
          user_id BIGINT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
          requested_notional TEXT NOT NULL, order_id TEXT, status TEXT NOT NULL,
          fill_price TEXT, quantity TEXT, realized_pnl TEXT NOT NULL DEFAULT '0',
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);
        CREATE TABLE IF NOT EXISTS bybit_connections (
          telegram_user_id BIGINT PRIMARY KEY REFERENCES users(telegram_user_id),
          encrypted_api_key BYTEA NOT NULL, encrypted_api_secret BYTEA NOT NULL,
          encrypted_passphrase BYTEA, provider TEXT NOT NULL DEFAULT 'bybit',
          is_testnet BOOLEAN NOT NULL, environment TEXT NOT NULL DEFAULT 'testnet',
          permissions_json TEXT NOT NULL, connected_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_automation_settings (
          telegram_user_id BIGINT PRIMARY KEY REFERENCES users(telegram_user_id),
          paper_automation_enabled BOOLEAN NOT NULL DEFAULT FALSE,
          max_order_notional TEXT NOT NULL DEFAULT '10', max_daily_loss TEXT NOT NULL DEFAULT '20',
          updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS automation_profiles (
          telegram_user_id BIGINT PRIMARY KEY REFERENCES users(telegram_user_id),
          enabled BOOLEAN NOT NULL DEFAULT FALSE, strategy TEXT NOT NULL DEFAULT 'rsi_reversion_demo',
          symbol TEXT NOT NULL DEFAULT 'BTCUSDT', max_order_notional TEXT NOT NULL DEFAULT '10',
          max_trades_per_day INTEGER NOT NULL DEFAULT 1, stop_loss_pct TEXT NOT NULL DEFAULT '2',
          take_profit_pct TEXT NOT NULL DEFAULT '4', updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals (
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          inst_id TEXT NOT NULL,
          token_input TEXT NOT NULL,
          strategy TEXT NOT NULL,
          direction TEXT NOT NULL,
          overall_confidence INTEGER NOT NULL DEFAULT 0,
          analysis_json TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_user_created ON signals(user_id, created_at);
        """
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(68422147)")
            conn.execute(schema)
            conn.execute("ALTER TABLE bybit_connections ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'bybit'")
            conn.execute("ALTER TABLE bybit_connections ADD COLUMN IF NOT EXISTS encrypted_passphrase BYTEA")

    def upsert_user(self, telegram_user_id: int, username: str | None, first_name: str | None) -> None:
        now = datetime.now(UTC)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO users (telegram_user_id,username,first_name,created_at,updated_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(telegram_user_id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,updated_at=EXCLUDED.updated_at",
                (telegram_user_id, username, first_name, now, now),
            )
            conn.execute(
                "INSERT INTO user_automation_settings (telegram_user_id,updated_at) VALUES (%s,%s) ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, now),
            )
            conn.execute(
                "INSERT INTO automation_profiles (telegram_user_id,updated_at) VALUES (%s,%s) ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, now),
            )

    def save_bybit_connection(self, telegram_user_id: int, api_key: bytes, api_secret: bytes, environment: str, permissions: dict) -> None:
        now = datetime.now(UTC)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO bybit_connections (telegram_user_id,encrypted_api_key,encrypted_api_secret,is_testnet,environment,permissions_json,connected_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(telegram_user_id) DO UPDATE SET encrypted_api_key=EXCLUDED.encrypted_api_key,encrypted_api_secret=EXCLUDED.encrypted_api_secret,is_testnet=EXCLUDED.is_testnet,environment=EXCLUDED.environment,permissions_json=EXCLUDED.permissions_json,updated_at=EXCLUDED.updated_at",
                (telegram_user_id, api_key, api_secret, environment == "testnet", environment, json.dumps(permissions), now, now),
            )

    def save_okx_connection(
        self,
        telegram_user_id: int,
        api_key: bytes,
        api_secret: bytes,
        passphrase: bytes,
        account: dict,
        *,
        environment: str = "demo",
    ) -> None:
        """Store an OKX connection.

        `environment` must be "demo" or "live". Existing call sites that
        don't pass it default to "demo", preserving old behavior.
        """
        if environment not in {"demo", "live"}:
            raise ValueError(f"Unsupported OKX environment: {environment!r}")
        now = datetime.now(UTC)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO bybit_connections (telegram_user_id,encrypted_api_key,encrypted_api_secret,encrypted_passphrase,provider,is_testnet,environment,permissions_json,connected_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(telegram_user_id) DO UPDATE SET encrypted_api_key=EXCLUDED.encrypted_api_key,encrypted_api_secret=EXCLUDED.encrypted_api_secret,encrypted_passphrase=EXCLUDED.encrypted_passphrase,provider=EXCLUDED.provider,is_testnet=EXCLUDED.is_testnet,environment=EXCLUDED.environment,permissions_json=EXCLUDED.permissions_json,updated_at=EXCLUDED.updated_at",
                (
                    telegram_user_id, api_key, api_secret, passphrase, "okx",
                    environment == "demo",
                    environment,
                    json.dumps(account), now, now,
                ),
            )

    def dashboard(self, telegram_user_id: int) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT u.telegram_user_id,u.first_name,c.provider,c.environment,c.connected_at,s.paper_automation_enabled,s.max_order_notional,s.max_daily_loss FROM users u LEFT JOIN bybit_connections c ON c.telegram_user_id=u.telegram_user_id LEFT JOIN user_automation_settings s ON s.telegram_user_id=u.telegram_user_id WHERE u.telegram_user_id=%s",
                (telegram_user_id,),
            ).fetchone()
        if not row:
            return {"connected": False, "paper_automation_enabled": False}
        return {
            "name": row["first_name"],
            "connected": row["connected_at"] is not None,
            "provider": row["provider"],
            "environment": row["environment"],
            "paper_automation_enabled": bool(row["paper_automation_enabled"]),
            "max_order_notional": row["max_order_notional"],
            "max_daily_loss": row["max_daily_loss"],
        }

    def connection_environment(self, user_id: int) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT environment FROM bybit_connections WHERE telegram_user_id=%s",
                (user_id,),
            ).fetchone()
        return row["environment"] if row else None

    def connection_provider(self, user_id: int) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT provider FROM bybit_connections WHERE telegram_user_id=%s",
                (user_id,),
            ).fetchone()
        return row["provider"] if row else None

    def exchange_credentials(self, user_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT provider,environment,encrypted_api_key,encrypted_api_secret,encrypted_passphrase FROM bybit_connections WHERE telegram_user_id=%s",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_paper_settings(self, telegram_user_id: int, enabled: bool, max_order_notional: Decimal, max_daily_loss: Decimal) -> None:
        if max_order_notional <= 0 or max_daily_loss <= 0:
            raise ValueError("Limits must be positive")
        with self.connection() as conn:
            conn.execute(
                "UPDATE user_automation_settings SET paper_automation_enabled=%s,max_order_notional=%s,max_daily_loss=%s,updated_at=%s WHERE telegram_user_id=%s",
                (enabled, str(max_order_notional), str(max_daily_loss), datetime.now(UTC), telegram_user_id),
            )

    def save_automation_profile(self, user_id: int, enabled: bool, strategy: str, symbol: str, max_order: Decimal, max_trades: int, stop_loss: Decimal, take_profit: Decimal) -> None:
        if strategy != "rsi_reversion_demo" or symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("Unsupported demo strategy or symbol")
        if not (Decimal("1") <= max_order <= Decimal("50") and 1 <= max_trades <= 3 and Decimal("0.5") <= stop_loss <= Decimal("5") and Decimal("1") <= take_profit <= Decimal("10")):
            raise ValueError("One or more limits are outside the demo safety range")
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=%s,strategy=%s,symbol=%s,max_order_notional=%s,max_trades_per_day=%s,stop_loss_pct=%s,take_profit_pct=%s,updated_at=%s WHERE telegram_user_id=%s",
                (enabled, strategy, symbol, str(max_order), max_trades, str(stop_loss), str(take_profit), datetime.now(UTC), user_id),
            )

    def automation_profile(self, user_id: int) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT enabled,strategy,symbol,max_order_notional,max_trades_per_day,stop_loss_pct,take_profit_pct FROM automation_profiles WHERE telegram_user_id=%s",
                (user_id,),
            ).fetchone()
        return dict(row) if row else {}

    def set_kill_switch(self, user_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=FALSE,updated_at=%s WHERE telegram_user_id=%s",
                (datetime.now(UTC), user_id),
            )

    def disconnect_bybit(self, user_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE automation_profiles SET enabled=FALSE,updated_at=%s WHERE telegram_user_id=%s",
                (datetime.now(UTC), user_id),
            )
            conn.execute("DELETE FROM bybit_connections WHERE telegram_user_id=%s", (user_id,))

    def history(self, user_id: int, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT order_id,symbol,side,status,fill_price,quantity,realized_pnl,created_at,updated_at FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def orders_for_sync(self, user_id: int, limit: int = 30) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT idempotency_key,order_id,symbol FROM orders WHERE user_id=%s AND order_id IS NOT NULL ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_exchange_order(self, key: str, status: str, price: Decimal, quantity: Decimal, realized_pnl: Decimal) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET status=%s,fill_price=%s,quantity=%s,realized_pnl=%s,updated_at=%s WHERE idempotency_key=%s",
                (status, str(price), str(quantity), str(realized_pnl), datetime.now(UTC), key),
            )

    def demo_automation_users(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.*,c.encrypted_api_key,c.encrypted_api_secret FROM automation_profiles p JOIN bybit_connections c ON c.telegram_user_id=p.telegram_user_id WHERE p.enabled=TRUE AND c.provider='bybit' AND c.environment='demo'"
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_order(self, key: str) -> OrderResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM orders WHERE idempotency_key=%s", (key,)).fetchone()
        if not row or not row["order_id"]:
            return None
        return OrderResult(row["order_id"], row["status"], Decimal(row["fill_price"]), Decimal(row["quantity"]))

    def reserve(self, intent: OrderIntent) -> bool:
        now = datetime.now(UTC)
        try:
            with self.connection() as conn:
                conn.execute(
                    "INSERT INTO orders (idempotency_key,user_id,symbol,side,requested_notional,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (intent.idempotency_key, intent.user_id, intent.symbol, intent.side, str(intent.notional_usdt), "PENDING", now, now),
                )
            return True
        except self.psycopg.errors.UniqueViolation:
            return False

    def finalize(self, key: str, result: OrderResult) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET order_id=%s,status=%s,fill_price=%s,quantity=%s,updated_at=%s WHERE idempotency_key=%s",
                (result.order_id, result.status, str(result.price), str(result.quantity), datetime.now(UTC), key),
            )

    def cancel_reservation(self, key: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE orders SET status='REJECTED', updated_at=%s WHERE idempotency_key=%s AND status='PENDING'",
                (datetime.now(UTC), key),
            )

    def today_stats(self, user_id: int) -> tuple[int, Decimal]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS trades, COALESCE(SUM(realized_pnl::numeric),0) AS pnl FROM orders WHERE user_id=%s AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AND created_at < date_trunc('day', NOW() AT TIME ZONE 'UTC') + interval '1 day' AND status IN ('FILLED','NEW')",
                (user_id,),
            ).fetchone()
        return int(row["trades"]), Decimal(str(row["pnl"]))

    # --- Signals --------------------------------------------------------

    def insert_signal(
        self,
        user_id: int,
        inst_id: str,
        token_input: str,
        strategy: str,
        direction: str,
        overall_confidence: int,
        analysis: dict,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO signals (user_id,inst_id,token_input,strategy,direction,"
                "overall_confidence,analysis_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user_id, inst_id, token_input, strategy, direction,
                    int(overall_confidence), json.dumps(analysis),
                    datetime.now(UTC),
                ),
            )

    def signals_history(self, user_id: int, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT inst_id,token_input,strategy,direction,overall_confidence,"
                "analysis_json,created_at FROM signals "
                "WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            entry = {k: row[k] for k in row.keys() if k != "analysis_json"}
            try:
                entry["analysis"] = json.loads(row["analysis_json"])
            except (json.JSONDecodeError, TypeError):
                entry["analysis"] = None
            out.append(entry)
        return out


class Database:
    """Select Postgres when DATABASE_URL is set; otherwise retain SQLite locally."""

    def __init__(self, path: Path, database_url: str | None = None):
        self._impl = PostgreSQLDatabase(database_url) if database_url else SQLiteDatabase(path)

    def __getattr__(self, name):
        return getattr(self._impl, name)
