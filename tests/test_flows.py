import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from nutrisnap.bot import Admission, App
from nutrisnap.domain import ServiceError, day_key, now_utc
from nutrisnap.jobs import weekly_reports


def message(text="борщ", uid=1):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        text=text,
        caption=None,
        photo=[],
        document=None,
        message_id=10,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=11)),
    )


def app_for(settings, db, meal):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return App(
        settings,
        db,
        SimpleNamespace(
            estimate=AsyncMock(return_value=meal), report=AsyncMock(return_value="Недельный отчёт")
        ),
        SimpleNamespace(lookup=AsyncMock(return_value=meal)),
        bot,
    )


async def test_text_edit_confirm_dashboard_delete_flow(settings, db, meal):
    app = app_for(settings, db, meal)
    await app.ingest(message())
    row = await db.draft(1)
    assert row
    query = SimpleNamespace(from_user=SimpleNamespace(id=1), data=f"edit:{row['id']}", answer=AsyncMock())
    await app.callback(query)
    await app.ingest(message("порция 500 г"))
    assert app.ai.estimate.call_args.args[2]["title"] == "Борщ"
    query.data = f"accept:{row['id']}"
    await app.callback(query)
    await app.callback(query)
    assert await db.totals(1, day_key(now_utc())) == [180, 12, 8, 15]
    assert len(await db.all("SELECT * FROM dashboards")) == 1
    sends_before_today = app.bot.send_message.await_count
    await app.show_today(message())
    assert app.bot.send_message.await_count == sends_before_today + 1
    await app.delete_me(message())
    assert not await db.all("SELECT * FROM meals")


async def test_dashboard_recovers_after_telegram_deletion(settings, db, meal):
    app = app_for(settings, db, meal)
    await db.user(1)
    await app.update_dashboard(1)
    app.bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(text="x", chat_id=1, message_id=20), message="message to edit not found"
    )
    await app.update_dashboard(1)
    assert app.bot.send_message.await_count == 2
    assert len(await db.all("SELECT * FROM dashboards")) == 1


async def test_barcode_route_skips_ai(settings, db, meal):
    app = app_for(settings, db, meal)
    await app.ingest(message("3017620422003 200 г"))
    app.ai.estimate.assert_not_awaited()
    app.facts.lookup.assert_awaited_once_with("3017620422003", "200 г")
    assert (await db.draft(1))["source"] == "barcode"


async def test_failed_estimate_does_not_consume_user_quota(settings, db, meal):
    app = app_for(settings, db, meal)
    app.ai.estimate.side_effect = ServiceError("Ошибка AI")
    await app.ingest(message())
    assert (await db.one("SELECT fair_count FROM users"))[0] == 0
    assert not await db.all("SELECT * FROM meals")
    assert app.cleanup_tasks
    await app.close()
    deleted = [call.args[1] for call in app.bot.delete_message.await_args_list]
    assert 10 in deleted  # Failed input.
    assert 11 in deleted  # Error notice.


async def test_temporary_notice_expires_with_input_but_preserves_dashboard(settings, db, meal):
    app = app_for(settings, db, meal)
    await db.user(1)
    await app.update_dashboard(1)
    await app.notice(message(), "Сервис AI временно недоступен.", ttl=0)
    await asyncio.gather(*app.cleanup_tasks)
    deleted = [call.args[1] for call in app.bot.delete_message.await_args_list]
    assert set(deleted) == {10, 11}
    assert (await db.one("SELECT message_id FROM dashboards"))[0] == 20
    await app.close()


async def test_empty_draft_hint_is_temporary(settings, db, meal):
    app = app_for(settings, db, meal)
    await app.show_draft(message("/draft"))
    assert app.cleanup_tasks
    assert not await db.all("SELECT * FROM meals")
    await app.close()
    app.bot.delete_message.assert_any_await(1, 10)
    app.bot.delete_message.assert_any_await(1, 11)


async def test_no_delete_permission_does_not_break_notice_cleanup(settings, db, meal):
    from aiogram.methods import DeleteMessage

    app = app_for(settings, db, meal)
    app.bot.delete_message.side_effect = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=10), message="message can't be deleted"
    )
    await app.notice(message(), "Ошибка", ttl=0)
    await asyncio.gather(*app.cleanup_tasks)
    assert app.bot.delete_message.await_count == 2
    await app.close()


