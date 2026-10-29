"""Store receipts: loyalty connections, receipts, shopping products.

Gated by the `receipts` module. Connection secrets never leave the
server: a connection is read back with its status, never its tokens.
"""
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload, selectinload

from app.core.app_clock import app_today
from app.core.database import get_async_session
from app.core.module_gate import require_module, require_module_write
from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.category import Category
from app.models.receipt import Receipt, ReceiptItem, RetailConnection, ShoppingProduct
from app.providers.retail import list_retail_providers
from app.schemas.receipt import (
    ConsumptionRead,
    PricePointRead,
    ReceiptAutoMatchResult,
    ReceiptDetail,
    ReceiptItemRead,
    ReceiptList,
    ReceiptMatchRequest,
    ReceiptRead,
    ReceiptTransactionRead,
    RetailAuthorize,
    RetailConnectionCreate,
    RetailConnectionRead,
    RetailConnectionStatus,
    RetailConnectionStarted,
    RetailConnectionUpdate,
    RetailProviderRead,
    RetailSyncResult,
    ShoppingProductAliasRead,
    ShoppingProductDetail,
    ShoppingProductMerge,
    ShoppingProductRead,
    ShoppingProductUpdate,
)
from app.services import receipt_service as svc
from app.services import retail_connection_service as connections
from app.services.module_service import ModuleId
from app.services.receipt_service import Purchase, ReceiptError

router = APIRouter(prefix="/api", tags=["receipts"])

read_ctx = require_module(ModuleId.RECEIPTS)
write_ctx = require_module_write(ModuleId.RECEIPTS)


