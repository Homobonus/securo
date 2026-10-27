"""Receipts, the products on them, and the bank transactions that paid.

Three jobs, kept apart so each can be tested without the others:

  - **Ingest.** A provider's `ReceiptData` becomes a `Receipt` with its
    items. Re-ingesting the same store id rewrites the lines (a better
    parser, a corrected receipt) and keeps the pairing.
  - **Pairing.** A receipt is paired with the debit that paid it when the
    evidence admits exactly one answer: same amount, a date at or just
    after the purchase, and, among several, the one whose description
    names the store. Two equal purchases two days apart are left for a
    person to pick rather than guessed. Manual pairings are never undone.
  - **Products.** Every printed name reaches a shopping product through an
    alias, created on first sight. From the items a product collects come
    its price history and, for consumables, how long one unit lasts.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.models.receipt import (
    Receipt,
    ReceiptItem,
    RetailConnection,
    ShoppingProduct,
    ShoppingProductAlias,
)
from app.models.transaction import Transaction
from app.providers.retail import get_retail_provider
from app.providers.retail.base import ReceiptData

#: A card payment posts on the day of purchase or a few days later; a day
#: before covers a receipt stamped after midnight in another timezone.
MATCH_DAYS_BEFORE = 1
MATCH_DAYS_AFTER = 5


class ReceiptError(Exception):
    """A request the domain refuses; the API maps it to 4xx."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_name(name: str) -> str:
    return " ".join((name or "").casefold().split())[:255]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
async def _resolve_product(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source: str,
    name: str,
    cache: dict[str, uuid.UUID],
) -> uuid.UUID:
    key = normalize_name(name)
    if key in cache:
        return cache[key]
    product_id = await session.scalar(
        select(ShoppingProductAlias.product_id).where(
            ShoppingProductAlias.workspace_id == workspace_id,
            ShoppingProductAlias.source == source,
            ShoppingProductAlias.normalized_name == key,
        )
    )
    if product_id is None:
        product = ShoppingProduct(workspace_id=workspace_id, name=name.strip()[:255])
        session.add(product)
        await session.flush()
        session.add(
            ShoppingProductAlias(
                workspace_id=workspace_id,
                product_id=product.id,
                source=source,
                name=name.strip()[:255],
                normalized_name=key,
            )
        )
        product_id = product.id
    cache[key] = product_id
    return product_id


async def ingest_receipt(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source: str,
    data: ReceiptData,
    *,
    connection_id: Optional[uuid.UUID] = None,
    product_cache: Optional[dict[str, uuid.UUID]] = None,
) -> tuple[Receipt, bool]:
    """Insert or rewrite one receipt. Returns (receipt, created)."""
    cache = product_cache if product_cache is not None else {}
    receipt = await session.scalar(
        select(Receipt).where(
            Receipt.workspace_id == workspace_id,
            Receipt.source == source,
            Receipt.external_id == data.external_id,
        )
    )
    created = receipt is None
    if receipt is None:
        receipt = Receipt(workspace_id=workspace_id, source=source, external_id=data.external_id)
        session.add(receipt)
    else:
        await session.execute(delete(ReceiptItem).where(ReceiptItem.receipt_id == receipt.id))
    receipt.connection_id = connection_id or receipt.connection_id
    receipt.purchased_at = data.purchased_at
    receipt.date = data.date
    receipt.store_name = (data.store_name or "")[:255] or None
    receipt.store_address = (data.store_address or "")[:500] or None
    receipt.total = data.total
    receipt.discount_total = data.discount_total
    receipt.currency = data.currency
    receipt.raw_data = data.raw_data
    await session.flush()

    for position, line in enumerate(data.lines):
        session.add(
            ReceiptItem(
                receipt_id=receipt.id,
                workspace_id=workspace_id,
                position=position,
                name=line.name[:255],
                code=line.code,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                total=line.total,
                discount=line.discount,
                product_id=await _resolve_product(session, workspace_id, source, line.name, cache),
            )
        )
    await session.flush()
    session.expire(receipt, ["items"])
    return receipt, created


