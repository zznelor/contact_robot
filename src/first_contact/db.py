"""Состояние сервиса в SQLite: сделки, журнал, настройки, пользователи.

Схема создаётся и дополняется сама при запуске (см. MIGRATIONS), руками ничего делать не надо.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:                        # замок базы между процессами (service_lock): POSIX — flock, Windows — msvcrt
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

# Статусы сделки в сервисе:
#   new            — увидели, ещё не обработали (или упали и повторим)
#   pending_first  — Орбита ответила 202: сообщение могло уйти, ждём вебхук message.sent / failed
#   sent_first     — первое сообщение ушло, ждём ответа до followup_due_at
#   replied        — клиент ответил; робот больше не пишет (кроме ответа по шаблону, если он включён)
#   sent_followup  — напоминание ушло, ждём ответа
#   no_reply       — клиент не ответил, напоминание выключено; робот закончил
#   no_messenger   — мессенджер не найден, этап сделки сменён
#   no_phone       — ни у одного контакта сделки нет телефона (или Орбита не приняла ни один номер)
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
-- Входящие вебхуки Орбиты (durable inbox): событие целиком сохраняется до ответа 2xx, обрабатывается
-- отсюда. id — номер события Орбиты (ключ дедупликации). state: new — ждёт обработки (в том числе
-- после сбоя или перезапуска), done — обработано, failed — обработка не удалась MAX_ATTEMPTS раз.
CREATE TABLE IF NOT EXISTS orbita_events (
    id         TEXT PRIMARY KEY,
    type       TEXT,
    at         TEXT NOT NULL,
    body       TEXT,
    state      TEXT NOT NULL DEFAULT 'done',
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dialog_id  TEXT,
    done_at    TEXT
);
-- Исходы отправок Орбиты по request_key (вебхуки message.sent / message.failed на отправки с ответом 202).
-- Исход сопоставляется с отправкой по ключу, а не по статусу сделки: применяется сразу, если ключ уже записан
-- в сделку (request_key / followup_request_key / reply_request_key), иначе ждёт здесь, и запись ключа сверяется
-- с ним той же транзакцией. applied_at пуст — исход ещё не нашёл свою отправку. Хранится EVENTS_KEEP по at.
CREATE TABLE IF NOT EXISTS send_outcomes (
    request_key TEXT PRIMARY KEY,
    ok          INTEGER NOT NULL,      -- 1 — message.sent, 0 — message.failed
    reason      TEXT,                  -- reason_code отказа
    dialog_id   TEXT,
    sent_at     TEXT,                  -- момент отправки из события, как прислала Орбита
    at          TEXT NOT NULL,         -- когда принят
    applied_at  TEXT
);
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

# Индексы — после MIGRATIONS: на старой установке их колонки появляются только миграцией
INDEXES = """
-- очередь необработанных событий (pending_events) и чистка обработанных (purge_events)
CREATE INDEX IF NOT EXISTS orbita_events_state ON orbita_events (state, at);
-- ответы клиента в диалоге после момента (incoming_since)
CREATE INDEX IF NOT EXISTS orbita_events_dialog ON orbita_events (dialog_id, type, at);
"""

# Колонки, добавленные после первых установок: (таблица, колонка, тип)
MIGRATIONS = [
    ("deals", "request_key", "TEXT"),        # REST API Орбиты, 20.10.2026
    ("deals", "client_replied_at", "TEXT"),  # ответ клиенту по шаблону, 20.10.2026
    ("deals", "reply_due_at", "TEXT"),
    ("deals", "reply_sent_at", "TEXT"),
    ("deals", "checked_at", "TEXT"),         # когда последний раз смотрели, не ответил ли клиент
    ("deals", "retry_n", "INTEGER NOT NULL DEFAULT 0"),  # ручные повторы: входят в ключ идемпотентности
    ("deals", "retried_at", "TEXT"),         # последний ручной повтор: от него считаются 48 ч
    # Намерение «написать первым»: пишется до запроса, стирается при определённом исходе. Повтор после
    # неизвестного исхода идёт в тот же канал с тем же ключом (иначе клиент получит два первых), 21.10.2026
    ("deals", "intent_phone", "TEXT"),
    ("deals", "intent_channel", "TEXT"),
    ("deals", "intent_key", "TEXT"),
    # Напоминание и ответ по шаблону: request_key отправки, на которую Орбита ответила 202, — пока не пришёл
    # message.sent / message.failed; номер ручного повтора шага входит в ключ идемпотентности, 21.10.2026
    ("deals", "followup_request_key", "TEXT"),
    ("deals", "reply_request_key", "TEXT"),
    ("deals", "followup_retry_n", "INTEGER NOT NULL DEFAULT 0"),
    ("deals", "reply_retry_n", "INTEGER NOT NULL DEFAULT 0"),
    # Этап сделки в Битриксе до переноса роботом на «Не нашёл мессенджер»: после успешного «Повторить»
    # сделка возвращается на него, 22.10.2026. Пусто — робот не переносил или перенёс до обновления
    ("deals", "stage_before", "TEXT"),
    # Возврат этапа — сохранённая работа: не удался (Битрикс недоступен) — повтор на следующих проходах с
    # нарастающей паузой, 22.10.2026
    # Намерение напоминания и ответа по шаблону: ключ идемпотентности отправки, пишется до запроса и стирается
    # только при определённом отказе (отказ 4xx, message.failed). «Повторить» после неизвестного исхода
    # повторяет этот ключ, а не выпускает новый -r<n> (иначе клиент может получить второе), 22.10.2026
    ("deals", "followup_key", "TEXT"),
    ("deals", "reply_key", "TEXT"),
    # Текст отправки под этим ключом: повтор тем же ключом несёт ровно его (шаблон или название сделки могли
    # поменяться, а с другим телом Орбита ответит idempotency_key_reused), 22.10.2026
    ("deals", "followup_text", "TEXT"),
    ("deals", "reply_text", "TEXT"),
    # Оператор завершил работу робота по сделке («Остановить робота», «Клиент ответил»); стирает «Повторить».
    # Поздние исходы и таймауты отправок, переданных до этого, решение не отменяют, 22.10.2026
    ("deals", "manual_stop_at", "TEXT"),
    # Шаг (followup / reply), чей ключ Орбита отвергла как idempotency_key_reused: исход неизвестен, ключ снят,
    # «Повторить» пойдёт новым -r<n> — панель предупреждает о возможном втором сообщении; «Повторить» снимает, 22.10.2026
    ("deals", "reused_key_step", "TEXT"),
    # Ручной «Повторить» ответа по шаблону: срок отправки (REPLY_MAX_LATE) отсчитывается и от этого момента
    ("deals", "reply_retried_at", "TEXT"),
    ("deals", "stage_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("deals", "stage_retry_at", "TEXT"),
    # Когда первое сообщение ушло в Орбиту с ответом 202 (ждём message.sent), 21.10.2026
    ("deals", "pending_at", "TEXT"),
    # Время следующей попытки шага после сбоя (нарастающая пауза) или срок, названный Орбитой, 21.10.2026
    ("deals", "next_attempt_at", "TEXT"),
    # Вебхуки Орбиты — durable inbox, 21.10.2026. Строки старых версий (тела нет) — уже обработанные: done
    ("orbita_events", "body", "TEXT"),
    ("orbita_events", "state", "TEXT NOT NULL DEFAULT 'done'"),
    ("orbita_events", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("orbita_events", "last_error", "TEXT"),
    ("orbita_events", "dialog_id", "TEXT"),
    ("orbita_events", "done_at", "TEXT"),
]


def service_lock(db_path: Path):
    """Межпроцессный замок робота на базу: файл «<база>.lock» рядом с ней. Сервис `run` держит его, пока работает,
    разовые `once` / `process` — на время своей работы: два процесса робота на одной базе не отправляют и не пишут
    статусы одновременно (у каждого процесса свой Worker.lock). Возвращает открытый файл (замок держится, пока он
    открыт; закрытие или конец процесса, в том числе аварийный, отпускают) или None — замок занят.

    POSIX (Docker, Linux, macOS) — fcntl.flock на весь файл; Windows (запуск без Docker) — msvcrt.locking на
    первый байт файла. Поведение одно: неблокирующий захват, освобождение при закрытии файла."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path.with_name(path.name + ".lock"), "a+")
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)                               # msvcrt.locking считает байты от текущей позиции
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, PermissionError):      # занят: flock — EWOULDBLOCK, locking — EACCES
        f.close()
        return None
    except BaseException:
        f.close()
        raise
    return f


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)  # доступ только из одного event loop
        self.conn.row_factory = sqlite3.Row
        # lower() в SQLite не знает кириллицу — для поиска по имени берём питоновский
        self.conn.create_function("py_lower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.executescript(INDEXES)
        # Первое сообщение, переданное с 202 версией до pending_at: ожидание началось с последней записи строки
        # (тогда это и была запись pending_first). Без него поздний message.sent не узнал бы свою отправку
        self.conn.execute("UPDATE deals SET pending_at=updated_at WHERE status='pending_first' AND pending_at IS NULL")
        # Статус версий с MCP: сделка ждала действия «написать первым» — теперь оно есть
        self.conn.execute("UPDATE deals SET status='new' WHERE status='waiting_orbita'")

    @contextmanager
    def tx(self):
        """Несколько записей одним атомарным переходом: все или ни одной (обрыв процесса посередине не
        оставит половину). Соединение в autocommit, поэтому транзакция явная. Внутри — без await: пока она
        открыта, писать в базу не может никто, а другая корутина того же соединения попала бы в неё.
        Вложенный tx() — часть внешней транзакции."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

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
        """Записать поля сделки одним UPDATE. Смена статуса — новый шаг: счётчик сбоев (attempts) и пауза
        (next_attempt_at) прежнего шага сбрасываются этой же записью, откуда бы переход ни пришёл (шаг, вебхук,
        опрос, кнопка), — если запись не задаёт их сама (сбой шага, пятый сбой → error)."""
        fields["updated_at"] = now()
        sets, args = [f"{k}=?" for k in fields], list(fields.values())
        if "status" in fields:
            for col, reset in (("attempts", "0"), ("next_attempt_at", "NULL")):
                if col not in fields:
                    # В SET справа — значения строки до записи: status здесь ещё прежний
                    sets.append(f"{col}=CASE WHEN status=? THEN {col} ELSE {reset} END")
                    args.append(fields["status"])
        self.conn.execute(f"UPDATE deals SET {', '.join(sets)} WHERE deal_id=?", (*args, deal_id))

    def deal_by(self, column: str, value) -> sqlite3.Row | None:
        if column != "dialog_id":
            raise ValueError(column)
        return self.conn.execute(
            f"SELECT * FROM deals WHERE {column}=? ORDER BY deal_id DESC LIMIT 1", (str(value),)
        ).fetchone()

    # ---------- исходы отправок по request_key ----------

    # Шаг → колонка request_key отправки этого шага, ждущей исхода (ответ Орбиты 202)
    SEND_KEYS = {"first": "request_key", "followup": "followup_request_key", "reply": "reply_request_key"}

    def deal_by_send_key(self, request_key: str) -> tuple[sqlite3.Row, str] | None:
        """Сделка и шаг (first / followup / reply), чья отправка с этим request_key ждёт исхода."""
        for step, col in self.SEND_KEYS.items():
            row = self.conn.execute(f"SELECT * FROM deals WHERE {col}=? ORDER BY deal_id DESC LIMIT 1",
                                    (str(request_key),)).fetchone()
            if row is not None:
                return row, step
        return None

    def save_outcome(self, request_key: str, ok: bool, reason: str | None, dialog_id: str | None,
                     sent_at: str | None) -> None:
        """Сохранить исход отправки (последний по ключу) как ещё не применённый."""
        self.conn.execute(
            "INSERT OR REPLACE INTO send_outcomes (request_key, ok, reason, dialog_id, sent_at, at, applied_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)", (str(request_key), int(ok), reason, dialog_id, sent_at, now()))

    def outcome(self, request_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM send_outcomes WHERE request_key=?", (str(request_key),)).fetchone()

    def outcome_applied(self, request_key: str) -> None:
        self.conn.execute("UPDATE send_outcomes SET applied_at=? WHERE request_key=?", (now(), str(request_key)))

    def purge_outcomes(self, before: str) -> int:
        """Удалить исходы, принятые раньше before (применённые и так и не нашедшие отправку — например, исходы
        тестовых сообщений из настроек или чужих отправок тем же ключом API)."""
        return self.conn.execute("DELETE FROM send_outcomes WHERE at<?", (before,)).rowcount

    # ---------- входящие события Орбиты ----------

    def accept_event(self, event_id: str, type_: str, body: str, dialog_id: str | None) -> bool:
        """Сохранить событие до ответа Орбите. False — такое уже принято (повтор доставки несёт тот же номер)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO orbita_events (id, type, at, body, state, dialog_id) VALUES (?, ?, ?, ?, 'new', ?)",
            (str(event_id), type_, now(), body, dialog_id))
        return cur.rowcount == 1

    def pending_events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orbita_events WHERE state='new' ORDER BY at, rowid").fetchall()

    def inbox_event(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orbita_events WHERE id=?", (str(event_id),)).fetchone()

    def incoming_since(self, dialog_id: str, since: str) -> bool:
        """Принимали ли вебхук message.received от клиента в этом диалоге не раньше since."""
        rows = self.conn.execute(
            "SELECT body FROM orbita_events WHERE type='message.received' AND dialog_id=? AND at>=?",
            (str(dialog_id), since)).fetchall()
        for r in rows:
            try:
                data = json.loads(r["body"] or "").get("data") or {}
            except (ValueError, AttributeError):
                continue
            if data.get("direction", "incoming") == "incoming":
                return True
        return False

    def event_done(self, event_id: str) -> None:
        self.conn.execute("UPDATE orbita_events SET state='done', done_at=?, last_error=NULL WHERE id=?",
                          (now(), str(event_id)))

    def purge_events(self, before: str) -> int:
        """Удалить обработанные (done) события, закрытые раньше before (у строк старых версий done_at нет —
        по времени приёма). Ждущие обработки и failed не трогаются. Возвращает число удалённых."""
        return self.conn.execute(
            "DELETE FROM orbita_events WHERE state='done' AND at<? AND coalesce(done_at, at)<?",
            (before, before)).rowcount

    def event_failed(self, event_id: str, error: str, give_up: bool) -> None:
        self.conn.execute("UPDATE orbita_events SET attempts=attempts+1, last_error=?, state=? WHERE id=?",
                          (error, "failed" if give_up else "new", str(event_id)))

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM deals WHERE status IN ({marks}) ORDER BY deal_id", statuses
        ).fetchall()

    def stages_to_restore(self) -> list[sqlite3.Row]:
        """Первое сообщение ушло, а этап, с которого робот увёл сделку на «Не нашёл мессенджер», не возвращён."""
        return self.conn.execute(
            "SELECT * FROM deals WHERE stage_before IS NOT NULL AND first_sent_at IS NOT NULL ORDER BY deal_id"
        ).fetchall()

    def unconfirmed_sends(self) -> list[sqlite3.Row]:
        """Напоминание или ответ по шаблону переданы с ответом 202, а исхода (вебхука) ещё нет."""
        return self.conn.execute(
            "SELECT * FROM deals WHERE (status='sent_followup' AND followup_request_key IS NOT NULL)"
            " OR (status='replied' AND reply_request_key IS NOT NULL) ORDER BY deal_id"
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

    def find(self, status: str = "", q: str = "", limit: int = 300) -> tuple[list[sqlite3.Row], int]:
        """Сделки по статусу и строке поиска (ID, имя, телефон) — по всей базе. (первые limit, всего)."""
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if q.strip():
            like = f"%{q.strip().lower()}%"
            where.append("(CAST(deal_id AS TEXT) LIKE ? OR py_lower(coalesce(name,'')) LIKE ? OR coalesce(phone,'') LIKE ?)")
            args += [like, like, like]
        cond = f" WHERE {' AND '.join(where)}" if where else ""
        total = self.conn.execute(f"SELECT COUNT(*) FROM deals{cond}", args).fetchone()[0]
        rows = self.conn.execute(f"SELECT * FROM deals{cond} ORDER BY deal_id DESC LIMIT ?", (*args, limit)).fetchall()
        return rows, total

    def recent(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deals ORDER BY deal_id DESC LIMIT ?", (limit,)
        ).fetchall()
