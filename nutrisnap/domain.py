import json
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

MSK = ZoneInfo("Europe/Moscow")
MACROS = ("kcal", "protein_g", "fat_g", "carbs_g")


def now_utc() -> datetime:
    return datetime.now(UTC)


def day_key(at: datetime) -> str:
    return (at.astimezone(MSK) - timedelta(hours=4)).date().isoformat()


def calendar_key(at: datetime) -> str:
    return at.astimezone(MSK).date().isoformat()


class MealDraft(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=160)
    weight_g: float = Field(gt=0, le=10000)
    weight_estimated: bool
    kcal: float = Field(ge=0, le=30000)
    protein_g: float = Field(ge=0, le=3000)
    fat_g: float = Field(ge=0, le=3000)
    carbs_g: float = Field(ge=0, le=10000)
    confidence: float = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=500)


class NotFood(Exception):
    pass


class ServiceError(Exception):
    pass


class QuotaError(ServiceError):
    pass


def parse_draft(content: str) -> MealDraft:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(text)
    if isinstance(data, dict) and data.get("not_food") is True:
        raise NotFood
    return MealDraft.model_validate(data)


def card(draft: MealDraft) -> str:
    estimate = "примерно " if draft.weight_estimated else ""
    return (
        f"<b>{escape(draft.title)}</b>\n"
        f"Порция: {estimate}{draft.weight_g:g} г\n"
        f"🔥 {draft.kcal:g} ккал\n"
        f"Б {draft.protein_g:g} г · Ж {draft.fat_g:g} г · У {draft.carbs_g:g} г\n\n"
        f"{escape(draft.notes)}\n"
        "Оценка приблизительная. Проверьте порцию и состав перед сохранением."
    )


def dashboard(totals: list[float], targets: list[float], key: str) -> str:
    lines = []
    for label, value, target in zip(("Ккал ", "Белки", "Жиры ", "Углев"), totals, targets):
        count = min(12, max(0, round(value / target * 12)))
        lines.append(f"{label} [{'█' * count}{'░' * (12 - count)}] {value:.0f} / {target:.0f}")
    return (
        f"<b>Дневник · {key}</b>\n<pre>"
        + "\n".join(lines)
        + (
            "</pre>\nКалории — ккал, Б/Ж/У — граммы. Новый день в 04:00 МСК.\n"
            "Нормы общие, не персональная рекомендация.\nПришлите фото, описание еды или штрихкод."
        )
    )
