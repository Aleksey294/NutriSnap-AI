import asyncio
from datetime import timedelta

import pytest

from nutrisnap.db import Database
from nutrisnap.domain import MealDraft, QuotaError, ServiceError, day_key, now_utc


async def test_confirm_is_idempotent_and_checks_owner(db, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    assert await db.confirm(2, mid) is None
    results = await asyncio.gather(db.confirm(1, mid), db.confirm(1, mid))
    assert sum(r is not None for r in results) == 1
    assert await db.totals(1, day_key(now_utc())) == [180, 12, 8, 15]


async def test_edit_replaces_not_duplicates_and_costs_quota(db, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    changed = meal.model_copy(update={"kcal": 360})
    assert await db.save_draft(1, changed, "text", mid) == mid
    assert len(await db.all("SELECT * FROM meals")) == 1
    assert (await db.one("SELECT fair_count FROM users"))[0] == 2


async def test_quota_race_does_not_exceed_limit(db, settings, meal):
    settings.fair_use_daily_limit = 1
    await db.user(1)
    results = await asyncio.gather(
        *(db.save_draft(1, meal, "text") for _ in range(3)), return_exceptions=True
    )
    assert sum(isinstance(r, QuotaError) for r in results) == 2
    assert len(await db.all("SELECT * FROM meals")) == 1


async def test_global_api_budget_survives_user_deletion(db, settings):
    settings.ai_daily_request_limit = 1
    await db.user(1)
    await db.reserve_api()
    await db.delete_user(1)
    with pytest.raises(QuotaError):
        await db.reserve_api()


async def test_draft_survives_restart(db, settings, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "photo")
    await db.execute("UPDATE meals SET editing=1 WHERE id=?", (mid,))
    other = Database(settings)
    await other.open()
    try:
        assert (await other.draft(1, editing=True))["id"] == mid
    finally:
        await other.close()


async def test_delete_me_cascades(db, meal):
    await db.user(1)
    await db.save_draft(1, meal, "text")
    await db.execute("INSERT INTO dashboards VALUES(1,'2026-10-18',12)")
    await db.execute(
        "INSERT INTO weekly_reports VALUES(1,'2026-10-11','report',?,1)", (now_utc().isoformat(),)
    )
    await db.delete_user(1)
    for table in ("users", "meals", "dashboards", "weekly_reports"):
        assert await db.all(f"SELECT * FROM {table}") == []


async def test_retention_and_expired_drafts(db, meal):
    await db.user(1)
    old = (now_utc() - timedelta(days=91)).isoformat()
    mid = await db.save_draft(1, meal, "text")
    await db.execute("UPDATE meals SET expires_at=? WHERE id=?", (old, mid))
    assert await db.confirm(1, mid) is None
    assert await db.draft(1) is None
    await db.cleanup()
    assert await db.all("SELECT * FROM meals") == []
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    await db.execute("UPDATE meals SET eaten_at=? WHERE id=?", (old, mid))
    await db.cleanup()
    assert await db.all("SELECT * FROM meals") == []


async def test_confirmed_revision_replaces_original_atomically(db, meal):
    await db.user(1)
    original_id = await db.save_draft(1, meal, "photo")
    original = await db.confirm(1, original_id)
    revision_id = await db.begin_revision(1, original_id)
    assert (await db.draft(1, editing=True))["id"] == revision_id
    changed = meal.model_copy(update={"kcal": 360, "weight_g": 500})
    await db.save_draft(1, changed, "text", revision_id)
    assert (await db.totals(1, original["day_key"]))[0] == 180
    results = await asyncio.gather(db.confirm(1, revision_id), db.confirm(1, revision_id))
    assert sum(row is not None for row in results) == 1
    assert (await db.totals(1, original["day_key"]))[0] == 360
    rows = await db.today_meals(1)
    assert len(rows) == 1
    assert rows[0]["id"] == original_id
    assert rows[0]["eaten_at"] == original["eaten_at"]
    assert MealDraft.model_validate_json(rows[0]["payload"]).weight_g == 500
    assert (await db.one("SELECT fair_count FROM users"))[0] == 2


async def test_revision_rejects_other_owner_old_day_and_pending_draft(db, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    with pytest.raises(ServiceError):
        await db.begin_revision(2, mid)
    assert await db.remove_today_meal(2, mid) is None
    await db.execute("UPDATE meals SET day_key='2000-01-01' WHERE id=?", (mid,))
    with pytest.raises(ServiceError):
        await db.begin_revision(1, mid)
    assert await db.remove_today_meal(1, mid) is None
    await db.execute("UPDATE meals SET day_key=? WHERE id=?", (day_key(now_utc()), mid))
    pending = await db.save_draft(1, meal, "text")
    with pytest.raises(ServiceError, match="текущую карточку"):
        await db.begin_revision(1, mid)
    assert (await db.draft(1))["id"] == pending


async def test_deleted_original_cannot_be_resurrected_by_revision(db, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    revision = await db.begin_revision(1, mid)
    await db.save_draft(1, meal.model_copy(update={"kcal": 360}), "text", revision)
    assert await db.remove_today_meal(1, mid)
    assert await db.remove_today_meal(1, mid) is None
    assert await db.confirm(1, revision) is None
    assert await db.totals(1, day_key(now_utc())) == [0, 0, 0, 0]


async def test_revision_stops_at_day_boundary(db, meal, monkeypatch):
    from datetime import UTC, datetime

    at = datetime(2026, 10, 19, 0, 59, tzinfo=UTC)
    monkeypatch.setattr("nutrisnap.db.now_utc", lambda: at)
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    revision = await db.begin_revision(1, mid)
    at = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    assert await db.draft(1) is None
    assert await db.confirm(1, revision) is None
    assert (await db.totals(1, "2026-10-18"))[0] == 180


async def test_schema_upgrade_keeps_existing_meals(db, settings, meal):
    await db.user(1)
    mid = await db.save_draft(1, meal, "text")
    await db.confirm(1, mid)
    await db.execute("DROP TABLE meal_revisions")
    await db.execute("PRAGMA user_version=1")
    upgraded = Database(settings)
    await upgraded.open()
    try:
        assert (await upgraded.today_meals(1))[0]["id"] == mid
        assert await upgraded.begin_revision(1, mid)
        assert (await upgraded.one("PRAGMA user_version"))[0] == 2
    finally:
        await upgraded.close()
