"""Lidl Plus: the loyalty app of the Lidl chain.

Unofficial: the private API of the mobile app, as used by the `lidl-plus`
and `ilidl` client libraries. Sign-in is an OAuth PKCE flow for the
public `LidlPlusNativeClient`; the login page has reCAPTCHA Enterprise,
which no headless browser gets through, so the person signs in in their
own browser and pastes the `com.lidlplus.app://callback?code=...`
redirect. A refresh token (`offline_access`) keeps the link alive.

Receipt detail is a printable HTML document (`htmlPrintedReceipt`) whose
article spans carry their data as attributes: `data-art-id`,
`data-art-description`, `data-unit-price`, `data-art-quantity`. The same
article is printed in more than one span (name line, price line), so the
spans are counted and divided by the multiplicity the whole receipt
shows, rather than de-duplicated by id: buying the same yoghurt twice in
two scans is two yoghurts, not one.
"""
from __future__ import annotations

import base64
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from app.providers.retail.base import (
    PendingAuthorization,
    ReceiptData,
    ReceiptLineData,
    ReceiptSummaryData,
    RetailAuthError,
    RetailClient,
    RetailProvider,
    RetailProviderError,
    code_from_redirect,
    generate_pkce,
    money,
    parse_datetime,
    to_decimal,
)

logger = logging.getLogger(__name__)

AUTH_API = "https://accounts.lidl.com"
TICKETS_API = "https://tickets.lidlplus.com/api"
CLIENT_ID = "LidlPlusNativeClient"
REDIRECT_URI = "com.lidlplus.app://callback"
SCOPES = "openid profile offline_access lpprofile lpapis"
COUNTRY = "PL"
LANGUAGE = "pl"
APP_HEADERS = {
    "App-Version": "999.99.9",
    "Operating-System": "iOs",
    "App": "com.lidl.eci.lidl.plus",
    "Accept-Language": LANGUAGE,
    "Accept": "application/json",
}
_WARSAW = ZoneInfo("Europe/Warsaw")
_BASIC = base64.b64encode(f"{CLIENT_ID}:secret".encode()).decode()

_NEGATIVE_AMOUNT = re.compile(r"(?<![\d,.])-\s?(\d{1,6}[.,]\d{2})(?!\d)")
# Lines past these words are the receipt's totals, not a rebate on an article.
_SUMMARY_MARKERS = ("suma", "razem", "do zapłaty", "sprzedaż opodatkowana", "ptu", "total")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class _ReceiptHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag != "span":
            return
        css = (dict(attrs).get("class") or "").split()
        if "article" not in css:
            return
        data = {k: v for k, v in attrs if k.startswith("data-") and v is not None}
        if data.get("data-art-description"):
            self.events.append(("article", data))

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.events.append(("text", text))


def _article_key(attrs: dict[str, str]) -> tuple:
    return tuple(sorted(attrs.items()))


def _line_for(attrs: dict[str, str], copies: int) -> Optional[ReceiptLineData]:
    unit_price = to_decimal(attrs.get("data-unit-price"))
    if unit_price is None:
        return None
    quantity = to_decimal(attrs.get("data-art-quantity")) or Decimal("1")
    fractional = quantity != quantity.to_integral_value()
    return ReceiptLineData(
        name=attrs["data-art-description"].strip(),
        quantity=quantity * copies,
        unit_price=money(unit_price),
        total=money(unit_price * quantity) * copies,
        unit="kg" if fractional else "szt",
        code=(attrs.get("data-art-id") or None),
    )