async def reparse_receipts(
    session: AsyncSession, workspace_id: uuid.UUID, *, source: Optional[str] = None
) -> int:
    """Rebuild stored receipts from the payload kept with them, without the store.

    For when the parser learns something new. Lines are rewritten,
    pairings and the receipt rows themselves stay. Returns how many
    receipts were rebuilt.
    """
    statement = select(
        Receipt.source, Receipt.external_id, Receipt.raw_data, Receipt.connection_id
    ).where(Receipt.workspace_id == workspace_id, Receipt.raw_data.is_not(None))
    if source:
        statement = statement.where(Receipt.source == source)
    rebuilt = 0
    caches: dict[str, dict[str, uuid.UUID]] = {}
    for receipt_source, external_id, raw, connection_id in (await session.execute(statement)).all():
        provider = get_retail_provider(receipt_source)
        if provider is None or not isinstance(raw, dict):
            continue
        data = provider.receipt_from_raw(external_id, raw)
        if data is None:
            continue
        await ingest_receipt(
            session,
            workspace_id,
            receipt_source,
            data,
            connection_id=connection_id,
            product_cache=caches.setdefault(receipt_source, {}),
        )
        rebuilt += 1
    await session.flush()
    return rebuilt


# ---------------------------------------------------------------------------
# Pairing with bank transactions
# ---------------------------------------------------------------------------
def _keywords(source: str) -> tuple[str, ...]:
    provider = get_retail_provider(source)
    return provider.merchant_keywords if provider else ()


def _names_store(tx: Transaction, keywords: Iterable[str]) -> bool:
    text = " ".join(
        part for part in (tx.description, tx.original_description, tx.payee) if part
    ).casefold()
    return any(keyword in text for keyword in keywords)


async def find_candidates(
    session: AsyncSession,
    receipt: Receipt,
    *,
    account_id: Optional[uuid.UUID] = None,
    exclude_ids: Iterable[uuid.UUID] = (),
) -> list[Transaction]:
    """Debits that could have paid this receipt, best first."""
    if receipt.total is None or receipt.total <= 0:
        return []
    claimed = (
        select(Receipt.transaction_id)
        .where(
            Receipt.workspace_id == receipt.workspace_id,
            Receipt.transaction_id.is_not(None),
            Receipt.id != receipt.id,
        )
        .scalar_subquery()
    )
    statement = (
        select(Transaction)
        .options(noload("*"))
        .where(
            Transaction.workspace_id == receipt.workspace_id,
            Transaction.type == "debit",
            func.abs(Transaction.amount) == receipt.total,
            Transaction.currency == receipt.currency,
            Transaction.date >= receipt.date - timedelta(days=MATCH_DAYS_BEFORE),
            Transaction.date <= receipt.date + timedelta(days=MATCH_DAYS_AFTER),
            Transaction.is_ignored.is_(False),
            Transaction.id.not_in(claimed),
        )
    )
    if account_id is not None:
        statement = statement.where(Transaction.account_id == account_id)
    excluded = set(exclude_ids)
    candidates = [
        tx for tx in (await session.execute(statement)).scalars().all() if tx.id not in excluded
    ]
    keywords = _keywords(receipt.source)
    candidates.sort(
        key=lambda tx: (not _names_store(tx, keywords), abs((tx.date - receipt.date).days), tx.date)
    )
    return candidates


def pick_match(receipt: Receipt, candidates: list[Transaction]) -> Optional[Transaction]:
    """The one unambiguous candidate, or None."""
    if not candidates:
        return None
    keywords = _keywords(receipt.source)
    naming = [tx for tx in candidates if _names_store(tx, keywords)]
    pool = naming or candidates
    if len(pool) == 1:
        return pool[0]
    # Several plausible debits: only the uniquely closest date settles it.
    distances = sorted(abs((tx.date - receipt.date).days) for tx in pool)
    if distances[0] < distances[1]:
        return min(pool, key=lambda tx: abs((tx.date - receipt.date).days))
    return None


