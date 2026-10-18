import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import aiosqlite

from .domain import MACROS, MealDraft, QuotaError, ServiceError, calendar_key, day_key, now_utc


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()

    async def open(self):
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.settings.database_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.execute("PRAGMA secure_delete=ON")
        await self.conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        await self.conn.commit()

    async def close(self):
        await self.conn.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def one(self, sql, args=()):
        async with self.transaction() as c, c.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def all(self, sql, args=()):
        async with self.transaction() as c, c.execute(sql, args) as cursor:
            return await cursor.fetchall()

    async def execute(self, sql, args=()):
        async with self.transaction() as c:
            cursor = await c.execute(sql, args)
            return cursor.lastrowid

    async def user(self, uid):
        s = self.settings
        await self.execute(
            "INSERT OR IGNORE INTO users(telegram_id,created_at,target_kcal,target_p,target_f,target_c) "
            "VALUES (?,?,?,?,?,?)",
            (
                uid,
                now_utc().isoformat(),
                s.default_target_kcal,
                s.default_target_p,
                s.default_target_f,
                s.default_target_c,
            ),
        )
        return await self.one("SELECT * FROM users WHERE telegram_id=?", (uid,))

    async def check_quota(self, uid):
        row = await self.one("SELECT * FROM users WHERE telegram_id=?", (uid,))
        if (
            row
            and row["fair_date"] == calendar_key(now_utc())
            and (row["fair_count"] >= self.settings.fair_use_daily_limit)
        ):
            raise QuotaError("Дневной лимит распознаваний исчерпан. Завтра снова!")

    async def save_draft(self, uid, draft: MealDraft, source, replace_id=None):
        at = now_utc()
        today = calendar_key(at)
        async with self.transaction() as c:
            cursor = await c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,))
            user = await cursor.fetchone()
            if not user:
                raise ValueError("Unknown user")
            count = user["fair_count"] if user["fair_date"] == today else 0
            if count >= self.settings.fair_use_daily_limit:
                raise QuotaError("Дневной лимит распознаваний исчерпан. Завтра снова!")
            await c.execute(
                "UPDATE users SET fair_date=?,fair_count=? WHERE telegram_id=?", (today, count + 1, uid)
            )
            if replace_id:
                cursor = await c.execute(
                    "UPDATE meals SET payload=?,editing=0,expires_at=? "
                    "WHERE id=? AND user_id=? AND status='draft' AND expires_at>?",
                    (
                        draft.model_dump_json(),
                        (at + timedelta(hours=24)).isoformat(),
                        replace_id,
                        uid,
                        at.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Expired draft")
                return replace_id
            await c.execute(
                "UPDATE meals SET status='cancelled',editing=0 WHERE user_id=? AND status='draft'", (uid,)
            )
            cursor = await c.execute(
                "INSERT INTO meals(user_id,eaten_at,day_key,source,payload,status,expires_at) "
                "VALUES(?,?,?,?,?,'draft',?)",
                (
                    uid,
                    at.isoformat(),
                    day_key(at),
                    source,
                    draft.model_dump_json(),
                    (at + timedelta(hours=24)).isoformat(),
                ),
            )
            return cursor.lastrowid

    async def draft(self, uid, meal_id=None, editing=False):
        sql = "SELECT * FROM meals WHERE user_id=? AND status='draft' AND expires_at>?"
        args = [uid, now_utc().isoformat()]
        sql += " AND (NOT EXISTS (SELECT 1 FROM meal_revisions WHERE draft_id=meals.id) OR day_key=?)"
        args.append(day_key(now_utc()))
        if meal_id is not None:
            sql += " AND id=?"
            args.append(meal_id)
        if editing:
            sql += " AND editing=1"
        return await self.one(sql + " ORDER BY id DESC LIMIT 1", args)

    async def confirm(self, uid, meal_id):
        async with self.transaction() as c:
            cursor = await c.execute(
                "SELECT * FROM meals WHERE id=? AND user_id=? AND status='draft' AND expires_at>?",
                (meal_id, uid, now_utc().isoformat()),
            )
            row = await cursor.fetchone()
            if row:
                cursor = await c.execute(
                    "SELECT original_id FROM meal_revisions WHERE draft_id=?", (meal_id,)
                )
                revision = await cursor.fetchone()
                if revision:
                    cursor = await c.execute(
                        "SELECT * FROM meals WHERE id=? AND user_id=? AND status='confirmed' AND day_key=?",
                        (revision["original_id"], uid, day_key(now_utc())),
                    )
                    original = await cursor.fetchone()
                    await c.execute("UPDATE meals SET status='cancelled',editing=0 WHERE id=?", (meal_id,))
                    if original:
                        await c.execute(
                            "UPDATE meals SET payload=? WHERE id=?", (row["payload"], original["id"])
                        )
                    return original
                await c.execute("UPDATE meals SET status='confirmed',editing=0 WHERE id=?", (meal_id,))
            return row

    async def today_meals(self, uid):
        return await self.all(
            "SELECT * FROM meals WHERE user_id=? AND day_key=? AND status='confirmed' ORDER BY eaten_at,id",
            (uid, day_key(now_utc())),
        )

    async def confirmed_today(self, uid, meal_id):
        return await self.one(
            "SELECT * FROM meals WHERE id=? AND user_id=? AND day_key=? AND status='confirmed'",
            (meal_id, uid, day_key(now_utc())),
        )

    async def begin_revision(self, uid, meal_id):
        at = now_utc()
        async with self.transaction() as c:
            cursor = await c.execute(
                "SELECT * FROM meals WHERE id=? AND user_id=? AND day_key=? AND status='confirmed'",
                (meal_id, uid, day_key(at)),
            )
            original = await cursor.fetchone()
            if not original:
                raise ServiceError("Запись недоступна. Менять можно только свои записи за сегодня.")
            cursor = await c.execute(
                "SELECT id FROM meals WHERE user_id=? AND status='draft' AND expires_at>? "
                "AND (NOT EXISTS (SELECT 1 FROM meal_revisions WHERE draft_id=meals.id) OR day_key=?)",
                (uid, at.isoformat(), day_key(at)),
            )
            if await cursor.fetchone():
                raise ServiceError("Сначала примите или отмените текущую карточку: /draft или /cancel.")
            await c.execute(
                "UPDATE meals SET status='cancelled',editing=0 WHERE user_id=? AND status='draft'", (uid,)
            )
            cursor = await c.execute(
                "INSERT INTO meals(user_id,eaten_at,day_key,source,payload,status,editing,expires_at) "
                "VALUES(?,?,?,?,?,'draft',1,?)",
                (
                    uid,
                    original["eaten_at"],
                    original["day_key"],
                    original["source"],
                    original["payload"],
                    (at + timedelta(hours=24)).isoformat(),
                ),
            )
            draft_id = cursor.lastrowid
            await c.execute("INSERT INTO meal_revisions VALUES(?,?)", (draft_id, meal_id))
            return draft_id

    async def remove_today_meal(self, uid, meal_id):
        async with self.transaction() as c:
            cursor = await c.execute(
                "SELECT * FROM meals WHERE id=? AND user_id=? AND day_key=? AND status='confirmed'",
                (meal_id, uid, day_key(now_utc())),
            )
            original = await cursor.fetchone()
            if not original:
                return None
            await c.execute(
                "UPDATE meals SET status='cancelled',editing=0 WHERE id=? OR id IN "
                "(SELECT draft_id FROM meal_revisions WHERE original_id=?)",
                (meal_id, meal_id),
            )
            return original

    async def reserve_api(self):
        today = now_utc().date().isoformat()  # OpenRouter's daily budget uses UTC.
        async with self.transaction() as c:
            await c.execute("INSERT OR IGNORE INTO api_usage VALUES(?,0)", (today,))
            cursor = await c.execute(
                "UPDATE api_usage SET count=count+1 WHERE day=? AND count<?",
                (today, self.settings.ai_daily_request_limit),
            )
            if cursor.rowcount != 1:
                raise QuotaError("Общий дневной лимит AI исчерпан. Попробуйте завтра или введите штрихкод.")

    async def totals(self, uid, key):
        rows = await self.all(
            "SELECT payload FROM meals WHERE user_id=? AND day_key=? AND status='confirmed'", (uid, key)
        )
        payloads = [json.loads(r["payload"]) for r in rows]
        return [sum(p[k] for p in payloads) for k in MACROS]

    async def cleanup(self):
        at = now_utc()
        cutoff = (at - timedelta(days=self.settings.meals_retention_days)).isoformat()
        async with self.transaction() as c:
            await c.execute(
                "DELETE FROM meals WHERE eaten_at<? OR (status!='confirmed' AND expires_at<?)",
                (cutoff, at.isoformat()),
            )
            await c.execute("DELETE FROM weekly_reports WHERE created_at<?", (cutoff,))
            await c.execute("DELETE FROM dashboards WHERE day_key<?", (cutoff[:10],))
            await c.execute(
                "DELETE FROM barcode_cache WHERE fetched_at<?", ((at - timedelta(days=30)).isoformat(),)
            )
            await c.execute("DELETE FROM api_usage WHERE day<?", (cutoff[:10],))

    async def delete_user(self, uid):
        await self.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
