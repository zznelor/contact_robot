"""Разбор истории диалога (orbita.py): ответил ли клиент, первое его сообщение, ответил ли менеджер."""

from datetime import datetime, timedelta

import pytest

from conftest import UTC
from first_contact.orbita import CLOCK_SKEW, Message, answered_after, client_replied, first_incoming

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)   # наше первое сообщение


def msg(outgoing, minutes=0.0, seconds=0.0, at=...):
    when = T0 + timedelta(minutes=minutes, seconds=seconds) if at is ... else at
    return Message("", "", when, outgoing)


# ---------- client_replied ----------

@pytest.mark.parametrize("messages, expected", [
    ([], None),                                           # истории нет — не понять
    ([msg(True), msg(None, minutes=5)], None),            # сообщение без направления — не понять
    ([msg(True), msg(True, minutes=5)], False),           # только наши
    ([msg(True), msg(False, minutes=5)], True),
    ([msg(False, minutes=-60), msg(True)], False),        # старая переписка с этим человеком не считается
])
def test_client_replied(messages, expected):
    assert client_replied(messages, T0) is expected


@pytest.mark.parametrize("shift, expected", [
    (-CLOCK_SKEW, True),                                  # ровно на запас по часам раньше — ещё считается
    (-CLOCK_SKEW - timedelta(seconds=1), None),           # раньше запаса — отсечено, остального нет
])
def test_client_replied_clock_skew_edge(shift, expected):
    assert client_replied([msg(False, at=T0 + shift)], T0) is expected


def test_client_replied_without_since_takes_whole_history():
    assert client_replied([msg(False, minutes=-600)]) is True


@pytest.mark.parametrize("messages, expected", [
    ([msg(True), msg(False, at=None)], None),               # без времени не расположить относительно нашего
    ([msg(True), msg(False, at=None), msg(False, minutes=5)], True),   # есть входящее со временем после нашего
    ([msg(True), msg(True, at=None)], False),                # наше без времени ответом клиента не бывает
])
def test_client_replied_message_without_time(messages, expected):
    """Сообщение без времени нельзя расположить относительно нашего первого: ответом его не считаем,
    молчанием тоже — если ничего другого нет, «не понять» (а робот зовёт человека)."""
    assert client_replied(messages, T0) is expected


def test_client_replied_without_since_counts_message_without_time():
    assert client_replied([msg(False, at=None)]) is True


# ---------- first_incoming ----------

def test_first_incoming_is_earliest_client_message():
    found = first_incoming([msg(True), msg(False, minutes=7), msg(False, minutes=3), msg(True, minutes=4)], T0)
    assert found.at == T0 + timedelta(minutes=3)


@pytest.mark.parametrize("messages", [
    [msg(True), msg(True, minutes=3)],                     # клиент не писал
    [msg(False, at=None)],                                 # без времени — не годится для паузы ответа
    [msg(None, minutes=3)],                                # без направления — не клиент
    [msg(False, at=T0 - CLOCK_SKEW - timedelta(seconds=1))],  # до нашего сообщения с учётом запаса
])
def test_first_incoming_none(messages):
    assert first_incoming(messages, T0) is None


def test_first_incoming_within_clock_skew_counted():
    assert first_incoming([msg(False, at=T0 - CLOCK_SKEW)], T0).at == T0 - CLOCK_SKEW


# ---------- answered_after ----------

@pytest.mark.parametrize("messages, expected", [
    ([msg(True, minutes=1)], True),                        # мы написали после
    ([msg(True)], False),                                  # ровно в тот момент — не «после»
    ([msg(True, minutes=-1)], False),
    ([msg(False, minutes=1)], False),                      # написал клиент, не мы
    ([msg(None, minutes=1)], False),                       # направление неизвестно
    ([msg(True, at=None)], False),
])
def test_answered_after(messages, expected):
    assert answered_after(messages, T0) is expected
