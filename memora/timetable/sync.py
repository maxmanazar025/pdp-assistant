"""Timetable ingestion: fetch -> parse -> diff -> upsert -> proactive change alert."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time as dtime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from memora import timeutil
from memora.db import kv_get, kv_set, session_scope
from memora.db.models import Course, TimetableChange, TimetableEntry
from memora.integrations.sheets import SheetFetchError, fetch_csv, source_label
from memora.textfmt import esc
from memora.timetable.parser import ParsedEntry, TimetableParseError, parse_timetable_csv
from memora.timeutil import WEEKDAYS_UZ

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_groups: list[str] = field(default_factory=list)
    total_active: int = 0
    first_sync: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def _snapshot(entry: TimetableEntry) -> dict[str, Any]:
    return {
        "group": entry.group_key, "weekday": entry.weekday, "slot_no": entry.slot_no,
        "start": entry.start_time.strftime("%H:%M"), "end": entry.end_time.strftime("%H:%M"),
        "subject": entry.subject, "room": entry.room, "teacher": entry.teacher,
    }


async def ensure_course(session: AsyncSession, group_key: str, key: str, title: str) -> int:
    await session.execute(
        pg_insert(Course).values(group_key=group_key, key=key, title=title)
        .on_conflict_do_nothing(constraint="uq_course_group_key")
    )
    return (
        await session.execute(select(Course.id).where(Course.group_key == group_key, Course.key == key))
    ).scalar_one()


async def apply_entries(session: AsyncSession, parsed: list[ParsedEntry], groups_found: set[str]) -> SyncReport:
    """Diff parsed entries against storage and upsert. Only groups actually present in
    the fetched sheets are touched, so a failed tab can never wipe a cohort's schedule."""
    report = SyncReport()
    existing = {
        (e.group_key, e.weekday, e.start_time): e
        for e in (await session.execute(select(TimetableEntry))).scalars()
    }
    seen: set[tuple] = set()
    for item in parsed:
        seen.add(item.natural_key)
        course_id = await ensure_course(session, item.group_key, item.course_key, item.course_title)
        row = existing.get(item.natural_key)
        if row is None:
            row = TimetableEntry(
                group_key=item.group_key, group_raw=item.group_raw, weekday=item.weekday, slot_no=item.slot_no,
                start_time=item.start_time, end_time=item.end_time, subject=item.subject, kind=item.kind,
                course_id=course_id, room=item.room, teacher=item.teacher, shared_groups=item.shared_groups,
                source=item.source, is_active=True, content_hash=item.content_hash,
            )
            session.add(row)
            await session.flush()
            report.added.append(_snapshot(row))
            continue
        before = _snapshot(row)
        was_active = row.is_active
        if was_active and row.content_hash == item.content_hash:
            continue
        row.group_raw, row.slot_no, row.end_time = item.group_raw, item.slot_no, item.end_time
        row.subject, row.kind, row.course_id = item.subject, item.kind, course_id
        row.room, row.teacher, row.shared_groups = item.room, item.teacher, item.shared_groups
        row.source, row.content_hash, row.is_active = item.source, item.content_hash, True
        if was_active:
            report.changed.append((before, _snapshot(row)))
        else:
            report.added.append(_snapshot(row))
    for key, row in existing.items():
        if row.is_active and row.group_key in groups_found and key not in seen:
            row.is_active = False
            report.removed.append(_snapshot(row))

    for snap in report.added:
        session.add(_change("added", snap, None, snap))
    for snap in report.removed:
        session.add(_change("removed", snap, snap, None))
    for before, after in report.changed:
        session.add(_change("changed", after, before, after))
    await session.flush()
    report.total_active = len(
        (await session.execute(select(TimetableEntry.id).where(TimetableEntry.is_active.is_(True)))).all()
    )
    return report


def _change(kind: str, ref: dict, before: dict | None, after: dict | None) -> TimetableChange:
    hh, mm = ref["start"].split(":")
    return TimetableChange(
        change_type=kind, group_key=ref["group"], weekday=ref["weekday"],
        start_time=dtime(int(hh), int(mm)), before=before, after=after,
    )


