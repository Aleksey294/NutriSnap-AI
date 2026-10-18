import asyncio
import io
import json
import logging
from contextlib import asynccontextmanager

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import MealDraft, NotFood, ServiceError, card, dashboard, day_key, now_utc
from .food import BARCODE_TEXT, MAX_PHOTO_BYTES, prepare_photo

log = logging.getLogger(__name__)


class Admission:
    """Reject overload before downloading images; never accumulate an image queue."""

    def __init__(self, limit):
        self.limit, self.active = limit, 0

    @asynccontextmanager
    async def slot(self):
        if self.active >= self.limit:
            raise ServiceError("Сейчас бот занят. Повторите через несколько секунд.")
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


class UserLocks:
    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(256)]

    def get(self, uid):
        return self.locks[uid % len(self.locks)]


class AccessMiddleware(BaseMiddleware):
    def __init__(self, settings, locks, notice, clean):
        self.allowed, self.locks = settings.allowed_ids, locks
        self.notice, self.clean = notice, clean

    async def reply(self, event, text):
        if hasattr(event, "chat"):
            await self.notice(event, text)
        else:
            await event.answer(text)

    async def __call__(self, handler, event, data):
        uid = event.from_user.id
        message = event if hasattr(event, "chat") else event.message
        if not message or message.chat.type != "private":
            if hasattr(event, "data"):
                await event.answer("Бот работает в личных сообщениях.", show_alert=True)
            return
        if self.allowed and uid not in self.allowed:
            await self.reply(event, "Доступ к боту ограничен.")
            return
        lock = self.locks.get(uid)
        if lock.locked():
            await self.reply(event, "Предыдущий запрос ещё обрабатывается. Подождите немного.")
            return
        async with lock:
            try:
                return await handler(event, data)
            finally:
                if hasattr(event, "chat") and (event.text or "").startswith("/"):
                    await self.clean(uid, event.message_id)


def buttons(mid):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{mid}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{mid}"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{mid}")],
        ]
    )


