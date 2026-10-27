"""The shape every loyalty-account integration speaks.

A retail provider reads e-receipts from a store's loyalty app. All of the
ones worth having sit behind a login page with a captcha, so none of them
can be signed into by the server: the person signs in in their own
browser, and the store redirects to the app's custom scheme
(`app://...?code=...`). The browser cannot open that address, but it
shows it, and pasting it back here completes a standard PKCE exchange.
From then on the provider lives on a refresh token, like the mobile app.

Providers only translate: a store's payload in, `ReceiptData` out.
Storing, pairing with bank transactions and grouping into products is
the service layer's job, the same split the bank providers keep.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

HTTP_TIMEOUT = 30.0


class RetailAuthError(Exception):
    """The store refused the credentials; only a new sign-in helps."""


class RetailProviderError(Exception):
    """A transient failure talking to the store. Retried on the next sync."""


@dataclass
class ReceiptLineData:
    name: str
    quantity: Decimal
    unit_price: Decimal
    # Shelf value of the line, before discounts.
    total: Decimal
    # Positive amount taken off this line.
    discount: Decimal = Decimal("0")
    unit: Optional[str] = None
    code: Optional[str] = None


@dataclass
class ReceiptSummaryData:
    """A receipt as the store's history list shows it, before the detail call."""

    external_id: str
    purchased_at: Optional[datetime] = None
    total: Optional[Decimal] = None
    store_name: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class ReceiptData:
    external_id: str
    purchased_at: Optional[datetime]
    date: date
    total: Decimal
    currency: str = "PLN"
    store_name: Optional[str] = None
    store_address: Optional[str] = None
    discount_total: Decimal = Decimal("0")
    lines: list[ReceiptLineData] = field(default_factory=list)
    raw_data: Optional[dict] = None


@dataclass
class PendingAuthorization:
    """What the person opens, and what has to be remembered until they return."""

    auth_url: str
    state: dict[str, Any]


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def code_from_redirect(value: str) -> str:
    """Pull the OAuth code out of whatever the person pasted.

    Browsers report the failed navigation as a console line ("Failed to
    launch 'app://...?code=...' because the scheme does not have a
    registered handler."), people copy the bare address from the Network
    tab, and some paste the code alone. All three are accepted.
    """
    raw = (value or "").strip().strip("\"'")
    if not raw:
        raise RetailAuthError("missing_auth_code")
    # Cut a URL out of a console line.
    for token in raw.replace("'", " ").replace('"', " ").split():
        if "code=" in token:
            raw = token.rstrip(".,;")
            break
    if "code=" not in raw:
        if " " in raw or "://" in raw:
            raise RetailAuthError("missing_auth_code")
        return raw
    parsed = urlparse(raw)
    query = parse_qs(parsed.query) or parse_qs(parsed.fragment)
    if not query and "?" not in raw:
        query = parse_qs(raw)
    code = (query.get("code") or [None])[0]
    if not code:
        raise RetailAuthError("missing_auth_code")
    return code


def to_decimal(value: Any, *, cents: bool = False) -> Optional[Decimal]:
    """Decimal from a number or a Polish-formatted string; None when unreadable."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if cents:
        number = number / 100
    return number


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class RetailClient(ABC):
    """An authenticated session against one loyalty account.

    `credentials` is updated in place whenever a token is refreshed; the
    caller persists it after the sync, refreshed or not.
    """

    def __init__(self, credentials: dict, http: httpx.AsyncClient) -> None:
        self.credentials = credentials
        self._http = http

    @abstractmethod
    async def list_receipts(
        self, *, known_ids: set[str], max_pages: int = 50
    ) -> list[ReceiptSummaryData]:
        """Newest first, stopping after the first page that holds nothing new."""
        ...

    @abstractmethod
    async def fetch_receipt(self, summary: ReceiptSummaryData) -> Optional[ReceiptData]:
        """Full receipt, or None when the store has no line detail for it."""
        ...


class RetailProvider(ABC):
    """One store chain's loyalty app."""

    #: Words a bank statement uses for this chain, for pairing receipts.
    merchant_keywords: tuple[str, ...] = ()

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    #: The scheme the store redirects to after sign-in, shown to the
    #: person so they know what to look for.
    redirect_prefix: str = ""

    @abstractmethod
    def start_authorization(self) -> PendingAuthorization: ...

    @abstractmethod
    async def complete_authorization(
        self, state: dict[str, Any], pasted: str, http: httpx.AsyncClient
    ) -> dict:
        """Exchange the pasted redirect for credentials."""
        ...

    async def credentials_from_refresh_token(
        self, refresh_token: str, http: httpx.AsyncClient
    ) -> dict:
        """Validate a refresh token obtained elsewhere and return credentials."""
        raise RetailAuthError("refresh_token_not_supported")

    @abstractmethod
    def client(self, credentials: dict, http: httpx.AsyncClient) -> RetailClient: ...

    def receipt_from_raw(self, external_id: str, raw_data: dict) -> Optional[ReceiptData]:
        """Read a stored receipt again from the payload kept with it, offline."""
        return None


def new_http_client(transport: Optional[httpx.AsyncBaseTransport] = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=transport, follow_redirects=False)
