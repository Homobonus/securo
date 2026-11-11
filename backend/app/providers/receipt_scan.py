"""Read a photo of a paper receipt with a vision model.

One request to an OpenAI-compatible `/chat/completions`: the photo goes
in as a data URL, the answer comes back constrained by a JSON schema, so
it parses or fails, never half-parses. The provider only reads; checking
the sum and turning the draft into a receipt is the service's job.

Nothing is sent anywhere unless the deploy set `OPENAI_API_KEY` (or
pointed `OPENAI_API_URL` at its own server).
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.agents.providers.openai import normalize_openai_base_url
from app.core.config import get_settings

OPENAI_DEFAULT_URL = "https://api.openai.com/v1"

#: Longest side of the image that is kept and sent. Receipts are long and
#: narrow; at this size a phone photo of a 40-line receipt stays legible
#: while the upload shrinks from megabytes to a few hundred kilobytes.
MAX_IMAGE_SIDE = 2400
JPEG_QUALITY = 85


class ReceiptScanError(Exception):
    """Reading failed. `code` is stable and shown translated in the app."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass
class ScanSettings:
    api_url: str
    api_key: str
    model: str
    timeout: float

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.api_url.rstrip("/") != OPENAI_DEFAULT_URL


def scan_settings() -> ScanSettings:
    settings = get_settings()
    return ScanSettings(
        api_url=normalize_openai_base_url(settings.openai_api_url.strip()) or OPENAI_DEFAULT_URL,
        api_key=settings.openai_api_key.get_secret_value().strip(),
        model=settings.receipt_scan_model.strip() or "gpt-4.1-mini",
        timeout=float(settings.receipt_scan_timeout_seconds),
    )


def prepare_image(data: bytes) -> bytes:
    """Upright, RGB, at most MAX_IMAGE_SIDE, as JPEG.

    Phones store the camera's orientation in EXIF instead of rotating the
    pixels; a model reading a sideways receipt reads it badly.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source) or source
            image = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ReceiptScanError("unsupported_image") from exc
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


_NUMBER = {"type": "number"}
_NULLABLE_STRING = {"type": ["string", "null"]}

RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_receipt",
        "store_name",
        "store_address",
        "tax_id",
        "purchased_at",
        "currency",
        "total",
        "items",
    ],
    "properties": {
        "is_receipt": {"type": "boolean"},
        "store_name": _NULLABLE_STRING,
        "store_address": _NULLABLE_STRING,
        "tax_id": _NULLABLE_STRING,
        "purchased_at": {
            "type": ["string", "null"],
            "description": "Local date and time of the sale, YYYY-MM-DDTHH:MM, or YYYY-MM-DD without a time.",
        },
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. PLN."},
        "total": {
            "type": ["number", "null"],
            "description": "The amount paid, as printed next to SUMA / TOTAL.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "quantity", "unit", "unit_price", "total", "discount"],
                "properties": {
                    "name": {"type": "string"},
                    "quantity": _NUMBER,
                    "unit": {
                        "type": ["string", "null"],
                        "description": "kg, l or similar for weighed goods; null for pieces.",
                    },
                    "unit_price": _NUMBER,
                    "total": {
                        "type": "number",
                        "description": "Line value before any discount: quantity x unit_price.",
                    },
                    "discount": {
                        "type": "number",
                        "description": "Positive amount taken off this line, 0 if none.",
                    },
                },
            },
        },
    },
}

PROMPT = """You read photos of shop receipts (mostly Polish fiscal receipts, "PARAGON FISKALNY").
Return every purchased line exactly once, in printed order.

Rules:
- Keep product names exactly as printed (same spelling, abbreviations and case); do not translate or expand them.
- quantity x unit_price = total, where total is the line value BEFORE discounts. Weighed goods: quantity in kg with unit "kg".
- A discount line printed under a product ("Rabat", "Upust", "Obniżka", "Promocja", "Lidl Plus", a negative amount) is not an item:
  add its absolute value to that product's "discount".
- Deposits ("Kaucja", bottle deposits) are their own items.
- Skip VAT/PTU summaries, "Sprzedaż opodatkowana", payment lines (card, cash, change), loyalty points and footer text.
- total (top level) is the amount actually paid ("SUMA PLN" / "DO ZAPŁATY").
- store_name: the shop's brand as a customer would say it (e.g. "Żabka", "Carrefour", "Rossmann"), not the legal company name, when the brand is visible;
  otherwise the company name as printed. store_address: the shop's address, not the company's head office, if both are printed.
- tax_id: the seller's NIP as printed, digits only.
- Use numbers with a dot as the decimal separator. If a value is unreadable, give your best reading rather than leaving the line out.
- If the photo is not a receipt, set is_receipt to false and return an empty item list."""


def _message(image: bytes) -> list[dict[str, Any]]:
    encoded = base64.b64encode(image).decode("ascii")
    return [
        {"role": "system", "content": PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read this receipt."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"},
                },
            ],
        },
    ]


async def extract(
    image: bytes,
    *,
    config: Optional[ScanSettings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    """Send one prepared JPEG; return the receipt as the schema describes it."""
    config = config or scan_settings()
    if not config.enabled:
        raise ReceiptScanError("not_configured")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    body = {
        "model": config.model,
        "messages": _message(image),
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "receipt", "strict": True, "schema": RECEIPT_SCHEMA},
        },
        "max_completion_tokens": 8000,
    }
    try:
        async with httpx.AsyncClient(timeout=config.timeout, transport=transport) as client:
            response = await client.post(f"{config.api_url}/chat/completions", json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise ReceiptScanError("timeout") from exc
    except httpx.HTTPError as exc:
        raise ReceiptScanError("unreachable", str(exc)[:200]) from exc

    if response.status_code in (401, 403):
        raise ReceiptScanError("auth")
    if response.status_code == 429:
        # OpenAI answers 429 both for "slow down" and for an empty balance.
        code = "quota" if "insufficient_quota" in response.text else "rate_limited"
        raise ReceiptScanError(code)
    if response.status_code >= 400:
        raise ReceiptScanError("provider_error", f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        choice = response.json()["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ReceiptScanError("bad_response") from exc
    if message.get("refusal"):
        raise ReceiptScanError("refused", str(message["refusal"])[:200])
    if choice.get("finish_reason") == "length":
        raise ReceiptScanError("too_long")
    try:
        parsed = json.loads(message.get("content") or "")
    except ValueError as exc:
        raise ReceiptScanError("bad_response") from exc
    if not isinstance(parsed, dict):
        raise ReceiptScanError("bad_response")
    if parsed.get("is_receipt") is False:
        raise ReceiptScanError("not_a_receipt")
    return parsed
