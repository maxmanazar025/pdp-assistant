"""Deterministic fast path: commands and the persistent reply keyboard. No LLM involved."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select

from memora import timeutil, ui
from memora.db import kv_get, kv_set, session_scope
from memora.db.models import Notification, Task
from memora.flows import Out, deliver
from memora.services import reports
from memora.services import state as st
from memora.textfmt import esc
from memora.timetable import service as timetable
from memora.timetable.sync import sync_timetable

log = logging.getLogger(__name__)
router = Router(name="menu")

HELP = """🤖 <b>Memora OS</b> — shaxsiy o'quv yordamchingiz

<b>Tugmalar</b>
📅 Bugungi / 📆 Ertangi jadval — darslar, tadbirlar, vazifalar
📋 Vazifalarim — ochiq vazifalar va eslatmalar
📖 O'qish rejimi — fan bo'yicha murabbiy, test va kartochkalar
📊 Haftalik hisobot — hafta yakunlari

<b>Erkin yozish yoki ovozli xabar</b>
• «Ertaga soat 3 da kutubxonada uchrashuv qo'y»
• «20 daqiqadan keyin choy damlashni eslat»
• «Juma kuni bo'sh vaqtim bormi?»
• «Python'da dekoratorlarni tushuntir»
• «Oxirgi dars bo'yicha meni test qil»

<b>Avtomatik</b>
☀️ 07:30 — kunlik reja · 🔔 darsdan 15 daqiqa oldin eslatma
📝 dars tugagach — mavzu va uy vazifasini so'rayman · 📊 yakshanba kechqurun — hisobot

