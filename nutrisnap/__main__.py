import asyncio
import logging
from contextlib import suppress

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .ai import AIClient
from .bot import App
from .config import Settings
from .db import Database
from .food import FoodFacts
from .jobs import run_jobs


async def main():
    settings = Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Fail early if the native barcode dependency is missing.
    from pyzbar.pyzbar import decode  # noqa: F401

    db = Database(settings)
    await db.open()
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML"))
    try:
        async with httpx.AsyncClient(
            timeout=settings.ai_timeout_seconds,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as http:
            app = App(
                settings, db, AIClient(settings, db, http), FoodFacts(db, http, settings.off_user_agent), bot
            )
            dp = Dispatcher(disable_fsm=True)
            dp.include_router(app.router)

            @dp.errors()
            async def on_error(event):
                logging.getLogger(__name__).error(
                    "update_failed error_type=%s", type(event.exception).__name__
                )
                return True

            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать и помощь"),
                    BotCommand(command="today", description="Дневник дня"),
                    BotCommand(command="draft", description="Последняя карточка"),
                    BotCommand(command="cancel", description="Отменить черновик"),
                ]
            )
            await bot.delete_webhook(drop_pending_updates=False)
            jobs = asyncio.create_task(run_jobs(app))
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=["message", "callback_query"],
                    tasks_concurrency_limit=16,
                    close_bot_session=False,
                )
            finally:
                jobs.cancel()
                with suppress(asyncio.CancelledError):
                    await jobs
                await app.close()
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
