"""Общие типы для работы с Орбитой и разбор истории диалога.

Сам клиент — orbita_api.py (REST API v1). MCP Орбиты робот не использует: «написать первым»
есть только в REST API.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# Запас на расхождение часов сервиса и Орбиты
CLOCK_SKEW = timedelta(minutes=2)


class OrbitaError(RuntimeError):
    pass


@dataclass
class FirstSendResult:
    status: str                # sent | pending | no_messenger; один канал: not_found | unchecked | invalid_phone
    dialog_id: str | None = None
    channel: str | None = None
    raw: Any = None
    request_key: str | None = None   # pending (202): исход придёт вебхуком с этим ключом


@dataclass
class ReplyResult:
    status: str                # sent (201, мессенджер подтвердил) | pending (202, исход придёт вебхуком)
    request_key: str | None = None


@dataclass
class Message:
    author: str
    text: str
    at: datetime | None
    outgoing: bool | None = None   # True — написали мы (робот или менеджер), False — клиент


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _after(messages: list[Message], since: datetime | None) -> list[Message]:
    """Сообщения не раньше since (старая переписка с этим человеком не считается)."""
    if since is None:
        return messages
    edge = since - CLOCK_SKEW
    return [m for m in messages if m.at is None or m.at.tzinfo is None or m.at >= edge]


def client_replied(messages: list[Message], since: datetime | None = None) -> bool | None:
    """Ответил ли клиент после нашего первого сообщения. None — не понять (нет направлений)."""
    messages = _after(messages, since)
    if not messages or any(m.outgoing is None for m in messages):
        return None
    return any(not m.outgoing for m in messages)


def first_incoming(messages: list[Message], since: datetime | None = None) -> Message | None:
    """Первое сообщение клиента после since."""
    incoming = [m for m in _after(messages, since) if m.outgoing is False and m.at is not None]
    return min(incoming, key=lambda m: m.at) if incoming else None


def answered_after(messages: list[Message], moment: datetime) -> bool:
    """Написали ли мы (менеджер) что-нибудь после moment — тогда ответ по шаблону не нужен."""
    return any(m.outgoing and m.at is not None and m.at > moment for m in messages)
