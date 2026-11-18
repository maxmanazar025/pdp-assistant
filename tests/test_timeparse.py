from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from memora.timeparse import parse_when, translit

TZ = ZoneInfo("Asia/Tashkent")
BASE = datetime(2026, 11, 17, 9, 35, tzinfo=TZ)  # Tuesday


def at(text: str) -> str | None:
    result = parse_when(text, BASE)
    return result.when.strftime("%Y-%m-%d %H:%M") if result else None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ertaga 15:00", "2026-11-18 15:00"),
        ("bugun kechqurun", "2026-11-17 19:00"),
        ("indinga ertalab", "2026-11-19 08:00"),
        ("20 daqiqadan keyin", "2026-11-17 09:55"),
        ("2 soatdan keyin", "2026-11-17 11:35"),
        ("yarim soatdan keyin", "2026-11-17 10:05"),
        ("juma kuni soat 3 da", "2026-11-20 15:00"),
        ("dushanba 9:30", "2026-11-23 09:30"),
        ("yakshanba kechqurun 8 da", "2026-11-22 20:00"),
        ("shanba 10:00", "2026-11-21 10:00"),
        ("soat 18:30 da", "2026-11-17 18:30"),
        ("soat 8 da", "2026-11-18 08:00"),            # 08:00 already passed today
        ("ertaga ertalab 7 da", "2026-11-18 07:00"),
        ("ertaga soat 3 yarim", "2026-11-18 15:30"),
        ("25-noyabr 14:00", "2026-11-25 14:00"),
        ("25.11 14:00", "2026-11-25 14:00"),
        ("2026-12-01 09:00", "2026-12-01 09:00"),
        ("3 kundan keyin 10:00", "2026-11-20 10:00"),
        # Uzbek Cyrillic
        ("эртага 15:00", "2026-11-18 15:00"),
        ("индинга кечқурун", "2026-11-19 19:00"),
        ("20 дақиқадан кейин", "2026-11-17 09:55"),
        ("жума куни соат 3 да", "2026-11-20 15:00"),
        # Russian
        ("завтра в 10", "2026-11-18 10:00"),
        ("через 15 минут", "2026-11-17 09:50"),
        ("послезавтра вечером", "2026-11-19 19:00"),
        ("в пятницу 16:00", "2026-11-20 16:00"),
        # English
        ("tomorrow at 5pm", "2026-11-18 17:00"),
        ("in 45 minutes", "2026-11-17 10:20"),
        ("friday 14:00", "2026-11-20 14:00"),
    ],
)
def test_parse_when(text, expected):
    assert at(text) == expected


def test_same_weekday_rolls_to_next_week_when_time_passed():
    assert at("seshanba 08:00") == "2026-11-24 08:00"
    assert at("seshanba 18:00") == "2026-11-17 18:00"


def test_unparseable_returns_none():
    assert parse_when("matematikadan uy vazifasi", BASE) is None
    assert parse_when("", BASE) is None


def test_flags():
    only_date = parse_when("ertaga", BASE)
    assert only_date and only_date.has_date and not only_date.has_time
    only_time = parse_when("soat 18:00", BASE)
    assert only_time and only_time.has_time and not only_time.has_date


def test_translit():
    assert translit("Эртага Соат Ўнда") == "ertaga soat o'nda"
    assert translit("Қўшимча ғоя") == "qo'shimcha g'oya"