def _http(error: ReceiptError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


# ---------------------------------------------------------------------------
# Loyalty connections
# ---------------------------------------------------------------------------
async def _connection_read(
    session: AsyncSession, connection: RetailConnection
) -> RetailConnectionRead:
    count = await session.scalar(
        select(func.count(Receipt.id)).where(Receipt.connection_id == connection.id)
    )
    raw_account = (connection.settings or {}).get("match_account_id")
    return RetailConnectionRead(
        id=connection.id,
        provider=connection.provider,
        display_name=connection.display_name,
        status=cast(RetailConnectionStatus, connection.status),
        last_error=connection.last_error,
        last_sync_at=connection.last_sync_at,
        created_at=connection.created_at,
        match_account_id=uuid.UUID(raw_account) if raw_account else None,
        receipt_count=count or 0,
    )


async def _load_connection(
    session: AsyncSession, connection_id: uuid.UUID, workspace_id: uuid.UUID
) -> RetailConnection:
    connection = await connections.get_connection(session, connection_id, workspace_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection


@router.get("/retail/providers", response_model=list[RetailProviderRead])
async def list_providers(ctx: WorkspaceContext = Depends(read_ctx)):
    return [
        RetailProviderRead(
            name=p.name,
            display_name=p.display_name,
            redirect_prefix=p.redirect_prefix,
            auth_kind=cast(Literal["redirect", "cookie"], p.auth_kind),
        )
        for p in list_retail_providers()
    ]


@router.get("/retail/connections", response_model=list[RetailConnectionRead])
async def list_connections(
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    rows = await connections.list_connections(session, ctx.workspace.id)
    return [await _connection_read(session, c) for c in rows]


@router.post(
    "/retail/connections",
    response_model=RetailConnectionStarted,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: RetailConnectionCreate,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        connection, auth_url = await connections.create_connection(
            session, ctx.workspace.id, ctx.user_id, payload.provider, payload.display_name
        )
    except ReceiptError as exc:
        raise _http(exc)
    await session.commit()
    return RetailConnectionStarted(
        connection=await _connection_read(session, connection), auth_url=auth_url
    )


@router.patch("/retail/connections/{connection_id}", response_model=RetailConnectionRead)
async def update_connection(
    connection_id: uuid.UUID,
    payload: RetailConnectionUpdate,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    connection = await _load_connection(session, connection_id, ctx.workspace.id)
    data = payload.model_dump(exclude_unset=True)
    account_id = data.get("match_account_id")
    if account_id is not None:
        account = await session.get(Account, account_id)
        if account is None or account.workspace_id != ctx.workspace.id:
            raise HTTPException(status_code=404, detail="Account not found")
    await connections.update_connection(session, connection, data)
    await session.commit()
    return await _connection_read(session, connection)


@router.delete("/retail/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Forget the sign-in. Receipts already fetched stay."""
    connection = await _load_connection(session, connection_id, ctx.workspace.id)
    await session.delete(connection)
    await session.commit()


@router.post("/retail/connections/{connection_id}/authorize", response_model=RetailConnectionRead)
async def authorize_connection(
    connection_id: uuid.UUID,
    payload: RetailAuthorize,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    connection = await _load_connection(session, connection_id, ctx.workspace.id)
    try:
        await connections.authorize(
            session,
            connection,
            redirect_url=payload.redirect_url,
            refresh_token=payload.refresh_token,
        )
    except ReceiptError as exc:
        raise _http(exc)
    await session.commit()
    return await _connection_read(session, connection)


@router.post(
    "/retail/connections/{connection_id}/reauthorize", response_model=RetailConnectionStarted
)
async def reauthorize_connection(
    connection_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    connection = await _load_connection(session, connection_id, ctx.workspace.id)
    auth_url = await connections.restart_authorization(session, connection)
    await session.commit()
    return RetailConnectionStarted(
        connection=await _connection_read(session, connection), auth_url=auth_url
    )


@router.post("/retail/connections/{connection_id}/sync", response_model=RetailSyncResult)
async def sync_connection(
    connection_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    connection = await _load_connection(session, connection_id, ctx.workspace.id)
    try:
        result = await connections.sync_connection(session, connection)
    except ReceiptError as exc:
        raise _http(exc)
    return RetailSyncResult(
        created=result.created,
        updated=result.updated,
        matched=result.matched,
        failed=result.failed,
        has_more=result.has_more,
        connection=await _connection_read(session, connection),
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------
def _receipt_read(receipt: Receipt, item_count: int, cls=ReceiptRead):
    payload = cls.model_validate(
        {
            **{
                field: getattr(receipt, field)
                for field in ReceiptRead.model_fields
                if field not in ("transaction", "item_count") and hasattr(receipt, field)
            },
            "transaction": (
                ReceiptTransactionRead.model_validate(receipt.transaction)
                if receipt.transaction is not None
                else None
            ),
            "item_count": item_count,
        }
    )
    return payload


async def _load_receipt(
    session: AsyncSession, receipt_id: uuid.UUID, workspace_id: uuid.UUID
) -> Receipt:
    receipt = await session.scalar(
        select(Receipt)
        .options(
            selectinload(Receipt.transaction).options(noload("*")),
            selectinload(Receipt.items).selectinload(ReceiptItem.product),
        )
        .where(Receipt.id == receipt_id, Receipt.workspace_id == workspace_id)
        .execution_options(populate_existing=True)
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


def _receipt_detail(receipt: Receipt) -> ReceiptDetail:
    detail = _receipt_read(receipt, len(receipt.items), ReceiptDetail)
    detail.items = [
        ReceiptItemRead.model_validate(item).model_copy(
            update={"product_name": item.product.name if item.product else None}
        )
        for item in receipt.items
    ]
    return detail


@router.get("/receipts", response_model=ReceiptList)
async def list_receipts(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    source: Optional[str] = Query(None),
    matched: Optional[bool] = Query(None),
    q: Optional[str] = Query(None, description="Store name or a product on the receipt"),
    product_id: Optional[uuid.UUID] = Query(None),
    transaction_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    filters = [Receipt.workspace_id == ctx.workspace.id]
    if date_from:
        filters.append(Receipt.date >= date_from)
    if date_to:
        filters.append(Receipt.date <= date_to)
    if source:
        filters.append(Receipt.source == source)
    if matched is not None:
        filters.append(
            Receipt.transaction_id.is_not(None) if matched else Receipt.transaction_id.is_(None)
        )
    if transaction_id:
        filters.append(Receipt.transaction_id == transaction_id)
    if product_id:
        filters.append(
            Receipt.id.in_(
                select(ReceiptItem.receipt_id).where(ReceiptItem.product_id == product_id)
            )
        )
    if q:
        pattern = f"%{q.strip().lower()}%"
        filters.append(
            or_(
                func.lower(Receipt.store_name).like(pattern),
                Receipt.id.in_(
                    select(ReceiptItem.receipt_id).where(
                        ReceiptItem.workspace_id == ctx.workspace.id,
                        func.lower(ReceiptItem.name).like(pattern),
                    )
                ),
            )
        )
    total = await session.scalar(select(func.count(Receipt.id)).where(*filters))
    item_counts = (
        select(ReceiptItem.receipt_id, func.count(ReceiptItem.id).label("n"))
        .group_by(ReceiptItem.receipt_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Receipt, func.coalesce(item_counts.c.n, 0))
            .outerjoin(item_counts, item_counts.c.receipt_id == Receipt.id)
            .options(noload(Receipt.items), selectinload(Receipt.transaction).options(noload("*")))
            .where(*filters)
            .order_by(Receipt.date.desc(), Receipt.purchased_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ReceiptList(items=[_receipt_read(r, n) for r, n in rows], total=total or 0)


@router.post("/receipts/match", response_model=ReceiptAutoMatchResult)
async def auto_match_receipts(
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    matched = await svc.auto_match(session, ctx.workspace.id)
    await session.commit()
    return ReceiptAutoMatchResult(matched=matched)


@router.get("/receipts/{receipt_id}", response_model=ReceiptDetail)
async def get_receipt(
    receipt_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    return _receipt_detail(await _load_receipt(session, receipt_id, ctx.workspace.id))


@router.delete("/receipts/{receipt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_receipt(
    receipt_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    receipt = await _load_receipt(session, receipt_id, ctx.workspace.id)
    await session.delete(receipt)
    await session.commit()


@router.get("/receipts/{receipt_id}/candidates", response_model=list[ReceiptTransactionRead])
async def receipt_candidates(
    receipt_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    receipt = await _load_receipt(session, receipt_id, ctx.workspace.id)
    candidates = await svc.find_candidates(session, receipt)
    return [ReceiptTransactionRead.model_validate(tx) for tx in candidates]


@router.put("/receipts/{receipt_id}/transaction", response_model=ReceiptDetail)
async def link_receipt(
    receipt_id: uuid.UUID,
    payload: ReceiptMatchRequest,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    receipt = await _load_receipt(session, receipt_id, ctx.workspace.id)
    try:
        await svc.link_transaction(session, receipt, payload.transaction_id)
    except ReceiptError as exc:
        raise _http(exc)
    await session.commit()
    return _receipt_detail(await _load_receipt(session, receipt_id, ctx.workspace.id))


@router.delete("/receipts/{receipt_id}/transaction", response_model=ReceiptDetail)
async def unlink_receipt(
    receipt_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    receipt = await _load_receipt(session, receipt_id, ctx.workspace.id)
    await svc.unlink_transaction(session, receipt)
    await session.commit()
    return _receipt_detail(await _load_receipt(session, receipt_id, ctx.workspace.id))


# ---------------------------------------------------------------------------
# Shopping products
# ---------------------------------------------------------------------------
def _product_read(
    product: ShoppingProduct, purchases: list[Purchase], cls=ShoppingProductRead
):
    stats: dict = {}
    if purchases:
        prices = [p.unit_price for p in purchases]
        first, last = purchases[0], purchases[-1]
        units = {p.unit for p in purchases if p.unit}
        stats = {
            "purchase_count": len(purchases),
            "total_quantity": sum((p.quantity for p in purchases), Decimal("0")),
            "unit": units.pop() if len(units) == 1 else None,
            "total_spent": sum((p.paid for p in purchases), Decimal("0")),
            "first_purchased_at": first.date,
            "last_purchased_at": last.date,
            "last_unit_price": last.unit_price,
            "min_unit_price": min(prices),
            "max_unit_price": max(prices),
            "price_change_pct": (
                round(float((last.unit_price - first.unit_price) / first.unit_price * 100), 1)
                if first.unit_price
                else None
            ),
            "sources": sorted({p.source for p in purchases}),
        }
    consumption = None
    if product.track_consumption and purchases:
        consumption = ConsumptionRead(**vars(svc.consumption(purchases, app_today())))
    return cls(
        id=product.id,
        name=product.name,
        category_id=product.category_id,
        track_consumption=product.track_consumption,
        notes=product.notes,
        consumption=consumption,
        **stats,
    )


async def _load_product(
    session: AsyncSession, product_id: uuid.UUID, workspace_id: uuid.UUID
) -> ShoppingProduct:
    product = await svc.get_product(session, product_id, workspace_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


async def _product_detail(session: AsyncSession, product: ShoppingProduct) -> ShoppingProductDetail:
    await session.refresh(product, ["aliases"])
    purchases = (await svc.purchases_by_product(session, product.workspace_id, [product.id])).get(
        product.id, []
    )
    detail = _product_read(product, purchases, ShoppingProductDetail)
    detail.aliases = [ShoppingProductAliasRead.model_validate(a) for a in product.aliases]
    detail.price_history = [
        PricePointRead(
            receipt_id=p.receipt_id,
            date=p.date,
            unit_price=p.unit_price,
            effective_unit_price=p.effective_unit_price,
            quantity=p.quantity,
            unit=p.unit,
            store_name=p.store_name,
            source=p.source,
        )
        for p in purchases
    ]
    return detail


@router.get("/shopping-products", response_model=list[ShoppingProductRead])
async def list_shopping_products(
    q: Optional[str] = Query(None),
    tracked: Optional[bool] = Query(None),
    category_id: Optional[uuid.UUID] = Query(None),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    products = await svc.list_products(
        session, ctx.workspace.id, q=q, tracked=tracked, category_id=category_id
    )
    purchases = await svc.purchases_by_product(
        session, ctx.workspace.id, [p.id for p in products] if q or tracked or category_id else None
    )
    return [_product_read(p, purchases.get(p.id, [])) for p in products]


@router.get("/shopping-products/{product_id}", response_model=ShoppingProductDetail)
async def get_shopping_product(
    product_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    return await _product_detail(session, await _load_product(session, product_id, ctx.workspace.id))


@router.patch("/shopping-products/{product_id}", response_model=ShoppingProductDetail)
async def update_shopping_product(
    product_id: uuid.UUID,
    payload: ShoppingProductUpdate,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    product = await _load_product(session, product_id, ctx.workspace.id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("category_id") is not None:
        category = await session.get(Category, data["category_id"])
        if category is None or category.workspace_id != ctx.workspace.id:
            raise HTTPException(status_code=404, detail="Category not found")
    try:
        await svc.update_product(session, product, data)
    except ReceiptError as exc:
        raise _http(exc)
    await session.commit()
    return await _product_detail(session, product)


@router.post("/shopping-products/{product_id}/merge", response_model=ShoppingProductDetail)
async def merge_shopping_products(
    product_id: uuid.UUID,
    payload: ShoppingProductMerge,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
):
    """Fold the listed products into this one; their printed names follow."""
    product = await _load_product(session, product_id, ctx.workspace.id)
    try:
        await svc.merge_products(session, product, payload.source_ids)
    except ReceiptError as exc:
        raise _http(exc)
    await session.commit()
    return await _product_detail(session, product)
