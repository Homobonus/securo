"""Allegro: purchases from the buyer side of allegro.pl.

The public Allegro REST API is a seller's API; nothing in it lists what an
account *bought*. The "Moje Allegro → Zakupy → Kupione" page reads its list
from `GET api.allegro.pl/myorder-api/myorders`, authenticated by the
browser's `QXLSESSID` session cookie, as the Home Assistant integration
github.com/Przemko92/home-assistant-allegro does. Unofficial, so it can
change without notice; the person pastes the cookie from a logged-in
browser and pastes a new one when the session ends.

One checkout ("order group") is paid in one go and lands in the bank as
one debit, so a group is one receipt: every offer is a line, and whatever
the payload says was paid on top of the offers (delivery, fees) becomes a
line too, so the receipt adds up to the debit it is paired with.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Iterable, Optional
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
    money,
    parse_datetime,
    to_decimal,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.allegro.pl"
LOGIN_URL = (
    "https://allegro.pl/logowanie?origin_url=%2Fmoje-allegro%2Fzakupy%2Fkupione"
)
COOKIE_NAME = "QXLSESSID"
PAGE_SIZE = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_WARSAW = ZoneInfo("Europe/Warsaw")
_COOKIE = re.compile(rf"{COOKIE_NAME}=([^;\s'\"]+)")

# Where a total or a delivery cost may sit. The myorders payload is not
# documented; these are tried in order and the receipt says what was found.
_TOTAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("totalCost",),
    ("totalPrice",),
    ("totalToPay",),
    ("summary", "totalToPay"),
    ("summary", "total"),
    ("payment", "paidAmount"),
    ("payment", "amount"),
)
_DELIVERY_PATHS: tuple[tuple[str, ...], ...] = (
    ("delivery", "cost"),
    ("delivery", "price"),
    ("delivery", "calculatedCost"),
    ("summary", "deliveryCost"),
)


def cookie_from_paste(value: str) -> str:
    """The QXLSESSID value from whatever was pasted.

    Accepts the bare value, `QXLSESSID=...`, a whole Cookie header, or a
    "copy as cURL" command that carries one.
    """
    raw = (value or "").strip()
    match = _COOKIE.search(raw)
    if match:
        return match.group(1)
    if raw and not any(ch in raw for ch in " ;=\n\t'\""):
        return raw
    raise RetailAuthError("missing_session_cookie")


def _dig(data: Any, path: Iterable[str]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _amount(value: Any) -> Optional[Decimal]:
    """A money value given as {"amount": "12.34"} or as a plain number."""
    if isinstance(value, dict):
        value = value.get("amount")
    return to_decimal(value)


def _first_amount(data: Any, paths: tuple[tuple[str, ...], ...]) -> Optional[Decimal]:
    for path in paths:
        amount = _amount(_dig(data, path))
        if amount is not None:
            return amount
    return None


def _currency(orders: list[dict]) -> str:
    for order in orders:
        for offer in order.get("offers") or []:
            code = _dig(offer, ("unitPrice", "currency"))
            if isinstance(code, str) and len(code) == 3:
                return code.upper()
    return "PLN"


def _orders(group: dict) -> list[dict]:
    """The group's orders that were not cancelled."""
    kept = []
    for order in group.get("myorders") or []:
        if not isinstance(order, dict):
            continue
        status = str(_dig(order, ("status", "primary", "status")) or "")
        if "CANCEL" in status.upper():
            continue
        kept.append(order)
    return kept


def _offer_lines(orders: list[dict]) -> list[ReceiptLineData]:
    lines: list[ReceiptLineData] = []
    for order in orders:
        for offer in order.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            title = str(offer.get("title") or "").strip()
            unit_price = _amount(offer.get("unitPrice"))
            if not title or unit_price is None:
                continue
            quantity = to_decimal(offer.get("quantity")) or Decimal("1")
            lines.append(
                ReceiptLineData(
                    name=title[:255],
                    quantity=quantity,
                    unit_price=money(unit_price),
                    total=money(unit_price * quantity),
                    unit="szt",
                    code=str(offer["id"])[:64] if offer.get("id") else None,
                )
            )
    return lines


def _order_total(order: dict, offers_value: Decimal) -> Optional[Decimal]:
    total = _first_amount(order, _TOTAL_PATHS)
    if total is not None:
        return total
    delivery = _first_amount(order, _DELIVERY_PATHS)
    return offers_value + delivery if delivery is not None else None


