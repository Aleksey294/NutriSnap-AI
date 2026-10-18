import io
import json

import httpx
import pytest
from PIL import Image

from nutrisnap.ai import AIClient
from nutrisnap.domain import ServiceError
from nutrisnap.food import MAX_PHOTO_BYTES, FoodFacts, prepare_photo


async def test_json_repair_and_vision_request(db, settings, meal):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "invalid" if len(calls) == 1 else meal.model_dump_json()}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await AIClient(settings, db, http).estimate("борщ", b"fake-jpeg")
    assert result == meal
    assert len(calls) == 2
    assert calls[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert all(call["reasoning"] == {"enabled": False} for call in calls)
    assert (await db.one("SELECT count FROM api_usage"))[0] == 2


@pytest.mark.parametrize("status", [401, 402, 429, 500, 503])
async def test_api_errors_no_retry_storm(db, settings, status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as http:
        with pytest.raises(ServiceError):
            await AIClient(settings, db, http).estimate("каша")
    assert (await db.one("SELECT count FROM api_usage"))[0] == 1


async def test_timeout_is_friendly(db, settings):
    def timeout(_):
        raise httpx.ReadTimeout("test")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(ServiceError):
            await AIClient(settings, db, http).estimate("каша")


async def test_empty_response_after_reasoning_budget_is_identified(db, settings, caplog):
    response = {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "private"}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        with pytest.raises(ServiceError, match="лимит ответа"):
            await AIClient(settings, db, http).estimate("каша")
    assert "OUTPUT_BUDGET_EXHAUSTED" in caplog.text
    assert "private" not in caplog.text


async def test_provider_error_inside_http_200_is_identified(db, settings, caplog):
    response = {"error": {"code": 403, "message": "secret-provider-details"}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        with pytest.raises(ServiceError, match="запретил"):
            await AIClient(settings, db, http).estimate("каша")
    assert "HTTP_403" in caplog.text
    assert "secret-provider-details" not in caplog.text


async def test_connection_error_is_distinct_from_recognition(db, settings, caplog):
    def offline(_):
        raise httpx.ConnectError("secret-connection-details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
        with pytest.raises(ServiceError, match="подключиться"):
            await AIClient(settings, db, http).estimate("каша")
    assert "CONNECTION_ERROR" in caplog.text
    assert "secret-connection-details" not in caplog.text


async def test_off_cache_and_portion(db):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": 1,
                "product": {
                    "product_name": "Продукт",
                    "nutriments": {
                        "energy-kcal_100g": 100,
                        "proteins_100g": 5,
                        "fat_100g": 2,
                        "carbohydrates_100g": 10,
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        facts = FoodFacts(db, http, "test")
        assert (await facts.lookup("3017620422003", "200 г")).kcal == 200
        assert (await facts.lookup("3017620422003", "50 г")).protein_g == 2.5
    assert len(calls) == 1


def test_photo_validation():
    with pytest.raises(ServiceError):
        prepare_photo(b"x" * (MAX_PHOTO_BYTES + 1))
    with pytest.raises(ServiceError):
        prepare_photo(b"invalid")


def test_photo_resize_in_memory():
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1000)).save(buffer, "PNG")
    data, barcode = prepare_photo(buffer.getvalue())
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (1280, 640)
    assert barcode is None