async def auto_match(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    receipt_ids: Optional[Iterable[uuid.UUID]] = None,
) -> int:
    """Pair every unpaired receipt that admits exactly one debit. Returns the count."""
    statement = (
        select(Receipt)
        .options(noload(Receipt.items))
        .where(Receipt.workspace_id == workspace_id, Receipt.transaction_id.is_(None))
        .order_by(Receipt.date)
    )
    if receipt_ids is not None:
        statement = statement.where(Receipt.id.in_(list(receipt_ids)))
    receipts = (await session.execute(statement)).scalars().all()
    settings_by_connection: dict[uuid.UUID, Optional[uuid.UUID]] = {}
    claimed: set[uuid.UUID] = set()
    matched = 0
    for receipt in receipts:
        account_id = None
        if receipt.connection_id is not None:
            if receipt.connection_id not in settings_by_connection:
                connection = await session.get(RetailConnection, receipt.connection_id)
                raw = ((connection.settings or {}) if connection else {}).get("match_account_id")
                settings_by_connection[receipt.connection_id] = uuid.UUID(raw) if raw else None
            account_id = settings_by_connection[receipt.connection_id]
        candidates = await find_candidates(
            session, receipt, account_id=account_id, exclude_ids=claimed
        )
        tx = pick_match(receipt, candidates)
        if tx is None:
            continue
        receipt.transaction_id = tx.id
        receipt.match_method = "auto"
        claimed.add(tx.id)
        matched += 1
    await session.flush()
    return matched


async def link_transaction(
    session: AsyncSession, receipt: Receipt, transaction_id: uuid.UUID
) -> None:
    tx = await session.get(Transaction, transaction_id)
    if tx is None or tx.workspace_id != receipt.workspace_id:
        raise ReceiptError("Transaction not found", 404)
    taken = await session.scalar(
        select(Receipt.id).where(
            Receipt.workspace_id == receipt.workspace_id,
            Receipt.transaction_id == transaction_id,
            Receipt.id != receipt.id,
        )
    )
    if taken is not None:
        raise ReceiptError("Transaction is already paired with another receipt", 409)
    receipt.transaction_id = transaction_id
    receipt.match_method = "manual"
    await session.flush()


async def unlink_transaction(session: AsyncSession, receipt: Receipt) -> None:
    receipt.transaction_id = None
    receipt.match_method = None
    await session.flush()


# ---------------------------------------------------------------------------
# Products: prices and consumption
# ---------------------------------------------------------------------------
@dataclass
class Purchase:
    receipt_id: uuid.UUID
    date: date
    quantity: Decimal
    unit: Optional[str]
    unit_price: Decimal
    total: Decimal
    discount: Decimal
    store_name: Optional[str]
    source: str

    @property
    def paid(self) -> Decimal:
        return self.total - self.discount

    @property
    def effective_unit_price(self) -> Decimal:
        if self.quantity <= 0:
            return self.unit_price
        return (self.paid / self.quantity).quantize(Decimal("0.01"))


@dataclass
class Consumption:
    """How fast a product is used up, from the gaps between purchases."""

    purchase_count: int
    avg_days_between: Optional[float]
    days_per_unit: Optional[float]
    units_per_month: Optional[float]
    monthly_cost: Optional[Decimal]
    estimated_depletion: Optional[date]
    days_left: Optional[int]


def consumption(purchases: list[Purchase], today: date) -> Consumption:
    """Consumption estimated from purchase spacing.

    Whatever was bought on one shopping day lasted until the next one, so
    every day but the last contributes its quantity to the time it
    covered. The last purchase is still in the cupboard and only says
    when it will run out. Two shopping days are the minimum to say
    anything.
    """
    by_day: dict[date, tuple[Decimal, Decimal]] = {}
    for p in purchases:
        quantity, paid = by_day.get(p.date, (Decimal("0"), Decimal("0")))
        by_day[p.date] = (quantity + p.quantity, paid + p.paid)
    days = sorted(by_day)
    count = len(purchases)
    if len(days) < 2:
        return Consumption(count, None, None, None, None, None, None)
    span = (days[-1] - days[0]).days
    used = sum((by_day[d][0] for d in days[:-1]), Decimal("0"))
    spent = sum((by_day[d][1] for d in days[:-1]), Decimal("0"))
    if span <= 0 or used <= 0:
        return Consumption(count, None, None, None, None, None, None)
    days_per_unit = span / float(used)
    depletion = days[-1] + timedelta(days=round(float(by_day[days[-1]][0]) * days_per_unit))
    return Consumption(
        purchase_count=count,
        avg_days_between=round(span / (len(days) - 1), 1),
        days_per_unit=round(days_per_unit, 2),
        units_per_month=round(30.0 / days_per_unit, 2),
        monthly_cost=(spent / span * 30).quantize(Decimal("0.01")),
        estimated_depletion=depletion,
        days_left=(depletion - today).days,
    )


