from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from nutrisnap.bot import App


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sent_messages = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            result = Message.model_validate(
                {
                    "message_id": len(self.calls) + 100,
                    "date": 1,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
            self.sent_messages.append(result)
            return result
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


async def test_real_dispatcher_routes_messages_and_callbacks(settings, db, meal):
    session = TelegramSession()
    bot = Bot("123456:fake-token", session=session)
    app = App(settings, db, SimpleNamespace(estimate=AsyncMock(return_value=meal)), None, bot)
    dp = Dispatcher(disable_fsm=True)
    dp.include_router(app.router)
    user = {"id": 1, "is_bot": False, "first_name": "Test"}
    message = {"message_id": 1, "date": 1, "chat": {"id": 1, "type": "private"}, "from": user, "text": "борщ"}
    await dp.feed_update(bot, Update.model_validate({"update_id": 1, "message": message}))
    draft = await db.draft(1)
    assert draft is not None
    await dp.feed_update(
        bot,
        Update.model_validate(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb1",
                    "from": user,
                    "chat_instance": "ci",
                    "message": message,
                    "data": f"accept:{draft['id']}",
                },
            }
        ),
    )
    assert await db.draft(1) is None
    assert (await db.one("SELECT status FROM meals"))[0] == "confirmed"
    # Group messages must never create profiles or invoke AI.
    message["chat"] = {"id": -123, "type": "group", "title": "Group"}
    message["from"] = {**user, "id": 2}
    await dp.feed_update(bot, Update.model_validate({"update_id": 3, "message": message}))
    assert not await db.one("SELECT * FROM users WHERE telegram_id=2")
    assert app.ai.estimate.await_count == 1
    await bot.session.close()


async def test_today_reappears_at_bottom_even_when_totals_are_unchanged(settings, db):
    session = TelegramSession()
    bot = Bot("123456:fake-token", session=session)
    app = App(settings, db, None, None, bot)
    dp = Dispatcher(disable_fsm=True)
    dp.include_router(app.router)
    try:
        for update_id in (1, 2):
            await dp.feed_update(
                bot,
                Update.model_validate(
                    {
                        "update_id": update_id,
                        "message": {
                            "message_id": update_id,
                            "date": 1,
                            "chat": {"id": 1, "type": "private"},
                            "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                            "text": "/today",
                            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
                        },
                    }
                ),
            )
        sends = [call for call in session.calls if isinstance(call, SendMessage)]
        assert len(sends) == 2
        assert sends[0].text == sends[1].text
        rows = await db.all("SELECT * FROM dashboards WHERE user_id=1")
        assert len(rows) == 1
        assert rows[0]["message_id"] == session.sent_messages[-1].message_id
        deleted = [call.message_id for call in session.calls if call.__api_method__ == "deleteMessage"]
        assert session.sent_messages[0].message_id in deleted
        assert 1 in deleted and 2 in deleted  # The /today commands disappear too.
        assert session.sent_messages[-1].message_id not in deleted
    finally:
        await bot.session.close()
