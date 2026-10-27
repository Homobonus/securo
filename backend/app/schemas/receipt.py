import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RetailConnectionStatus = Literal["pending", "active", "reauth_required", "error"]


# ---------------------------------------------------------------------------
# Loyalty connections
# ---------------------------------------------------------------------------
class RetailProviderRead(BaseModel):
    name: str
    display_name: str
    #: What the store redirects to after sign-in, so the UI can say what
    #: to look for in the browser.
    redirect_prefix: str


class RetailConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    display_name: Optional[str] = None
    status: RetailConnectionStatus
    last_error: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    created_at: datetime
    match_account_id: Optional[uuid.UUID] = None
    receipt_count: int = 0


class RetailConnectionCreate(BaseModel):
    provider: str = Field(..., min_length=1, max_length=50)
    display_name: Optional[str] = Field(default=None, max_length=255)


class RetailConnectionStarted(BaseModel):
    connection: RetailConnectionRead
    #: Open in a browser, sign in, paste back where it redirects.
    auth_url: str


class RetailAuthorize(BaseModel):
    #: The `app://...?code=...` address, the console line holding it, or the code.
    redirect_url: Optional[str] = Field(default=None, max_length=4000)
    refresh_token: Optional[str] = Field(default=None, max_length=8000)


class RetailConnectionUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    #: Pair receipts only with transactions of this account. Null clears it.
    match_account_id: Optional[uuid.UUID] = None


class RetailSyncResult(BaseModel):
    created: int
    updated: int
    matched: int
    #: Receipts the store would not return this time; retried on later syncs.
    failed: int = 0
    #: True when more receipts wait than one sync fetches; sync again.
    has_more: bool
    connection: RetailConnectionRead


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------
class ReceiptItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    name: str
    code: Optional[str] = None
    quantity: Decimal
    unit: Optional[str] = None
    unit_price: Decimal
    total: Decimal
    discount: Decimal
    product_id: Optional[uuid.UUID] = None
    product_name: Optional[str] = None


class ReceiptTransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    description: str
    amount: Decimal
    currency: str
    date: date


class ReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    connection_id: Optional[uuid.UUID] = None
    external_id: str
    purchased_at: Optional[datetime] = None
    date: date
    store_name: Optional[str] = None
    store_address: Optional[str] = None
    total: Decimal
    discount_total: Decimal
    currency: str
    transaction_id: Optional[uuid.UUID] = None
    match_method: Optional[Literal["auto", "manual"]] = None
    transaction: Optional[ReceiptTransactionRead] = None
    item_count: int = 0


class ReceiptDetail(ReceiptRead):
    items: list[ReceiptItemRead] = []


class ReceiptList(BaseModel):
    items: list[ReceiptRead]
    total: int


class ReceiptMatchRequest(BaseModel):
    transaction_id: uuid.UUID


class ReceiptAutoMatchResult(BaseModel):
    matched: int


# ---------------------------------------------------------------------------
# Shopping products
# ---------------------------------------------------------------------------
class ConsumptionRead(BaseModel):
    purchase_count: int
    avg_days_between: Optional[float] = None
    #: How many days one unit (piece, kilogram) lasts.
    days_per_unit: Optional[float] = None
    units_per_month: Optional[float] = None
    monthly_cost: Optional[Decimal] = None
    estimated_depletion: Optional[date] = None
    days_left: Optional[int] = None


class ShoppingProductRead(BaseModel):
    id: uuid.UUID
    name: str
    category_id: Optional[uuid.UUID] = None
    track_consumption: bool
    notes: Optional[str] = None
    purchase_count: int = 0
    total_quantity: Decimal = Decimal("0")
    unit: Optional[str] = None
    total_spent: Decimal = Decimal("0")
    first_purchased_at: Optional[date] = None
    last_purchased_at: Optional[date] = None
    last_unit_price: Optional[Decimal] = None
    min_unit_price: Optional[Decimal] = None
    max_unit_price: Optional[Decimal] = None
    #: Change of the shelf price from the first purchase to the last, in percent.
    price_change_pct: Optional[float] = None
    sources: list[str] = []
    consumption: Optional[ConsumptionRead] = None


class ShoppingProductAliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    name: str


class PricePointRead(BaseModel):
    receipt_id: uuid.UUID
    date: date
    unit_price: Decimal
    #: What one unit actually cost once the line's discounts are applied.
    effective_unit_price: Decimal
    quantity: Decimal
    unit: Optional[str] = None
    store_name: Optional[str] = None
    source: str


class ShoppingProductDetail(ShoppingProductRead):
    aliases: list[ShoppingProductAliasRead] = []
    price_history: list[PricePointRead] = []


class ShoppingProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    category_id: Optional[uuid.UUID] = None
    track_consumption: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=1000)


class ShoppingProductMerge(BaseModel):
    source_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=100)
