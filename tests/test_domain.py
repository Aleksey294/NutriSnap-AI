from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nutrisnap.config import Settings
from nutrisnap.domain import NotFood, calendar_key, card, day_key, parse_draft
from nutrisnap.food import portion, valid_barcode


def test_day_boundary_and_quota_day_are_different():
    before = datetime(2026, 10, 18, 0, 59, tzinfo=UTC)
    after = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    assert day_key(before) == "2026-10-17"
    assert day_key(after) == "2026-10-18"
    assert calendar_key(before) == "2026-10-18"


def test_reject_non_food():
    with pytest.raises(NotFood):
        parse_draft('{"not_food":true}')


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_bad_macros(meal, value):
    data = meal.model_dump()
    data["kcal"] = value
    with pytest.raises(ValidationError):
        type(meal).model_validate(data)


def test_model_html_is_escaped(meal):
    meal.title = "<a href='x'>еда</a>"
    assert "&lt;a" in card(meal)


def test_fenced_json(meal):
    assert parse_draft("```json\n" + meal.model_dump_json() + "\n```") == meal


def test_paid_model_requires_explicit_config():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bot_token="test", openrouter_api_key="test", openrouter_model="google/paid")


def test_portion_and_checksum():
    assert valid_barcode("3017620422003")
    assert not valid_barcode("3017620422004")
    assert portion("0,2 кг") == (200, False)
    assert portion("") == (100, True)