async def test_admission_rejects_without_waiting():
    admission = Admission(1)
    async with admission.slot():
        with pytest.raises(ServiceError):
            async with admission.slot():
                pytest.fail("Overload was admitted")
    assert admission.active == 0


async def test_weekly_report_dedup_and_retry_reuses_body(settings, db, meal):
    app = app_for(settings, db, meal)
    at = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)  # Sunday 20:00 Moscow
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    await db.execute("UPDATE meals SET eaten_at=? WHERE id=?", ((at - timedelta(days=1)).isoformat(), mid))
    app.bot.send_message.side_effect = RuntimeError("offline")
    await weekly_reports(app, at)
    assert app.ai.report.await_count == 1
    app.bot.send_message.side_effect = None
    await weekly_reports(app, at)
    await weekly_reports(app, at)
    assert app.ai.report.await_count == 1
    assert app.bot.send_message.await_count == 2
    assert (await db.one("SELECT sent FROM weekly_reports"))[0] == 1


async def test_weekly_report_not_before_schedule(settings, db, meal):
    app = app_for(settings, db, meal)
    await weekly_reports(app, datetime(2026, 10, 18, 16, 59, tzinfo=UTC))
    app.ai.report.assert_not_awaited()


def day_query(action, value, uid=1):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        data=f"day:{action}:{value}",
        message=SimpleNamespace(message_id=20),
        answer=AsyncMock(),
    )


async def test_accepted_meal_edit_flow_and_confirmed_removal(settings, db, meal):
    app = app_for(settings, db, meal)
    await app.ingest(message())
    original = await db.draft(1)
    await app.callback(
        SimpleNamespace(from_user=SimpleNamespace(id=1), data=f"accept:{original['id']}", answer=AsyncMock())
    )
    await app.day_callback(day_query("list", day_key(now_utc())))
    markup = app.bot.edit_message_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"day:open:{original['id']}"
    await app.day_callback(day_query("open", original["id"]))
    await app.day_callback(day_query("edit", original["id"]))
    revision = await db.draft(1, editing=True)
    assert revision and revision["id"] != original["id"]
    assert (await db.totals(1, original["day_key"]))[0] == 180
    app.ai.estimate.return_value = meal.model_copy(update={"kcal": 360, "weight_g": 500})
    await app.ingest(message("На самом деле порция 500 г"))
    await app.callback(
        SimpleNamespace(from_user=SimpleNamespace(id=1), data=f"accept:{revision['id']}", answer=AsyncMock())
    )
    assert (await db.totals(1, original["day_key"]))[0] == 360
    assert len(await db.today_meals(1)) == 1
    await app.day_callback(day_query("remove", original["id"]))
    assert len(await db.today_meals(1)) == 1  # Deletion requires confirmation.
    await app.day_callback(day_query("delete", original["id"]))
    assert await db.today_meals(1) == []
    assert (await db.totals(1, original["day_key"]))[0] == 0
    await app.close()


async def test_cancelling_or_failed_revision_preserves_accepted_meal(settings, db, meal):
    app = app_for(settings, db, meal)
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    await app.day_callback(day_query("edit", mid))
    app.ai.estimate.side_effect = ServiceError("Нет связи с AI")
    await app.ingest(message("порция 500 г"))
    assert (await db.totals(1, day_key(now_utc())))[0] == 180
    assert await db.draft(1, editing=True)
    await app.cancel(message("/cancel"))
    assert not await db.draft(1)
    assert len(await db.today_meals(1)) == 1
    assert (await db.totals(1, day_key(now_utc())))[0] == 180
    await app.close()


async def test_day_editor_lists_only_current_users_confirmed_meals(settings, db, meal):
    app = app_for(settings, db, meal)
    for uid in (1, 2):
        await db.user(uid)
        mid = await db.save_draft(uid, meal, "text")
        await db.confirm(uid, mid)
    own = (await db.today_meals(1))[0]
    other = (await db.today_meals(2))[0]
    await db.save_draft(1, meal, "text")  # Unconfirmed meal is absent from the list.
    await app.day_callback(day_query("list", day_key(now_utc())))
    markup = app.bot.edit_message_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"day:open:{own['id']}"
    assert len(markup.inline_keyboard) == 2
    app.bot.edit_message_text.reset_mock()
    rejected = day_query("edit", other["id"])
    await app.day_callback(rejected)
    assert rejected.answer.call_args.kwargs["show_alert"]
    app.bot.edit_message_text.assert_not_awaited()