def build_receipt(group: dict) -> Optional[ReceiptData]:
    group_id = str(group.get("groupId") or "")
    orders = _orders(group)
    if not group_id or not orders:
        return None
    lines = _offer_lines(orders)
    offers_value = sum((line.total for line in lines), Decimal("0"))

    total = _first_amount(group, _TOTAL_PATHS)
    if total is None:
        per_order = []
        for order in orders:
            value = sum(
                (line.total for line in _offer_lines([order])), Decimal("0")
            )
            per_order.append(_order_total(order, value))
        if per_order and all(t is not None for t in per_order):
            total = sum((t for t in per_order if t is not None), Decimal("0"))
    if total is None:
        total = offers_value

    discount_total = Decimal("0")
    gap = money(total - offers_value)
    if gap > 0:
        # Paid on top of the offers: delivery and fees.
        lines.append(
            ReceiptLineData(
                name="Dostawa i opłaty", quantity=Decimal("1"), unit_price=gap, total=gap, unit="szt"
            )
        )
    elif gap < 0:
        discount_total = -gap

    dates = [d for d in (parse_datetime(o.get("orderDate")) for o in orders) if d]
    purchased_at = min(dates) if dates else None
    if purchased_at is None:
        return None
    sellers = sorted(
        {str(_dig(o, ("seller", "login")) or "").strip() for o in orders} - {""}
    )
    local = purchased_at.astimezone(_WARSAW) if purchased_at.tzinfo else purchased_at
    return ReceiptData(
        external_id=group_id,
        purchased_at=purchased_at,
        date=local.date(),
        total=money(total),
        currency=_currency(orders),
        store_name="Allegro",
        store_address=", ".join(sellers)[:500] or None,
        discount_total=money(discount_total),
        lines=lines,
        raw_data={"group": group},
    )


def _headers(cookie: str, version: int) -> dict[str, str]:
    return {
        "Cookie": f"{COOKIE_NAME}={cookie}",
        "Accept": f"application/vnd.allegro.public.v{version}+json",
        "Referer": "https://allegro.pl/",
        "User-Agent": USER_AGENT,
        "Accept-Language": "pl-PL",
    }


async def _get(http: httpx.AsyncClient, cookie: str, path: str, version: int, params=None) -> Any:
    try:
        response = await http.get(f"{API_URL}{path}", params=params, headers=_headers(cookie, version))
    except httpx.HTTPError as exc:
        raise RetailProviderError(f"allegro: {type(exc).__name__} {exc}".strip()) from exc
    if response.status_code in (401, 403):
        raise RetailAuthError("session_expired")
    if response.status_code >= 400:
        raise RetailProviderError(f"allegro_http_{response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise RetailProviderError("allegro_not_json") from exc


class AllegroClient(RetailClient):
    async def list_receipts(
        self, *, known_ids: set[str], max_pages: int = 50
    ) -> list[ReceiptSummaryData]:
        cookie = self.credentials.get("cookie")
        if not cookie:
            raise RetailAuthError("missing_session_cookie")
        found: list[ReceiptSummaryData] = []
        seen: set[str] = set()
        for page in range(max_pages):
            payload = await _get(
                self._http,
                cookie,
                "/myorder-api/myorders",
                3,
                params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            groups = [
                g for g in (payload.get("orderGroups") if isinstance(payload, dict) else None) or []
                if isinstance(g, dict) and g.get("groupId")
            ]
            fresh = [g for g in groups if str(g["groupId"]) not in seen]
            if not fresh:
                break  # empty page, or the endpoint ignoring the offset
            for group in fresh:
                group_id = str(group["groupId"])
                seen.add(group_id)
                dates = [
                    d
                    for d in (parse_datetime(o.get("orderDate")) for o in _orders(group))
                    if d
                ]
                found.append(
                    ReceiptSummaryData(
                        external_id=group_id,
                        purchased_at=min(dates) if dates else None,
                        raw=group,
                    )
                )
            if all(str(g["groupId"]) in known_ids for g in fresh) or len(groups) < PAGE_SIZE:
                break
        return found

    async def fetch_receipt(self, summary: ReceiptSummaryData) -> Optional[ReceiptData]:
        # The list already carries everything a receipt needs.
        return build_receipt(summary.raw)


class AllegroProvider(RetailProvider):
    merchant_keywords = ("allegro", "payu")
    redirect_prefix = COOKIE_NAME
    auth_kind = "cookie"

    @property
    def name(self) -> str:
        return "allegro"

    @property
    def display_name(self) -> str:
        return "Allegro"

    def start_authorization(self) -> PendingAuthorization:
        return PendingAuthorization(auth_url=LOGIN_URL, state={})

    async def complete_authorization(
        self, state: dict[str, Any], pasted: str, http: httpx.AsyncClient
    ) -> dict:
        cookie = cookie_from_paste(pasted)
        payload = await _get(http, cookie, "/users", 2)
        login = _dig(payload, ("accounts", "allegro", "login"))
        return {"cookie": cookie, "login": login if isinstance(login, str) else None}

    async def credentials_from_refresh_token(
        self, refresh_token: str, http: httpx.AsyncClient
    ) -> dict:
        return await self.complete_authorization({}, refresh_token, http)

    def client(self, credentials: dict, http: httpx.AsyncClient) -> RetailClient:
        return AllegroClient(credentials, http)

    def receipt_from_raw(self, external_id: str, raw_data: dict) -> Optional[ReceiptData]:
        group = raw_data.get("group")
        return build_receipt(group) if isinstance(group, dict) else None
