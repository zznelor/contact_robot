"""Состояние сервиса в SQLite: сделки, журнал, настройки, пользователи.

Схема создаётся и дополняется сама при запуске (см. MIGRATIONS), руками ничего делать не надо.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Статусы сделки в сервисе:
#   new            — увидели, ещё не обработали (или упали и повторим)
#   pending_first  — Орбита ответила 202: сообщение могло уйти, ждём вебхук message.sent / failed
#   sent_first     — первое сообщение ушло, ждём ответа до followup_due_at
#   replied        — клиент ответил; робот больше не пишет (кроме ответа по шаблону, если он включён)
#   sent_followup  — напоминание ушло, ждём ответа
#   no_reply       — клиент не ответил, напоминание выключено; робот закончил
#   no_messenger   — мессенджер не найден, этап сделки сменён
#   no_phone       — в контакте нет телефона
#   skipped        — сделку закрыли, увели с этапа или остановили вручную
#   needs_check    — не смогли понять, ушло ли сообщение или ответил ли клиент; нужен человек
#   error          — ошибка после нескольких попыток, нужен человек

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    deal_id          INTEGER PRIMARY KEY,
    status           TEXT NOT NULL,
    name             TEXT,
    phone            TEXT,
    dialog_id        TEXT,
    channel          TEXT,
    request_key      TEXT,
    first_text       TEXT,
    first_sent_at    TEXT,
    followup_due_at  TEXT,
    followup_sent_at TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orbita_events (id TEXT PRIMARY KEY, type TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    deal_id INTEGER,
    event   TEXT NOT NULL,
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL UNIQUE,
    name       TEXT,
    role       TEXT NOT NULL,          -- admin | operator
    pw_hash    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    created_by INTEGER,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
"""

# Колонки, добавленные после первых установок: (таблица, колонка, тип)
MIGRATIONS = [
    ("deals", "request_key", "TEXT"),        # REST API Орбиты, 20.10.2026
    ("deals", "client_replied_at", "TEXT"),  # ответ клиенту по шаблону, 20.10.2026
    ("deals", "reply_due_at", "TEXT"),
    ("deals", "reply_sent_at", "TEXT"),
    ("deals", "checked_at", "TEXT"),         # когда последний раз смотрели, не ответил ли клиент
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)  # доступ только из одного event loop
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # Статус версий с MCP: сделка ждала действия «написать первым» — теперь оно есть
        self.conn.execute("UPDATE deals SET status='new' WHERE status='waiting_orbita'")

    # ---------- служебное ----------

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value) -> None:
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, str(value)))

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT * FROM settings")}

    def save_settings(self, values: dict) -> None:
        for k, v in values.items():
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                              (k, json.dumps(v, ensure_ascii=False)))

    # ---------- сделки ----------

    def deal(self, deal_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()

    def add_deal(self, deal_id: int) -> bool:
        """True, если сделка новая для сервиса."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO deals (deal_id, status, created_at, updated_at) VALUES (?, 'new', ?, ?)",
            (deal_id, now(), now()),
        )
        return cur.rowcount == 1

    def update(self, deal_id: int, **fields) -> None:
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE deals SET {cols} WHERE deal_id=?", (*fields.values(), deal_id))

    def deal_by(self, column: str, value) -> sqlite3.Row | None:
        assert column in ("dialog_id", "request_key")
        return self.conn.execute(
            f"SELECT * FROM deals WHERE {column}=? ORDER BY deal_id DESC LIMIT 1", (str(value),)
        ).fetchone()

    def first_seen_event(self, event_id: str, type_: str) -> bool:
        """True, если событие Орбиты пришло впервые (повтор доставки несёт тот же номер)."""
        cur = self.conn.execute("INSERT OR IGNORE INTO orbita_events VALUES (?, ?, ?)",
                                (str(event_id), type_, now()))
        return cur.rowcount == 1

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM deals WHERE status IN ({marks}) ORDER BY deal_id", statuses
        ).fetchall()

    def replies_due(self, at: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deals WHERE status='replied' AND reply_due_at IS NOT NULL AND reply_sent_at IS NULL"
            " AND reply_due_at<=? ORDER BY deal_id", (at,)
        ).fetchall()

    def log(self, deal_id: int | None, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (at, deal_id, event, detail) VALUES (?, ?, ?, ?)",
            (now(), deal_id, event, detail),
        )

    def events(self, deal_id: int | None = None, limit: int = 300) -> list[sqlite3.Row]:
        if deal_id is None:
            return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return self.conn.execute("SELECT * FROM events WHERE deal_id=? ORDER BY id DESC LIMIT ?",
                                 (deal_id, limit)).fetchall()

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in
                self.conn.execute("SELECT status, COUNT(*) AS n FROM deals GROUP BY status")}

    def recent(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deals ORDER BY deal_id DESC LIMIT ?", (limit,)
        ).fetchall()