def render_changes(report: SyncReport) -> str:
    """Uzbek change notice. Highlights changes that hit today or tomorrow."""
    today = timeutil.today()
    soon = {today.weekday(): "bugungi", (today + timedelta(days=1)).weekday(): "ertangi"}
    urgent: set[str] = set()

    def when(snap: dict) -> str:
        if snap["weekday"] in soon:
            urgent.add(soon[snap["weekday"]])
        return f"{WEEKDAYS_UZ[snap['weekday']]} {snap['start']}–{snap['end']}"

    def details(snap: dict) -> str:
        bits = [esc(snap["subject"])]
        if snap.get("room"):
            bits.append(f"📍 {esc(snap['room'])}")
        if snap.get("teacher"):
            bits.append(f"👤 {esc(snap['teacher'])}")
        return " · ".join(bits)

    lines = ["🔔 <b>Dars jadvali o'zgardi</b>", ""]
    for snap in report.removed:
        lines.append(f"❌ <b>Olib tashlandi</b> — {when(snap)} ({esc(snap['group'])})\n   {details(snap)}")
    for snap in report.added:
        lines.append(f"➕ <b>Yangi dars</b> — {when(snap)} ({esc(snap['group'])})\n   {details(snap)}")
    for before, after in report.changed:
        lines.append(f"✏️ <b>O'zgardi</b> — {when(after)} ({esc(after['group'])})")
        if before["subject"] != after["subject"]:
            lines.append(f"   📘 {esc(before['subject'])} → <b>{esc(after['subject'])}</b>")
        else:
            lines.append(f"   📘 {esc(after['subject'])}")
        if before["room"] != after["room"]:
            lines.append(f"   📍 {esc(before['room'] or '—')} → <b>{esc(after['room'] or '—')}</b>")
        if before["teacher"] != after["teacher"]:
            lines.append(f"   👤 {esc(before['teacher'] or '—')} → <b>{esc(after['teacher'] or '—')}</b>")
        if before["end"] != after["end"]:
            lines.append(f"   🕒 tugashi {before['end']} → <b>{after['end']}</b>")
    if urgent:
        lines += ["", f"⚠️ <b>Diqqat:</b> o'zgarish {' va '.join(sorted(urgent))} darslarga ta'sir qiladi."]
    return "\n".join(lines)


async def sync_timetable(app, *, announce: bool = True) -> SyncReport:  # noqa: ANN001 - App (avoids import cycle)
    """Full sync cycle. Safe to call concurrently with the bot; all writes are one transaction."""
    settings = app.settings
    parsed: list[ParsedEntry] = []
    found: set[str] = set()
    errors: list[str] = []
    for url in settings.sheet_urls:
        label = source_label(url)
        try:
            entries, groups = parse_timetable_csv(await fetch_csv(url), settings.groups, label)
            parsed.extend(entries)
            found |= groups
        except (SheetFetchError, TimetableParseError) as exc:
            errors.append(f"{label}: {exc}")
            log.warning("timetable tab failed", extra={"tab": label, "error": str(exc)})

    async with session_scope() as session:
        report = await apply_entries(session, parsed, found)
        report.errors = errors
        report.missing_groups = sorted(set(settings.groups) - found) if not errors else []
        report.first_sync = not await kv_get(session, "timetable:initialized", False)
        if found:
            await kv_set(session, "timetable:initialized", True)
        await kv_set(session, "timetable:last_sync", {
            "at": timeutil.iso(timeutil.now()), "ok": not errors, "errors": errors,
            "active": report.total_active, "missing": report.missing_groups,
        })

    if errors:
        await app.notifier.alert_admin("Dars jadvalini sinxronlashda xatolik", "\n".join(errors), window_minutes=180)
    if report.missing_groups:
        await app.notifier.alert_admin(
            "Guruh jadvalda topilmadi",
            f"{', '.join(report.missing_groups)} — eski jadval saqlab qolindi. Guruh nomi o'zgargan bo'lishi mumkin.",
            window_minutes=720,
        )
    if announce and report.has_changes:
        if report.first_sync:
            await app.notifier.send(
                f"📚 <b>Dars jadvali yuklandi</b>\nHaftasiga {report.total_active} ta dars "
                f"({esc(', '.join(sorted(found)))}). Har darsdan {settings.class_alert_minutes} daqiqa oldin eslataman."
            )
        else:
            await app.notifier.send(render_changes(report))
    log.info("timetable sync done", extra={
        "added": len(report.added), "removed": len(report.removed), "changed": len(report.changed),
        "errors": len(errors), "active": report.total_active,
    })
    return report