class App:
    def __init__(self, settings, db, ai, facts, bot):
        self.settings, self.db, self.ai, self.facts, self.bot = settings, db, ai, facts, bot
        self.locks = UserLocks()
        self.admission = Admission(settings.max_concurrent_requests)
        self.cleanup_tasks = set()
        self.router = Router()
        middleware = AccessMiddleware(settings, self.locks, self.notice, self.clean)
        self.router.message.outer_middleware(middleware)
        self.router.callback_query.outer_middleware(middleware)
        self.router.message.register(self.start, Command("start", "help"))
        self.router.message.register(self.show_today, Command("today"))
        self.router.message.register(self.show_draft, Command("draft"))
        self.router.message.register(self.cancel, Command("cancel"))
        self.router.message.register(self.delete_me, Command("delete_me"))
        self.router.message.register(self.privacy, Command("privacy"))
        self.router.callback_query.register(self.day_callback, F.data.startswith("day:"))
        self.router.callback_query.register(self.callback, F.data.startswith(("accept:", "edit:", "cancel:")))
        self.router.message.register(self.ingest, F.photo | F.text | F.document)
        self.router.message.register(self.unsupported)

    async def clean(self, uid, message_id):
        if not message_id:
            return
        try:
            await self.bot.delete_message(uid, message_id)
        except TelegramAPIError:
            log.debug("clean_chat_failed")

    async def expire_notice(self, uid, message_ids, ttl):
        try:
            await asyncio.sleep(ttl)
        finally:
            for message_id in message_ids:
                await self.clean(uid, message_id)

    def cleanup_done(self, task):
        self.cleanup_tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.warning("notice_cleanup_failed error_type=%s", type(task.exception()).__name__)

    async def notice(self, message, text, *, ttl=15):
        sent = await message.answer(text)
        self.schedule_cleanup(message.from_user.id, (sent.message_id, message.message_id), ttl)

    def schedule_cleanup(self, uid, message_ids, ttl=15):
        task = asyncio.create_task(self.expire_notice(uid, message_ids, ttl))
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_done)

    async def hint(self, uid, text):
        sent = await self.bot.send_message(uid, text)
        self.schedule_cleanup(uid, (sent.message_id,))

    async def close(self):
        tasks = list(self.cleanup_tasks)
        # Let each coroutine enter its try/finally before cancelling its timer.
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self, message):
        await self.db.user(message.from_user.id)
        await message.answer(
            "Привет! Я NutriSnap — дневник питания. Пришлите фото еды, описание "
            "(например, «гречка 150 г и курица 100 г») или штрихкод с весом. "
            "Проверьте оценку и нажмите «Принять».\n\n"
            "/today — дневник\n/draft — последняя карточка\n/cancel — отменить черновик"
        )
        await self.update_dashboard(message.from_user.id)

    async def privacy(self, message):
        await message.answer(
            "Храним Telegram ID, общие нормы, оценки еды и недельные отчёты. "
            f"Записи и отчёты удаляются через {self.settings.meals_retention_days} дней, "
            "черновики действуют 24 часа. Фото обрабатываются в памяти сервера и передаются "
            "OpenRouter и его провайдеру; их хранение регулируется политиками этих сервисов. "
            "Штрихкоды проверяются в Open Food Facts. /delete_me удаляет данные из рабочей БД, "
            "но не копии у Telegram, AI-провайдеров и в ранее сделанных резервных копиях."
        )

    async def show_today(self, message):
        await self.db.user(message.from_user.id)
        await self.update_dashboard(message.from_user.id, refresh=True)

    async def update_dashboard(self, uid, key=None, *, refresh=False):
        key = key or day_key(now_utc())
        user = await self.db.one("SELECT * FROM users WHERE telegram_id=?", (uid,))
        if not user:
            return
        totals = await self.db.totals(uid, key)
        text = dashboard(totals, [user[x] for x in ("target_kcal", "target_p", "target_f", "target_c")], key)
        row = await self.db.one("SELECT message_id FROM dashboards WHERE user_id=? AND day_key=?", (uid, key))
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Изменить записи", callback_data=f"day:list:{key}")]
                ]
            )
            if key == day_key(now_utc())
            else None
        )
        if row and not refresh:
            try:
                await self.bot.edit_message_text(
                    text, chat_id=uid, message_id=row["message_id"], reply_markup=markup
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                if not any(
                    x in str(exc).lower() for x in ("message to edit not found", "message can't be edited")
                ):
                    raise
        sent = await self.bot.send_message(uid, text, reply_markup=markup)
        await self.db.execute("INSERT OR REPLACE INTO dashboards VALUES(?,?,?)", (uid, key, sent.message_id))
        if row and refresh:
            await self.clean(uid, row["message_id"])

    async def send_card(self, uid, mid, draft, old_card=None):
        revision = await self.db.one("SELECT original_id FROM meal_revisions WHERE draft_id=?", (mid,))
        text = card(draft)
        if revision:
            text = "<b>Правка сохранённой записи</b>\nИзменения применятся после «Принять».\n\n" + text
        sent = await self.bot.send_message(uid, text, reply_markup=buttons(mid))
        await self.db.execute(
            "UPDATE meals SET card_id=? WHERE id=? AND user_id=?", (sent.message_id, mid, uid)
        )
        await self.clean(uid, old_card)

    async def show_draft(self, message):
        uid = message.from_user.id
        row = await self.db.draft(uid)
        if not row:
            await self.notice(message, "Нет неподтверждённой карточки. Пришлите фото или описание еды.")
            return
        await self.send_card(uid, row["id"], MealDraft.model_validate_json(row["payload"]), row["card_id"])

    async def cancel(self, message):
        uid = message.from_user.id
        row = await self.db.draft(uid)
        if row:
            await self.db.execute("UPDATE meals SET status='cancelled',editing=0 WHERE id=?", (row["id"],))
            await self.clean(uid, row["card_id"])
        await self.notice(message, "Черновик отменён. Можно прислать новое блюдо.")

    async def delete_me(self, message):
        uid = message.from_user.id
        cards = await self.db.all("SELECT card_id FROM meals WHERE user_id=? AND card_id IS NOT NULL", (uid,))
        dashboards = await self.db.all("SELECT message_id FROM dashboards WHERE user_id=?", (uid,))
        await self.db.delete_user(uid)
        await message.answer(
            "Профиль, записи, черновики и отчёты удалены из базы бота. "
            "Для нового дневника отправьте /start. Историю чата можно удалить в Telegram."
        )
        # Bounded best-effort chat cleanup; database deletion is complete regardless.
        for row in (cards + dashboards)[-20:]:
            await self.clean(uid, row[0])

    async def callback(self, query):
        uid = query.from_user.id
        action, _, value = query.data.partition(":")
        if not value.isdigit():
            await query.answer("Некорректная карточка.")
            return
        mid = int(value)
        row = await self.db.draft(uid, mid)
        if not row:
            await query.answer("Карточка уже сохранена, отменена или устарела.")
            return
        if action == "accept":
            confirmed = await self.db.confirm(uid, mid)
            if not confirmed:
                await query.answer("Запись уже изменена, удалена или день завершён.", show_alert=True)
                return
            await query.answer("Сохранено")
            await self.update_dashboard(uid, confirmed["day_key"])
            await self.clean(uid, row["card_id"])
            return
        await query.answer()
        if action == "edit":
            await self.db.execute("UPDATE meals SET editing=1 WHERE id=? AND user_id=?", (mid, uid))
            await self.bot.send_message(
                uid,
                "Напишите правку, например: «порция 200 г, без масла». "
                "Фото для правки не требуется. /cancel — отменить карточку.",
            )
        elif action == "cancel":
            await self.db.execute(
                "UPDATE meals SET status='cancelled',editing=0 WHERE id=? AND user_id=?", (mid, uid)
            )
            await self.clean(uid, row["card_id"])

    async def editor(self, query, text, rows):
        try:
            await self.bot.edit_message_text(
                text,
                chat_id=query.from_user.id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def day_callback(self, query):
        uid = query.from_user.id
        parts = query.data.split(":")
        if len(parts) != 3:
            await query.answer("Некорректная кнопка.")
            return
        _, action, value = parts
        back = [InlineKeyboardButton(text="← К дневнику", callback_data="day:back:0")]
        if action == "back":
            await query.answer()
            await self.update_dashboard(uid)
            return
        if action in ("list", "page"):
            if action == "list" and value != day_key(now_utc()):
                await query.answer(
                    "Этот день завершён. Откройте сегодняшний дневник: /today.", show_alert=True
                )
                return
            if action == "page" and not value.isdigit():
                await query.answer("Некорректная страница.")
                return
            meals = await self.db.today_meals(uid)
            page = min(int(value) if action == "page" else 0, max(0, (len(meals) - 1) // 6))
            rows = []
            for index, row in enumerate(meals[page * 6 : page * 6 + 6], start=page * 6 + 1):
                meal = MealDraft.model_validate_json(row["payload"])
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{index}. {meal.title[:40]} · {meal.kcal:g} ккал",
                            callback_data=f"day:open:{row['id']}",
                        )
                    ]
                )
            nav = []
            if page:
                nav.append(InlineKeyboardButton(text="←", callback_data=f"day:page:{page - 1}"))
            if (page + 1) * 6 < len(meals):
                nav.append(InlineKeyboardButton(text="→", callback_data=f"day:page:{page + 1}"))
            if nav:
                rows.append(nav)
            rows.append(back)
            await query.answer()
            await self.editor(
                query,
                "<b>Записи за сегодня</b>\n"
                + (
                    "Выберите приём пищи, чтобы изменить или удалить его."
                    if meals
                    else "Пока нет принятых записей."
                ),
                rows,
            )
            return
        if action not in ("open", "edit", "remove", "delete") or not value.isdigit():
            await query.answer("Некорректная кнопка.")
            return
        mid = int(value)
        row = await self.db.confirmed_today(uid, mid)
        if not row:
            await query.answer("Запись удалена или день завершён. Откройте /today.", show_alert=True)
            return
        if action == "edit":
            try:
                draft_id = await self.db.begin_revision(uid, mid)
            except ServiceError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.answer()
            await self.update_dashboard(uid)
            await self.send_card(uid, draft_id, MealDraft.model_validate_json(row["payload"]))
            await self.hint(
                uid,
                "Напишите правку, например: «порция 200 г, без масла». "
                "Затем нажмите «Принять» в новой карточке. /cancel — отменить правку.",
            )
        elif action == "delete":
            revisions = await self.db.all(
                "SELECT card_id FROM meals JOIN meal_revisions ON draft_id=meals.id "
                "WHERE original_id=? AND user_id=? AND status='draft'",
                (mid, uid),
            )
            removed = await self.db.remove_today_meal(uid, mid)
            await query.answer("Запись удалена" if removed else "Запись уже удалена")
            await self.update_dashboard(uid)
            for revision in revisions:
                await self.clean(uid, revision["card_id"])
        else:
            await query.answer()
            if action == "open":
                rows = [
                    [
                        InlineKeyboardButton(text="✏️ Исправить", callback_data=f"day:edit:{mid}"),
                        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"day:remove:{mid}"),
                    ],
                    [
                        InlineKeyboardButton(
                            text="← К записям", callback_data=f"day:list:{day_key(now_utc())}"
                        )
                    ],
                ]
                text = "<b>Сохранённая запись</b>\n\n" + card(MealDraft.model_validate_json(row["payload"]))
            else:
                rows = [
                    [
                        InlineKeyboardButton(text="Да, удалить", callback_data=f"day:delete:{mid}"),
                        InlineKeyboardButton(text="Отмена", callback_data=f"day:open:{mid}"),
                    ]
                ]
                text = "Удалить этот приём пищи из сегодняшнего дневника?"
            await self.editor(query, text, rows)

    async def ingest(self, message):
        uid = message.from_user.id
        text = message.text or message.caption or ""
        if text.startswith("/"):
            await self.notice(message, "Неизвестная команда. /help — помощь.")
            return
        if len(text) > 1000:
            await self.notice(message, "Описание должно быть не длиннее 1000 символов.")
            return
        if not text.strip() and not message.photo and not message.document:
            return
        loading = None
        try:
            async with self.admission.slot():
                await self.db.user(uid)
                await self.db.check_quota(uid)
                previous = await self.db.draft(uid, editing=True)
                photo, barcode = None, None
                source = "text"
                media = message.photo[-1] if message.photo else message.document
                if media and previous:
                    raise ServiceError(
                        "Сейчас ожидается текстовая правка. /cancel — отменить и отправить новое фото."
                    )
                if media:
                    if message.document and message.document.mime_type not in (
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                    ):
                        raise ServiceError("Поддерживаются фото JPEG, PNG и WEBP.")
                    if not media.file_size or media.file_size > MAX_PHOTO_BYTES:
                        raise ServiceError("Фото должно быть не больше 5 МБ.")
                loading = await message.answer("Распознаю…")
                if media:
                    buffer = io.BytesIO()
                    await self.bot.download(media.file_id, destination=buffer, timeout=20)
                    photo, barcode = await asyncio.to_thread(prepare_photo, buffer.getvalue())
                    buffer.close()
                    source = "photo_caption" if text else "photo"
                match = BARCODE_TEXT.fullmatch(text) if not photo and not previous else None
                portion_text = text
                if match:
                    barcode, portion_text = match[1], match[2]
                if barcode:
                    draft = await self.facts.lookup(barcode, portion_text)
                    if draft is None:
                        raise ServiceError(
                            "Штрихкод найден, но данных КБЖУ нет. "
                            "Сфотографируйте название крупно или опишите продукт текстом."
                        )
                    source = "barcode"
                else:
                    draft = await self.ai.estimate(
                        text, photo, json.loads(previous["payload"]) if previous else None
                    )
                old = await self.db.draft(uid)
                mid = await self.db.save_draft(uid, draft, source, previous["id"] if previous else None)
                await self.send_card(uid, mid, draft, old["card_id"] if old else None)
                await self.clean(uid, message.message_id)
        except NotFood:
            await self.notice(message, "Я помогаю только с едой и КБЖУ.")
        except ServiceError as exc:
            await self.notice(message, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep polling, never log private request data
            log.error("ingest_failed error_type=%s", type(exc).__name__)
            await self.notice(
                message,
                "Не удалось обработать запрос. Попробуйте позже. "
                "Если карточка не появилась, проверьте /draft.",
            )
        finally:
            if loading:
                await self.clean(uid, loading.message_id)

    async def unsupported(self, message):
        await self.notice(
            message, "Пришлите фото еды, текст или штрихкод. Голос и видео пока не поддерживаются."
        )
