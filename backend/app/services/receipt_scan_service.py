"""Receipt photos: from a picture to a reviewed receipt.

  1. The photo is stored (upright, shrunk, JPEG) and a scan row is opened.
  2. A vision model reads it into a draft (`app.providers.receipt_scan`).
  3. A person corrects the draft and accepts it; only then does it become
     a `Receipt` with source `scan`, its lines feeding shopping products
     like any store's, and it is paired with the bank debit that paid.

The scan keeps the photo after acceptance, so the receipt can show it.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_timezone
from app.models.receipt import Receipt, ReceiptScan
from app.providers import get_storage_provider
from app.providers.receipt_scan import ReceiptScanError, extract, prepare_image, scan_settings
from app.providers.retail.base import ReceiptData, ReceiptLineData
from app.schemas.receipt import ReceiptScanAccept, ReceiptScanDraft, ReceiptScanLine
from app.services import receipt_service

logger = logging.getLogger(__name__)

#: `Receipt.source` of everything read off a photo. One source for every
#: shop, so the same printed name bought in two shops is one product.
SCAN_SOURCE = "scan"

CENT = Decimal("0.01")
MILLI = Decimal("0.001")


def _decimal(value: Any, quantum: Decimal = CENT) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", ".").strip()).quantize(quantum)
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _when(value: Any) -> tuple[Optional[date], Optional[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    raw = value.strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            return date.fromisoformat(raw[:10]), None
        except ValueError:
            return None, None
    has_time = "T" in raw
    return parsed.date(), parsed.strftime("%H:%M") if has_time else None


def draft_from_model(raw: dict[str, Any]) -> ReceiptScanDraft:
    """What the model returned, made safe to show and edit.

    The schema already fixes the shape; this guards the values: a line
    without a usable price is dropped rather than failing the whole scan,
    and a discount is always a positive amount off.
    """
    lines: list[ReceiptScanLine] = []
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 255)
        quantity = _decimal(item.get("quantity"), MILLI)
        if quantity is None or quantity <= 0:
            quantity = Decimal("1.000")
        unit_price = _decimal(item.get("unit_price"))
        total = _decimal(item.get("total"))
        if name is None or (unit_price is None and total is None):
            continue
        if total is None:
            total = (quantity * (unit_price or Decimal("0"))).quantize(CENT)
        if unit_price is None:
            unit_price = (total / quantity).quantize(CENT)
        discount = abs(_decimal(item.get("discount")) or Decimal("0"))
        unit = _text(item.get("unit"), 10)
        lines.append(
            ReceiptScanLine(
                name=name,
                quantity=quantity,
                unit=unit.lower() if unit else None,
                unit_price=unit_price,
                total=total,
                discount=discount,
            )
        )
    day, clock = _when(raw.get("purchased_at"))
    currency = _text(raw.get("currency"), 3)
    tax_id = "".join(ch for ch in str(raw.get("tax_id") or "") if ch.isdigit())[:32] or None
    total = _decimal(raw.get("total"))
    return ReceiptScanDraft(
        store_name=_text(raw.get("store_name"), 255),
        store_address=_text(raw.get("store_address"), 500),
        tax_id=tax_id,
        date=day,
        time=clock,
        currency=currency.upper() if currency and len(currency) == 3 and currency.isalpha() else "PLN",
        total=total if total is not None and total > 0 else None,
        items=lines,
    )


def items_sum(lines: list[ReceiptScanLine]) -> Decimal:
    return sum((line.total - line.discount for line in lines), Decimal("0")).quantize(CENT)


def draft_of(scan: ReceiptScan) -> Optional[ReceiptScanDraft]:
    if not scan.result:
        return None
    try:
        return ReceiptScanDraft.model_validate(scan.result)
    except ValueError:
        logger.warning("Receipt scan %s holds an unreadable draft", scan.id)
        return None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def create_scan(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: Optional[uuid.UUID], data: bytes
) -> ReceiptScan:
    """Store the photo and open a scan for it. Raises ReceiptScanError."""
    image = prepare_image(data)
    scan_id = uuid.uuid4()
    storage_key = f"receipt-scans/{workspace_id}/{scan_id}.jpg"
    await get_storage_provider().upload(storage_key, image, "image/jpeg")
    scan = ReceiptScan(
        id=scan_id,
        workspace_id=workspace_id,
        user_id=user_id,
        status="processing",
        storage_key=storage_key,
        content_type="image/jpeg",
    )
    session.add(scan)
    await session.flush()
    return scan


async def read_scan(session: AsyncSession, scan: ReceiptScan) -> ReceiptScan:
    """Have the model read the stored photo; the scan ends `ready` or `failed`.

    Commit before calling: the model may take a minute, and no row should
    stay locked that long.
    """
    config = scan_settings()
    scan.model = config.model
    try:
        image = await get_storage_provider().download(scan.storage_key)
        draft = draft_from_model(await extract(image, config=config))
    except ReceiptScanError as exc:
        logger.info("Receipt scan %s failed: %s %s", scan.id, exc.code, exc)
        scan.status = "failed"
        scan.error = exc.code
    except FileNotFoundError:
        scan.status = "failed"
        scan.error = "image_missing"
    else:
        scan.status = "ready"
        scan.error = None
        scan.result = draft.model_dump(mode="json")
    await session.flush()
    return scan


def _receipt_data(scan: ReceiptScan, payload: ReceiptScanAccept) -> ReceiptData:
    purchased_at = None
    if payload.time:
        hours, minutes = (int(part) for part in payload.time.split(":"))
        purchased_at = datetime.combine(
            payload.date, time(min(hours, 23), min(minutes, 59)), tzinfo=app_timezone()
        )
    lines = [
        ReceiptLineData(
            name=line.name.strip(),
            quantity=line.quantity,
            unit_price=line.unit_price.quantize(CENT),
            total=line.total.quantize(CENT),
            discount=line.discount.quantize(CENT),
            unit=line.unit,
        )
        for line in payload.items
    ]
    return ReceiptData(
        external_id=str(scan.id),
        purchased_at=purchased_at,
        date=payload.date,
        total=payload.total.quantize(CENT),
        currency=payload.currency.upper(),
        store_name=payload.store_name,
        store_address=payload.store_address,
        discount_total=sum((line.discount for line in lines), Decimal("0")),
        lines=lines,
        raw_data={"scan_id": str(scan.id), "tax_id": payload.tax_id, "model": scan.model},
    )


async def accept_scan(
    session: AsyncSession, scan: ReceiptScan, payload: ReceiptScanAccept
) -> Receipt:
    """Turn the corrected draft into a receipt, or rewrite the one it became."""
    receipt, _ = await receipt_service.ingest_receipt(
        session, scan.workspace_id, SCAN_SOURCE, _receipt_data(scan, payload)
    )
    scan.status = "accepted"
    scan.error = None
    scan.result = payload.model_dump(mode="json")
    scan.receipt_id = receipt.id
    await session.flush()
    if receipt.transaction_id is None:
        await receipt_service.auto_match(session, scan.workspace_id, receipt_ids=[receipt.id])
    return receipt


async def forget_image(scan: ReceiptScan) -> None:
    try:
        await get_storage_provider().delete(scan.storage_key)
    except Exception:  # noqa: BLE001 - a missing file must not block deleting the row
        logger.warning("Could not delete the photo of receipt scan %s", scan.id, exc_info=True)
