"""Regenerates tests/fixtures/*.csv.

The fixtures reproduce the exact shape of the live export (header rows, 4-row group
blocks, CRLF line endings, quoted shared-group cells, a stray whitespace-only cell) with
the real rows for the user's cohorts and their neighbours, captured on 2026-11-17.
Run:  python tests/make_fixtures.py
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SLOTS = ["08:00 - 09:20", "09:30 - 10:50", "11:00 - 12:20", "13:30 - 14:50",
         "15:00 - 16:20", "16:30 - 17:50", "18:00 - 19:20", "19:30 - 20:50"]

PY_P = "Programming (Python) Level 4 (Practice)"
PY_L = "Programming (Python) Level 4 (Lecture)"
DB = "Database Design & Development"
BD = "Big Data & Visualization"
PP = "Professional Practice in the Digital Economy"
BB, QB, DA, KA, UL = "Bekdurdiyev Boburjon", "Qurbonov Behzod", "Davlatov Alisher", "Kamolov Azizbek", "Uzoqov Lochinbek"

# group -> {(day, slot_no): (subject, teacher, room, shared)}
DEGREE = {
    "25-407 (UZ)": {
        ("Tuesday", 1): (PY_P, BB, "231 Room", "25-407 (UZ)"),
        ("Tuesday", 3): (BD, QB, "222 Room", "25-407 (UZ)"),
        ("Tuesday", 4): (PP, KA, "323 Lecture Room", "25-406 (UZ), 25-407 (UZ)"),
        ("Wednesday", 2): (BD, QB, "221 Room", "25-407 (UZ)"),
        ("Wednesday", 3): (PY_P, BB, "222 Room", "25-407 (UZ)"),
        ("Thursday", 2): (PY_L, BB, "133 Lecture Room", "25-407 (UZ), 25-408 (UZ)"),
        ("Friday", 1): (DB, UL, "222 Room", "25-407 (UZ)"),
        ("Friday", 2): (PY_L, BB, "236 Lecture Room", "25-407 (UZ), 25-408 (UZ)"),
        ("Friday", 3): (DB, UL, "222 Room", "25-407 (UZ)"),
    },
    "25-408 (UZ)": {
        ("Monday", 2): (BD, QB, "231 Room", "25-408 (UZ)"),
        ("Monday", 4): (BD, QB, "231 Room", "25-408 (UZ)"),
        ("Tuesday", 2): (PY_P, BB, "231 Room", "25-408 (UZ)"),
        ("Tuesday", 3): (PY_P, BB, "232 Room", "25-408 (UZ)"),
        ("Tuesday", 4): (DB, DA, "232 Room", "25-408 (UZ)"),
        ("Thursday", 1): (PP, KA, "236 Lecture Room", "25-408 (UZ), 25-410 (UZ)"),
        ("Thursday", 2): (PY_L, BB, "133 Lecture Room", "25-407 (UZ), 25-408 (UZ)"),
        ("Thursday", 4): (DB, DA, "232 Room", "25-408 (UZ)"),
        ("Friday", 1): (DB, DA, "232 Room", "25-408 (UZ)"),
        ("Friday", 2): (PY_L, BB, "236 Lecture Room", "25-407 (UZ), 25-408 (UZ)"),
        ("Friday", 3): (PY_P, BB, "220 Lab", "25-408 (UZ)"),
        ("Friday", 4): (PY_P, BB, "231 Room", "25-408 (UZ)"),
    },
    "25-409 (UZ)": {
        ("Monday", 1): (BD, "Usmonov Javohir", "221 Room", "25-409 (UZ)"),
        ("Monday", 3): (BD, "Usmonov Javohir", "221 Room", "25-409 (UZ)"),
        ("Friday", 2): (PP, KA, "323 Lecture Room", "25-405 (UZ), 25-409 (UZ)"),
    },
}
SAY, MAM, SOB = "Sayfuddinova Shoxista Kamol qizi", "Mamadaliyeva Zaynabxon Bahodirjon qizi", "Sobirov Nosirjon Gulamovich"
ENGLISH = {
    "Intermediate 12": {
        ("Tuesday", 2): ("English", SOB, "219 Room", "25-407 (22)"),
        ("Thursday", 1): ("English", SOB, "219 Room", "25-407 (22)"),
    },
    "Intermediate 13": {
        ("Monday", 3): ("English", SAY, "229 Room", "25-408 (18)"),
        ("Thursday", 3): ("English", SAY, "229 Room", "25-408 (18)"),
    },
    "Intermediate 14": {
        ("Monday", 2): ("English", MAM, "230 Room", "25-409 (16), 25-401 (6)"),
        ("Wednesday", 3): ("English", MAM, "230 Room", "25-409 (16), 25-401 (6)"),
    },
}


def build(groups: dict, slots_per_day: int, whitespace_cell: tuple[str, str, int] | None = None) -> str:
    width = 1 + 7 * slots_per_day
    day_row = [""] * width
    slot_row = ["Class"] + [""] * (width - 1)
    col_of: dict[tuple[str, int], int] = {}
    for d, day in enumerate(DAYS):
        day_row[1 + d * slots_per_day] = day
        for s in range(slots_per_day):
            col = 1 + d * slots_per_day + s
            slot_row[col] = f"{s + 1}. {SLOTS[s]}"
            col_of[(day, s + 1)] = col
    rows = [day_row, slot_row]
    for name, cells in groups.items():
        block = [[""] * width for _ in range(4)]
        block[0][0] = name
        for key, values in cells.items():
            for line, value in enumerate(values):
                block[line][col_of[key]] = value
        if whitespace_cell and whitespace_cell[0] == name:
            block[0][col_of[(whitespace_cell[1], whitespace_cell[2])]] = " "
        rows.extend(block)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    return buf.getvalue().rstrip("\r\n")


def main() -> None:
    out = Path(__file__).parent / "fixtures"
    out.mkdir(exist_ok=True)
    (out / "sheet_degree.csv").write_text(build(DEGREE, 8, ("25-408 (UZ)", "Wednesday", 1)), newline="")
    (out / "sheet_english.csv").write_text(build(ENGLISH, 7), newline="")
    # Variant mandated by the spec: everything inside one multi-line cell.
    width = 1 + 7 * 8
    rows = [[""] * width for _ in range(4)]
    rows[0][1], rows[0][9] = "Monday", "Tuesday"
    for d in range(7):
        if d > 1:
            rows[0][1 + d * 8] = DAYS[d]
        for s in range(8):
            rows[1][1 + d * 8 + s] = f"{s + 1}. {SLOTS[s]}"
    rows[1][0] = "Class"
    rows[2][0] = "25-408"
    rows[2][2] = "Big Data & Visualization\n231 Room\nQurbonov Behzod\n25-408 (UZ)"
    rows[2][10] = "Programming (Python) Level 4 (Practice)\n220 Lab\nBekdurdiyev Boburjon"
    rows[3][0] = "25-4080"
    rows[3][3] = "Must Not Match\n101 Room\nNobody"
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    (out / "sheet_multiline.csv").write_text(buf.getvalue(), newline="")


if __name__ == "__main__":
    main()
