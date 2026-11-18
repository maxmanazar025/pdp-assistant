from datetime import time
from pathlib import Path

import pytest

from memora.timetable.parser import TimetableParseError, match_group, parse_timetable_csv, split_subject

FIX = Path(__file__).parent / "fixtures"
TARGETS = ["25-408", "Intermediate 13"]


def _load(name: str) -> str:
    return (FIX / name).read_text(newline="")


def test_degree_sheet_real_layout():
    entries, found = parse_timetable_csv(_load("sheet_degree.csv"), TARGETS, "degree")
    assert found == {"25-408"}
    assert len(entries) == 12
    assert {e.group_key for e in entries} == {"25-408"}
    by_key = {(e.weekday, e.slot_no): e for e in entries}

    mon2 = by_key[(0, 2)]
    assert (mon2.subject, mon2.room, mon2.teacher) == ("Big Data & Visualization", "231 Room", "Qurbonov Behzod")
    assert (mon2.start_time, mon2.end_time) == (time(9, 30), time(10, 50))
    assert mon2.group_raw == "25-408 (UZ)"

    thu1 = by_key[(3, 1)]
    assert thu1.subject == "Professional Practice in the Digital Economy"
    assert thu1.room == "236 Lecture Room"
    assert thu1.shared_groups == "25-408 (UZ), 25-410 (UZ)"

    fri3 = by_key[(4, 3)]
    assert fri3.room == "220 Lab" and fri3.kind == "practice"
    assert fri3.course_key == "programming (python) level 4"
    assert by_key[(4, 2)].kind == "lecture"
    assert by_key[(4, 2)].course_key == fri3.course_key
    # Whitespace-only cell (present in the live sheet) must not create a phantom class.
    assert (2, 1) not in by_key
    # No Wednesday / weekend classes for this cohort.
    assert {e.weekday for e in entries} == {0, 1, 3, 4}


def test_english_sheet_has_seven_slots_per_day():
    entries, found = parse_timetable_csv(_load("sheet_english.csv"), TARGETS, "english")
    assert found == {"Intermediate 13"}
    assert [(e.weekday, e.slot_no, e.start_time) for e in entries] == [(0, 3, time(11, 0)), (3, 3, time(11, 0))]
    assert entries[0].teacher == "Sayfuddinova Shoxista Kamol qizi"
    assert entries[0].room == "229 Room"
    assert entries[0].shared_groups == "25-408 (18)"


def test_multiline_cell_variant():
    entries, found = parse_timetable_csv(_load("sheet_multiline.csv"), TARGETS, "ml")
    assert found == {"25-408"}
    assert len(entries) == 2
    first, second = entries
    assert (first.weekday, first.room, first.teacher) == (0, "231 Room", "Qurbonov Behzod")
    assert first.shared_groups == "25-408 (UZ)"
    assert (second.weekday, second.slot_no, second.room) == (1, 2, "220 Lab")


def test_group_matching_is_strict():
    assert match_group("25-408 (UZ)", TARGETS) == "25-408"
    assert match_group("25-4080", TARGETS) is None
    assert match_group("Intermediate 130", TARGETS) is None
    assert match_group("Intermediate 1", TARGETS) is None
    assert match_group("intermediate 13", TARGETS) == "Intermediate 13"


def test_split_subject():
    assert split_subject("Programming (Python) Level 4 (Practice)") == ("Programming (Python) Level 4", "practice")
    assert split_subject("English") == ("English", None)


def test_broken_sheet_raises():
    with pytest.raises(TimetableParseError):
        parse_timetable_csv("a,b\nc,d\ne,f\n", TARGETS, "x")
    with pytest.raises(TimetableParseError):
        parse_timetable_csv("<html>Sign in</html>", TARGETS, "x")


def test_content_hash_changes_with_room():
    entries, _ = parse_timetable_csv(_load("sheet_degree.csv"), TARGETS, "degree")
    moved, _ = parse_timetable_csv(_load("sheet_degree.csv").replace("220 Lab", "221 Lab"), TARGETS, "degree")
    changed = [a for a, b in zip(entries, moved) if a.content_hash != b.content_hash]
    assert len(changed) == 1 and changed[0].room == "220 Lab"
