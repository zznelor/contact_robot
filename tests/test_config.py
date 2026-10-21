"""Нормализация телефона (bitrix.py) и вычисляемые настройки (config.Settings)."""

import pytest

from first_contact.bitrix import normalize_phone
from first_contact.config import Settings


@pytest.mark.parametrize("raw, expected", [
    ("+7 (999) 000-00-01", "+79990000001"),
    ("89990000001", "+79990000001"),
    ("8 999 000-00-01", "+79990000001"),
    ("9990000001", "+79990000001"),     # без кода страны
    ("+79990000001", "+79990000001"),
    ("abc", None),                      # нечисловое
    ("", None),
    (None, None),
    ("12-34-5", None),                  # слишком коротко для номера
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("deal_category_id, expected", [
    (None, "0"),     # не выбрана — основная воронка, как показывает панель
    ("", "0"),
    ("0", "0"),
    ("3", "3"),
])
def test_settings_category(deal_category_id, expected):
    assert Settings(deal_category_id=deal_category_id).category == expected


@pytest.mark.parametrize("webhook, expected", [
    ("https://p.bitrix24.ru/rest/1/abc/", "https://p.bitrix24.ru"),
    ("", ""),
    ("https://p.bitrix24.ru/", ""),     # не вебхук REST — портала не знаем
])
def test_settings_portal(webhook, expected):
    assert Settings(bitrix_webhook=webhook).portal == expected
