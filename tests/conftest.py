import pytest

from nutrisnap.config import Settings
from nutrisnap.db import Database
from nutrisnap.domain import MealDraft


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        bot_token="123456:testing",
        openrouter_api_key="test-key",
        database_path=tmp_path / "test.db",
        ai_min_interval_seconds=0,
    )


@pytest.fixture
async def db(settings):
    db = Database(settings)
    await db.open()
    yield db
    await db.close()


@pytest.fixture
def meal():
    return MealDraft(
        title="Борщ",
        weight_g=250,
        weight_estimated=True,
        kcal=180,
        protein_g=12,
        fat_g=8,
        carbs_g=15,
        confidence=0.7,
    )
