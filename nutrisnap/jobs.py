import asyncio
import json
import logging
from datetime import timedelta

from .domain import MACROS, MSK, now_utc

log = logging.getLogger(__name__)


async def weekly_reports(app, at=None):
    at = at or now_utc()
    local = at.astimezone(MSK)
    if local.weekday() != 6 or local.hour < 20:
        return
    # Fixed Sunday 20:00 window: retries never change the report's input period.
    end = local.replace(hour=20, minute=0, second=0, microsecond=0).astimezone(at.tzinfo)
    start = end - timedelta(days=7)
    key = start.astimezone(MSK).date().isoformat()
    users = await app.db.all(
        "SELECT DISTINCT user_id FROM meals WHERE status='confirmed' AND eaten_at>=? AND eaten_at<?",
        (start.isoformat(), end.isoformat()),
    )
    for user in users:
        uid = user["user_id"]
        if app.settings.allowed_ids and uid not in app.settings.allowed_ids:
            continue
        if app.locks.get(uid).locked():
            continue
        try:
            async with app.locks.get(uid), app.admission.slot():
                if not await app.db.one("SELECT telegram_id FROM users WHERE telegram_id=?", (uid,)):
                    continue
                report = await app.db.one(
                    "SELECT * FROM weekly_reports WHERE user_id=? AND week_start=?", (uid, key)
                )
                if report and report["sent"]:
                    continue
                if report:
                    body = report["body"]
                else:
                    rows = await app.db.all(
                        "SELECT day_key,payload FROM meals WHERE user_id=? AND status='confirmed' "
                        "AND eaten_at>=? AND eaten_at<?",
                        (uid, start.isoformat(), end.isoformat()),
                    )
                    daily = {}
                    for row in rows:
                        values = daily.setdefault(row["day_key"], dict.fromkeys(MACROS, 0))
                        payload = json.loads(row["payload"])
                        for macro in MACROS:
                            values[macro] += payload[macro]
                    body = await app.ai.report(
                        {
                            "period_start": start.isoformat(),
                            "period_end": end.isoformat(),
                            "daily_totals": daily,
                            "logged_meals": len(rows),
                        }
                    )
                    await app.db.execute(
                        "INSERT OR IGNORE INTO weekly_reports VALUES(?,?,?,?,0)",
                        (uid, key, body, at.isoformat()),
                    )
                await app.bot.send_message(uid, "Недельный отчёт\n\n" + body, parse_mode=None)
                await app.db.execute(
                    "UPDATE weekly_reports SET sent=1 WHERE user_id=? AND week_start=?", (uid, key)
                )
        except Exception as exc:  # noqa: BLE001 - isolate each recipient
            log.warning("weekly_report_failed error_type=%s", type(exc).__name__)
        finally:
            app.settings.database_path.with_suffix(".heartbeat").touch()


async def run_jobs(app):
    last_cleanup, last_report = None, None
    while True:
        try:
            at = now_utc()
            if last_cleanup != at.date():
                await app.db.cleanup()
                last_cleanup = at.date()
            # Retry failed delivery/generation every 15 minutes on Sunday evening.
            bucket = int(at.timestamp()) // 900
            if bucket != last_report:
                await weekly_reports(app, at)
                last_report = bucket
            # Liveness probe without a public HTTP server.
            app.settings.database_path.with_suffix(".heartbeat").touch()
        except Exception as exc:  # noqa: BLE001 - retry periodic jobs after transient failures
            log.error("scheduler_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(30)