async def purchases_by_product(
    session: AsyncSession, workspace_id: uuid.UUID, product_ids: Optional[list[uuid.UUID]] = None
) -> dict[uuid.UUID, list[Purchase]]:
    statement = (
        select(
            ReceiptItem.product_id,
            Receipt.id,
            Receipt.date,
            ReceiptItem.quantity,
            ReceiptItem.unit,
            ReceiptItem.unit_price,
            ReceiptItem.total,
            ReceiptItem.discount,
            Receipt.store_name,
            Receipt.source,
        )
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(ReceiptItem.workspace_id == workspace_id, ReceiptItem.product_id.is_not(None))
        .order_by(Receipt.date, Receipt.purchased_at, ReceiptItem.position)
    )
    if product_ids is not None:
        statement = statement.where(ReceiptItem.product_id.in_(product_ids))
    grouped: dict[uuid.UUID, list[Purchase]] = {}
    for row in (await session.execute(statement)).all():
        grouped.setdefault(row[0], []).append(
            Purchase(
                receipt_id=row[1],
                date=row[2],
                quantity=Decimal(row[3]),
                unit=row[4],
                unit_price=Decimal(row[5]),
                total=Decimal(row[6]),
                discount=Decimal(row[7] or 0),
                store_name=row[8],
                source=row[9],
            )
        )
    return grouped


async def list_products(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    q: Optional[str] = None,
    tracked: Optional[bool] = None,
    category_id: Optional[uuid.UUID] = None,
) -> list[ShoppingProduct]:
    statement = select(ShoppingProduct).where(ShoppingProduct.workspace_id == workspace_id)
    if q:
        pattern = f"%{q.strip().casefold()}%"
        aliased_ids = select(ShoppingProductAlias.product_id).where(
            ShoppingProductAlias.workspace_id == workspace_id,
            ShoppingProductAlias.normalized_name.like(pattern),
        )
        statement = statement.where(
            or_(func.lower(ShoppingProduct.name).like(pattern), ShoppingProduct.id.in_(aliased_ids))
        )
    if tracked is not None:
        statement = statement.where(ShoppingProduct.track_consumption.is_(tracked))
    if category_id is not None:
        statement = statement.where(ShoppingProduct.category_id == category_id)
    statement = statement.order_by(func.lower(ShoppingProduct.name))
    return list((await session.execute(statement)).scalars().all())


async def get_product(
    session: AsyncSession, product_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[ShoppingProduct]:
    product = await session.get(ShoppingProduct, product_id)
    if product is None or product.workspace_id != workspace_id:
        return None
    return product


async def update_product(session: AsyncSession, product: ShoppingProduct, data: dict) -> None:
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise ReceiptError("Name must not be empty", 422)
        product.name = name[:255]
    if "category_id" in data:
        product.category_id = data["category_id"]
    if "track_consumption" in data and data["track_consumption"] is not None:
        product.track_consumption = bool(data["track_consumption"])
    if "notes" in data:
        product.notes = data["notes"]
    await session.flush()


async def merge_products(
    session: AsyncSession, target: ShoppingProduct, source_ids: list[uuid.UUID]
) -> int:
    """Fold other products into `target`: their items and their aliases.

    The aliases move rather than vanish, so the next sync files those
    printed names under the target too.
    """
    ids = [pid for pid in dict.fromkeys(source_ids) if pid != target.id]
    if not ids:
        return 0
    found = (
        await session.execute(
            select(ShoppingProduct.id).where(
                ShoppingProduct.workspace_id == target.workspace_id,
                ShoppingProduct.id.in_(ids),
            )
        )
    ).scalars().all()
    if len(found) != len(ids):
        raise ReceiptError("Product not found", 404)
    await session.execute(
        update(ReceiptItem).where(ReceiptItem.product_id.in_(ids)).values(product_id=target.id)
    )
    await session.execute(
        update(ShoppingProductAlias)
        .where(ShoppingProductAlias.product_id.in_(ids))
        .values(product_id=target.id)
    )
    if target.category_id is None or not target.track_consumption:
        sources = (
            await session.execute(select(ShoppingProduct).where(ShoppingProduct.id.in_(ids)))
        ).scalars().all()
        for other in sources:
            target.category_id = target.category_id or other.category_id
            target.track_consumption = target.track_consumption or other.track_consumption
    await session.execute(delete(ShoppingProduct).where(ShoppingProduct.id.in_(ids)))
    await session.flush()
    session.expire(target, ["aliases"])
    return len(ids)
