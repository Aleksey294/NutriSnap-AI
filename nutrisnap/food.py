import io
import json
import re
import warnings
from datetime import timedelta

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import MealDraft, ServiceError, now_utc

MAX_PHOTO_BYTES = 5 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000
BARCODE_TEXT = re.compile(r"^\s*(\d{12,13}|\d{8})(?!\d)\s*(.*)$", re.DOTALL)


def valid_barcode(code):
    if not code.isascii() or not code.isdigit() or len(code) not in (8, 12, 13):
        return False
    total = sum(int(n) * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(code[:-1])))
    return (10 - total % 10) % 10 == int(code[-1])


def prepare_photo(data):
    if len(data) > MAX_PHOTO_BYTES:
        raise ServiceError("Фото слишком большое. Максимум — 5 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as raw:
                if raw.format not in ("JPEG", "PNG", "WEBP"):
                    raise ServiceError("Пришлите фото в JPEG, PNG или WEBP.")
                raw.load()
                img = ImageOps.exif_transpose(raw).convert("RGB")
                img.thumbnail((1280, 1280))
                # Pillow + native zbar: no OpenCV or local neural network.
                from pyzbar.pyzbar import ZBarSymbol, decode

                codes = decode(img, symbols=[ZBarSymbol.EAN13, ZBarSymbol.EAN8, ZBarSymbol.UPCA])
                barcode = next(
                    (c.data.decode("ascii") for c in codes if valid_barcode(c.data.decode("ascii"))), None
                )
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=85)
                return output.getvalue(), barcode
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ValueError,
    ):
        raise ServiceError("Не удалось открыть фото. Пришлите JPEG/PNG/WEBP до 5 МБ и 16 Мп.") from None


def portion(text):
    match = re.search(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(кг|kg|г|гр|g)\b", text, re.IGNORECASE)
    if not match:
        if text.strip():
            raise ServiceError("Для штрихкода укажите порцию в граммах, например: 200 г.")
        return 100.0, True
    # Reject extra ingredients rather than silently ignoring them.
    if (text[: match.start()] + text[match.end() :]).strip(" ,.;:-"):
        raise ServiceError(
            "Для штрихкода укажите только вес, например: 200 г. "
            "Составное блюдо опишите текстом без штрихкода."
        )
    weight = float(match[1].replace(",", ".")) * (1000 if match[2].lower() in ("kg", "кг") else 1)
    if not 0 < weight <= 10000:
        raise ServiceError("Укажите вес порции от 1 до 10000 г.")
    return weight, False


class FoodFacts:
    def __init__(self, db, http, user_agent):
        self.db, self.http, self.user_agent = db, http, user_agent

    async def lookup(self, code, text):
        if not valid_barcode(code):
            raise ServiceError("Штрихкод некорректен. Проверьте цифры или пришлите описание еды.")
        grams, estimated = portion(text)
        row = await self.db.one(
            "SELECT payload FROM barcode_cache WHERE barcode=? AND fetched_at>?",
            (code, (now_utc() - timedelta(days=30)).isoformat()),
        )
        if row:
            product = json.loads(row["payload"])
        else:
            try:
                response = await self.http.get(
                    f"https://world.openfoodfacts.org/api/v2/product/{code}.json",
                    params={"fields": "product_name,product_name_ru,nutriments"},
                    headers={"User-Agent": self.user_agent},
                    timeout=8,
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                data = response.json()
                if data.get("status") != 1:
                    return None
                p = data["product"]
                n = p["nutriments"]
                kcal = n.get("energy-kcal_100g")
                if kcal is None and n.get("energy_100g") is not None:
                    kcal = float(n["energy_100g"]) / 4.184
                product = {
                    "title": p.get("product_name_ru") or p.get("product_name") or "Продукт",
                    "kcal": float(kcal),
                    "protein_g": float(n["proteins_100g"]),
                    "fat_g": float(n["fat_100g"]),
                    "carbs_g": float(n["carbohydrates_100g"]),
                }
                MealDraft(**product, weight_g=100, weight_estimated=False, confidence=0.9)
                await self.db.execute(
                    "INSERT OR REPLACE INTO barcode_cache VALUES(?,?,?)",
                    (code, json.dumps(product), now_utc().isoformat()),
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return None
        try:
            return MealDraft(
                title=product["title"],
                weight_g=grams,
                weight_estimated=estimated,
                confidence=0.9,
                notes="Open Food Facts (ODbL). "
                + ("Принята порция 100 г — уточните вес." if estimated else "По данным этикетки."),
                **{
                    key: round(product[key] * grams / 100, 2)
                    for key in ("kcal", "protein_g", "fat_g", "carbs_g")
                },
            )
        except ValueError:
            raise ServiceError("Данные продукта некорректны. Пришлите описание еды текстом.") from None