<b>Buyruqlar</b>
/status — tizim holati · /sync — jadvalni yangilash
/darslar — yozilmagan darslar · /pauza 3 — eslatmalarni 3 kunga to'xtatish
/davom — eslatmalarni yoqish · /cancel — joriy amalni bekor qilish"""


async def build_screen(app, name: str) -> Out:  # noqa: ANN001
    """Render a menu screen purely from Postgres."""
    async with session_scope() as session:
        if name == "today":
            return Out(await reports.render_day(session, app, timeutil.today()), ui.schedule_kb(True))
        if name == "tomorrow":
            return Out(await reports.render_day(session, app, timeutil.today() + timedelta(days=1)), ui.schedule_kb(False))
        if name == "tasks":
            return Out(*await reports.render_tasks(session))
        if name == "week":
            return Out(await reports.render_weekly(session))
        if name == "pending":
            return Out(*await reports.render_pending_lessons(session))
        if name == "study":
            courses = await timetable.list_courses(session)
            return Out("📖 <b>O'qish rejimi</b>\nQaysi fan bo'yicha shug'ullanamiz?",
                       ui.study_courses_kb([(c.id, c.title) for c in courses]))
    raise ValueError(name)


@router.message(CommandStart())
async def cmd_start(message: Message, app) -> None:  # noqa: ANN001
    await app.notifier.send(
        f"Assalomu alaykum, {esc(app.settings.owner_name)}! 👋\n\n"
        "Men <b>Memora OS</b> — dars jadvali, vazifalar va o'qishingizni nazorat qiladigan yordamchingizman.\n"
        "Pastdagi tugmalardan foydalaning yoki shunchaki yozing / ovozli xabar yuboring.\n\n"
        "Batafsil: /help",
        ui.main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, app) -> None:  # noqa: ANN001
    await app.notifier.send(HELP, ui.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, app) -> None:  # noqa: ANN001
    async with session_scope() as session:
        await st.reset_state(session, app.chat_id)
    await app.notifier.send("↩️ Joriy amal bekor qilindi.", ui.main_menu())


@router.message(Command("darslar"))
async def cmd_pending(message: Message, app) -> None:  # noqa: ANN001
    await deliver(app, [await build_screen(app, "pending")])


@router.message(Command("sync"))
async def cmd_sync(message: Message, app) -> None:  # noqa: ANN001
    report = await sync_timetable(app, announce=True)
    if report.errors:
        await app.notifier.send("⚠️ Jadvalni yangilashda xatolik:\n" + esc("\n".join(report.errors)))
    elif not report.has_changes:
        await app.notifier.send(f"✅ Jadval yangilandi — o'zgarish yo'q ({report.total_active} ta dars).")


@router.message(Command("pauza"))
async def cmd_pause(message: Message, command: CommandObject, app) -> None:  # noqa: ANN001
    days = int(command.args) if command.args and command.args.strip().isdigit() else 1
    until = timeutil.day_bounds(timeutil.today() + timedelta(days=max(1, min(days, 60))))[0]
    async with session_scope() as session:
        await kv_set(session, "mute_until", timeutil.iso(until))
    await app.notifier.send(
        f"⏸ Dars eslatmalari va ertalabki reja <b>{timeutil.fmt_date(until.date())}</b>gacha to'xtatildi.\n"
        "Vazifa eslatmalari ishlashda davom etadi. Qayta yoqish: /davom"
    )


@router.message(Command("davom"))
async def cmd_resume(message: Message, app) -> None:  # noqa: ANN001
    async with session_scope() as session:
        await kv_set(session, "mute_until", None)
    await app.notifier.send("▶️ Eslatmalar qayta yoqildi.")


@router.message(Command("status"))
async def cmd_status(message: Message, app) -> None:  # noqa: ANN001
    async with session_scope() as session:
        sync = await kv_get(session, "timetable:last_sync") or {}
        beat = await kv_get(session, "worker:heartbeat")
        gsync = await kv_get(session, "google:last_sync") or {}
        mute = await kv_get(session, "mute_until")
        unpushed = (await session.execute(select(func.count(Task.id)).where(Task.needs_push.is_(True)))).scalar_one()
        failed = (
            await session.execute(
                select(func.count(Notification.id)).where(
                    Notification.status == "failed", Notification.created_at >= timeutil.now() - timedelta(days=1))
            )
        ).scalar_one()
        state, _ = await st.get_state(session, app.chat_id)
    beat_age = int((timeutil.now() - timeutil.parse_iso_local(beat)).total_seconds()) if beat else None
    worker = "🟢 ishlayapti" if beat_age is not None and beat_age < 180 else "🔴 javob bermayapti"
    chains = app.llm.describe()
    lines = [
        "🩺 <b>Tizim holati</b>",
        f"• Fon ishchisi: {worker}" + (f" ({beat_age} s oldin)" if beat_age is not None else ""),
        f"• Dars jadvali: {'🟢' if sync.get('ok') else '🔴'} {esc(sync.get('at', 'hali sinxronlanmagan'))} "
        f"— {sync.get('active', 0)} ta dars",
        f"• Google: {'🟢 ulangan' if app.google.enabled else '⚪️ ulanmagan'}"
        + (f", oxirgi sinxron {esc(gsync.get('at', '—'))}" if app.google.enabled else "")
        + (f" ⚠️ {esc(str(gsync.get('error'))[:120])}" if gsync.get("error") else ""),
        f"• Google'ga yuborilmagan vazifalar: {unpushed}",
        f"• LLM (asosiy): {esc(' → '.join(chains['smart']) or 'sozlanmagan')}",
        f"• LLM (tezkor): {esc(' → '.join(chains['fast']) or 'sozlanmagan')}",
        f"• 24 soatda yuborilmagan xabarlar: {failed}",
        f"• Suhbat holati: <code>{state}</code>",
    ]
    if mute:
        lines.append(f"• ⏸ Eslatmalar to'xtatilgan: {esc(mute)} gacha")
    if sync.get("errors"):
        lines.append("• Jadval xatolari: " + esc("; ".join(sync["errors"])[:300]))
    await app.notifier.send("\n".join(lines))


@router.message(F.text.in_(ui.MENU_BUTTONS))
async def on_menu_button(message: Message, app) -> None:  # noqa: ANN001
    started = time.perf_counter()
    screen = await build_screen(app, ui.MENU_BUTTONS[message.text])
    render_ms = round((time.perf_counter() - started) * 1000)
    await deliver(app, [screen])
    log.info("fast path", extra={"screen": ui.MENU_BUTTONS[message.text], "render_ms": render_ms})