def parse_receipt_html(
    html: str, total: Optional[Decimal] = None
) -> tuple[list[ReceiptLineData], Decimal]:
    """(lines, discount total) from Lidl's printable receipt.

    `total` is the amount paid, when the ticket states it. It settles how
    many spans one printed article occupies when the counts alone allow
    more than one reading.
    """
    if not html:
        return [], Decimal("0")
    parser = _ReceiptHTML()
    parser.feed(html)

    counts: Counter = Counter()
    attrs_by_key: dict[tuple, dict[str, str]] = {}
    discounts: Counter = Counter()
    current: Optional[tuple] = None
    in_summary = False
    for kind, payload in parser.events:
        if kind == "article":
            key = _article_key(payload)
            attrs_by_key.setdefault(key, payload)
            counts[key] += 1
            current = key
            # Totals follow the last article; a marker word in the header
            # must not silence the rebates below every article after it.
            in_summary = False
            continue
        lowered = payload.lower()
        if any(marker in lowered for marker in _SUMMARY_MARKERS):
            in_summary = True
        if in_summary or current is None:
            continue
        for match in _NEGATIVE_AMOUNT.finditer(payload):
            discounts[current] += to_decimal(match.group(1)) or Decimal("0")

    if not counts:
        return [], Decimal("0")

    def build(factor: int) -> list[ReceiptLineData]:
        lines = []
        for key, count in counts.items():
            line = _line_for(attrs_by_key[key], max(count // factor, 1))
            if line is None:
                continue
            line.discount = money(discounts.get(key, Decimal("0")))
            lines.append(line)
        return lines

    shared = 0
    for count in counts.values():
        shared = math.gcd(shared, count)
    factors = [f for f in range(shared, 0, -1) if shared % f == 0]
    lines = build(factors[0])
    if total is not None and len(factors) > 1:
        def distance(candidate: list[ReceiptLineData]) -> Decimal:
            net = sum((line.total - line.discount for line in candidate), Decimal("0"))
            return abs(net - total)

        best = min((build(f) for f in factors), key=distance)
        if distance(best) < distance(lines):
            lines = best

    shelf = sum((line.total for line in lines), Decimal("0"))
    parsed_discounts = sum((line.discount for line in lines), Decimal("0"))
    if total is not None and shelf - total > 0:
        discount_total = money(shelf - total)
    else:
        discount_total = money(parsed_discounts)
    return lines, discount_total


def _ticket(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("ticket")
    return nested if isinstance(nested, dict) else payload


def _store(ticket: dict) -> tuple[Optional[str], Optional[str]]:
    store = ticket.get("store")
    if not isinstance(store, dict):
        return None, None
    name = store.get("name") or None
    parts = [store.get("address"), store.get("postalCode"), store.get("locality")]
    address = ", ".join(str(p).strip() for p in parts if p and str(p).strip()) or None
    return name, address


def build_receipt(summary: ReceiptSummaryData, payload: Any) -> ReceiptData:
    ticket = _ticket(payload)
    total = to_decimal(ticket.get("totalAmount"))
    if total is None:
        total = summary.total
    html = ticket.get("htmlPrintedReceipt") or ticket.get("htmlReceipt") or ""
    lines, discount_total = parse_receipt_html(html, total)
    if total is None:
        total = sum((line.total - line.discount for line in lines), Decimal("0"))
    purchased_at = parse_datetime(ticket.get("date")) or summary.purchased_at
    if purchased_at is None:
        local_date = datetime.now(_WARSAW).date()
    elif purchased_at.tzinfo is None:
        local_date = purchased_at.date()
    else:
        local_date = purchased_at.astimezone(_WARSAW).date()
    store_name, store_address = _store(ticket)
    currency = ticket.get("currency")
    code = currency.get("code") if isinstance(currency, dict) else None
    return ReceiptData(
        external_id=summary.external_id,
        purchased_at=purchased_at,
        date=local_date,
        total=money(total),
        currency=(code or "PLN").upper()[:3],
        store_name=store_name or summary.store_name or "Lidl",
        store_address=store_address,
        discount_total=discount_total,
        lines=lines,
        raw_data={"ticket": ticket},
    )


def summary_from_ticket(ticket: dict) -> Optional[ReceiptSummaryData]:
    ticket_id = ticket.get("id")
    if not ticket_id:
        return None
    return ReceiptSummaryData(
        external_id=str(ticket_id),
        purchased_at=parse_datetime(ticket.get("date")),
        total=to_decimal(ticket.get("totalAmount")),
        raw=ticket,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def _token_request(http: httpx.AsyncClient, data: dict[str, str]) -> dict:
    try:
        response = await http.post(
            f"{AUTH_API}/connect/token",
            data=data,
            headers={"Authorization": f"Basic {_BASIC}"},
        )
    except httpx.HTTPError as exc:
        raise RetailProviderError(f"lidl_token: {exc}") from exc
    if response.status_code >= 500:
        raise RetailProviderError(f"lidl_token_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not payload.get("access_token"):
        raise RetailAuthError("token_rejected")
    expires_in = payload.get("expires_in")
    seconds = int(expires_in) if isinstance(expires_in, (int, float)) else 3600
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or data.get("refresh_token"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
    }


class LidlPlusClient(RetailClient):
    async def _refresh(self) -> None:
        refresh_token = self.credentials.get("refresh_token")
        if not refresh_token:
            raise RetailAuthError("missing_refresh_token")
        self.credentials.update(
            await _token_request(
                self._http, {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
        )

    async def _ensure_fresh(self) -> None:
        expires_at = parse_datetime(self.credentials.get("expires_at"))
        if (
            not self.credentials.get("access_token")
            or expires_at is None
            or expires_at - timedelta(seconds=60) <= datetime.now(timezone.utc)
        ):
            await self._refresh()

    async def _get(self, url: str, *, params=None, retry: bool = True) -> Any:
        await self._ensure_fresh()
        headers = {**APP_HEADERS, "Authorization": f"Bearer {self.credentials['access_token']}"}
        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise RetailProviderError(f"lidl: {exc}") from exc
        if response.status_code == 401:
            if not retry:
                raise RetailAuthError("unauthorized")
            await self._refresh()
            return await self._get(url, params=params, retry=False)
        if response.status_code >= 400:
            raise RetailProviderError(f"lidl_http_{response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise RetailProviderError("lidl_not_json") from exc

    async def list_receipts(
        self, *, known_ids: set[str], max_pages: int = 50
    ) -> list[ReceiptSummaryData]:
        found: list[ReceiptSummaryData] = []
        url = f"{TICKETS_API}/v2/{COUNTRY}/tickets"
        page, last_page = 1, 1
        while page <= min(last_page, max_pages):
            payload = await self._get(url, params={"pageNumber": page, "onlyFavorite": "false"})
            if not isinstance(payload, dict):
                break
            summaries = [
                s
                for s in (
                    summary_from_ticket(t)
                    for t in payload.get("tickets") or []
                    if isinstance(t, dict)
                )
                if s is not None
            ]
            found.extend(summaries)
            if not summaries or all(s.external_id in known_ids for s in summaries):
                break
            size = payload.get("size") or len(summaries)
            total_count = payload.get("totalCount") or 0
            if isinstance(size, int) and size > 0 and isinstance(total_count, int):
                last_page = max(1, math.ceil(total_count / size))
            page += 1
        return found

    async def fetch_receipt(self, summary: ReceiptSummaryData) -> Optional[ReceiptData]:
        payload = await self._get(f"{TICKETS_API}/v3/{COUNTRY}/tickets/{summary.external_id}")
        if not isinstance(payload, dict):
            return None
        return build_receipt(summary, payload)


class LidlPlusProvider(RetailProvider):
    merchant_keywords = ("lidl",)
    redirect_prefix = REDIRECT_URI

    @property
    def name(self) -> str:
        return "lidl_plus"

    @property
    def display_name(self) -> str:
        return "Lidl Plus"

    def start_authorization(self) -> PendingAuthorization:
        verifier, challenge = generate_pkce()
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "Country": COUNTRY,
            "language": f"{LANGUAGE}-{COUNTRY}",
        }
        return PendingAuthorization(
            auth_url=f"{AUTH_API}/connect/authorize?{urlencode(params)}",
            state={"code_verifier": verifier},
        )

    async def complete_authorization(
        self, state: dict[str, Any], pasted: str, http: httpx.AsyncClient
    ) -> dict:
        verifier = state.get("code_verifier")
        if not verifier:
            raise RetailAuthError("login_not_started")
        return await _token_request(
            http,
            {
                "grant_type": "authorization_code",
                "code": code_from_redirect(pasted),
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )

    async def credentials_from_refresh_token(
        self, refresh_token: str, http: httpx.AsyncClient
    ) -> dict:
        return await _token_request(
            http, {"grant_type": "refresh_token", "refresh_token": refresh_token.strip()}
        )

    def client(self, credentials: dict, http: httpx.AsyncClient) -> RetailClient:
        return LidlPlusClient(credentials, http)
