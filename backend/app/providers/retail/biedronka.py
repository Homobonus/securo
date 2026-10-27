"""Moja Biedronka: the loyalty app of the Biedronka chain.

Unofficial: this is the private API of the Android app, as documented by
the Home Assistant integration (github.com/Przemko92/home-assistant-
mojabiedronka), whose constants and payload handling this follows.

Sign-in is a Keycloak PKCE flow for the public `cma20` client. The login
page carries a Cloudflare captcha and an SMS code, so the person signs in
in their own browser; Keycloak then redirects to
`app://cma20.biedronka.pl?code=...`, which they paste back. The code
lives for about a minute.

E-receipts are fiscal JSON with every amount in grosze: `sellLine` for a
product, `discountLine` for a rebate on the line above it, either one
possibly a storno of an earlier line.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
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

API_BASE = "https://api.prod.biedronka.cloud/api/v7"
AUTH_BASE = "https://konto.biedronka.pl"
REALM = "loyalty"
CLIENT_ID = "cma20"
REDIRECT_URI = "app://cma20.biedronka.pl"
AUTH_URL = f"{AUTH_BASE}/realms/{REALM}/protocol/openid-connect/auth"
TOKEN_URL = f"{AUTH_BASE}/realms/{REALM}/protocol/openid-connect/token"

# Lowest app version known to work. The API names a newer one with HTTP
# 426 when it stops accepting this, and the client adopts it.
APP_VERSION = "2.56.0"
MIN_VERSION_HEADER = "min-supported-version-android"
KEYCLOAK_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
_WARSAW = ZoneInfo("Europe/Warsaw")
_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _version_tuple(version: Optional[str]) -> tuple[int, ...]:
    if not version or not _VERSION.fullmatch(version.strip()):
        return ()
    return tuple(int(part) for part in version.strip().split("."))


def _jwt_exp(token: Optional[str]) -> Optional[float]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        exp = json.loads(base64.urlsafe_b64decode(padded.encode("ascii"))).get("exp")
    except (ValueError, json.JSONDecodeError):
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_LINE_TYPES = ("sellLine", "discountLine", "discountSummary", "vatSummary", "sumInCurrency")


def _as_json(payload: Any) -> Any:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


def extract_lines(payload: Any) -> list[dict]:
    """The fiscal line list, wherever this response version nests it."""
    payload = _as_json(payload)
    if isinstance(payload, list):
        if any(isinstance(i, dict) and any(k in i for k in _LINE_TYPES) for i in payload):
            return [i for i in payload if isinstance(i, dict)]
        for item in payload:
            found = extract_lines(item)
            if found:
                return found
        return []
    if not isinstance(payload, dict):
        return []
    for key in ("lines", "receiptLines"):
        if isinstance(payload.get(key), list):
            return [i for i in payload[key] if isinstance(i, dict)]
    for key in ("receipt", "data", "payload", "json"):
        if payload.get(key) is not None:
            found = extract_lines(payload[key])
            if found:
                return found
    return []


def _quantity(value: Any) -> Decimal:
    quantity = to_decimal(value)
    return quantity if quantity is not None and quantity > 0 else Decimal("1")


def _unit(raw: Any, quantity: Decimal) -> Optional[str]:
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()[:10]
    return "kg" if quantity != quantity.to_integral_value() else "szt"


def parse_e_receipt(payload: Any) -> tuple[list[ReceiptLineData], Optional[Decimal], Decimal]:
    """(lines, fiscal total, discount total) from an e-receipt. Amounts in grosze."""
    items: list[ReceiptLineData] = []
    total: Optional[Decimal] = None
    discount_summary: Optional[Decimal] = None
    for line in extract_lines(payload):
        sell = line.get("sellLine")
        if isinstance(sell, dict):
            name = str(sell.get("name") or "").strip()
            line_total = to_decimal(sell.get("total"), cents=True)
            if not name or line_total is None:
                continue
            quantity = _quantity(sell.get("quantity"))
            if sell.get("isStorno"):
                # A storno cancels the newest matching line instead of
                # standing as a line of its own.
                for idx in range(len(items) - 1, -1, -1):
                    if items[idx].name == name and money(items[idx].total) == money(line_total):
                        del items[idx]
                        break
                continue
            unit_price = to_decimal(sell.get("price"), cents=True)
            if unit_price is None:
                unit_price = line_total / quantity
            items.append(
                ReceiptLineData(
                    name=name,
                    quantity=quantity,
                    unit_price=money(unit_price),
                    total=money(line_total),
                    unit=_unit(sell.get("unit"), quantity),
                    code=str(sell["ean"])[:64] if sell.get("ean") else None,
                )
            )
            continue
        discount = line.get("discountLine")
        if isinstance(discount, dict):
            value = to_decimal(discount.get("value"), cents=True)
            if (
                value is None
                or not items
                or discount.get("isStorno")
                or discount.get("isDiscount") is False
            ):
                continue
            items[-1].discount = money(items[-1].discount + abs(value))
            continue
        summary = line.get("discountSummary")
        if isinstance(summary, dict):
            discount_summary = to_decimal(summary.get("discounts"), cents=True)
            continue
        fiscal = line.get("sumInCurrency")
        if isinstance(fiscal, dict):
            total = to_decimal(fiscal.get("fiscalTotal"), cents=True)
    discount_total = (
        money(abs(discount_summary))
        if discount_summary is not None
        else money(sum((i.discount for i in items), Decimal("0")))
    )
    return items, (money(total) if total is not None else None), discount_total


_UNITS = {"szt": "szt", "opa": "szt", "kg": "kg"}


def parse_details_items(payload: Any) -> list[ReceiptLineData]:
    """Line items from the transaction detail, for receipts without an e-receipt.

    Amounts are in PLN. Each item states its value before and after its
    rebates (`total_price_without_discount`, `total_price`); the line keeps
    the shelf value and the rebate apart, as an e-receipt does.
    """
    items: list[ReceiptLineData] = []
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        quantity = _quantity(raw.get("quantity"))
        unit_price = to_decimal(raw.get("unit_price") or raw.get("price"))
        paid = to_decimal(raw.get("total_price") or raw.get("total"))
        shelf = to_decimal(raw.get("total_price_without_discount"))
        if not name or (unit_price is None and paid is None and shelf is None):
            continue
        if shelf is None:
            shelf = paid if paid is not None else (unit_price or Decimal("0")) * quantity
        rebate = to_decimal(raw.get("total_discount"))
        if rebate is None:
            rebate = shelf - paid if paid is not None else Decimal("0")
        if unit_price is None:
            unit_price = shelf / quantity
        measure = str(raw.get("measure_unit") or raw.get("unit") or "").strip().lower()
        items.append(
            ReceiptLineData(
                name=name,
                quantity=quantity,
                unit_price=money(unit_price),
                total=money(shelf),
                discount=money(abs(rebate)),
                unit=_UNITS.get(measure) or _unit(None, quantity),
                code=str(raw["ean"])[:64] if raw.get("ean") else None,
            )
        )
    return items


def _store(summary: ReceiptSummaryData, payload: Any) -> tuple[str, Optional[str]]:
    """("Biedronka <city>", street address). The list calls the address its store name."""
    store = payload.get("store") if isinstance(payload, dict) else None
    if isinstance(store, dict) and (store.get("street") or store.get("city")):
        city = str(store.get("city") or "").strip()
        place = " ".join(p for p in (store.get("zip_code"), city) if p)
        address = ", ".join(p for p in (store.get("street"), place) if p)
        return (f"Biedronka {city}".strip(), address or None)
    if summary.store_name:
        return "Biedronka", summary.store_name
    return "Biedronka", None


def build_receipt(summary: ReceiptSummaryData, payload: Any, *, e_receipt: bool) -> ReceiptData:
    if e_receipt:
        lines, fiscal_total, discount_total = parse_e_receipt(payload)
    else:
        lines, fiscal_total = parse_details_items(payload), None
        discount_total = money(sum((i.discount for i in lines), Decimal("0")))
    total = fiscal_total if fiscal_total is not None else summary.total
    if total is None:
        total = money(sum((i.total - i.discount for i in lines), Decimal("0")))
    purchased_at = summary.purchased_at
    local_date = (
        purchased_at.astimezone(_WARSAW).date()
        if purchased_at and purchased_at.tzinfo
        else (purchased_at.date() if purchased_at else datetime.now(_WARSAW).date())
    )
    store_name, store_address = _store(summary, payload)
    return ReceiptData(
        external_id=summary.external_id,
        purchased_at=purchased_at,
        date=local_date,
        total=money(total),
        currency="PLN",
        store_name=store_name,
        store_address=store_address,
        discount_total=discount_total,
        lines=lines,
        raw_data={"transaction": summary.raw, "receipt": _as_json(payload)},
    )


def summary_from_transaction(tx: dict) -> Optional[ReceiptSummaryData]:
    tx_id = tx.get("id")
    if tx_id is None:
        return None
    return ReceiptSummaryData(
        external_id=str(tx_id),
        purchased_at=parse_datetime(tx.get("date")),
        total=to_decimal(tx.get("total_price")),
        store_name=tx.get("store_name"),
        raw=tx,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def _token_request(http: httpx.AsyncClient, data: dict[str, str]) -> dict:
    try:
        response = await http.post(
            TOKEN_URL,
            data=data,
            headers={"User-Agent": KEYCLOAK_USER_AGENT},
        )
    except httpx.HTTPError as exc:
        raise RetailProviderError(f"biedronka_token: {type(exc).__name__} {exc}".strip()) from exc
    if response.status_code >= 500:
        raise RetailProviderError(f"biedronka_token_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not payload.get("access_token"):
        raise RetailAuthError("token_rejected")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or data.get("refresh_token"),
    }


class BiedronkaClient(RetailClient):
    async def _refresh(self) -> None:
        refresh_token = self.credentials.get("refresh_token")
        if not refresh_token:
            raise RetailAuthError("missing_refresh_token")
        tokens = await _token_request(
            self._http,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "refresh_token": refresh_token,
            },
        )
        self.credentials.update(tokens)

    async def _ensure_fresh(self) -> None:
        exp = _jwt_exp(self.credentials.get("access_token"))
        if exp is None or exp - 60 <= datetime.now().timestamp():
            await self._refresh()

    async def _get(self, path: str, *, params=None, headers=None, retry: bool = True) -> Any:
        await self._ensure_fresh()
        version = self.credentials.get("app_version") or APP_VERSION
        request_headers = {
            "Authorization": f"Bearer {self.credentials.get('access_token')}",
            "User-Agent": f"Android/{version}",
            "Accept-Language": "pl-PL",
            "Accept": "application/json",
            **(headers or {}),
        }
        try:
            response = await self._http.get(
                f"{API_BASE}/{path.lstrip('/')}", params=params, headers=request_headers
            )
        except httpx.HTTPError as exc:
            raise RetailProviderError(f"biedronka: {type(exc).__name__} {exc}".strip()) from exc
        if response.status_code == 401:
            if not retry:
                raise RetailAuthError("unauthorized")
            await self._refresh()
            return await self._get(path, params=params, headers=headers, retry=False)
        if response.status_code == 426:
            wanted = response.headers.get(MIN_VERSION_HEADER) or ""
            match = _VERSION.search(wanted) or _VERSION.search(response.text or "")
            if match and _version_tuple(match.group(0)) > _version_tuple(version):
                logger.warning("Biedronka API requires app version %s", match.group(0))
                self.credentials["app_version"] = match.group(0)
                return await self._get(path, params=params, headers=headers, retry=retry)
            raise RetailProviderError("biedronka_upgrade_required")
        if response.status_code >= 400:
            raise RetailProviderError(f"biedronka_http_{response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return _as_json(response.text)

    async def list_receipts(
        self, *, known_ids: set[str], max_pages: int = 50
    ) -> list[ReceiptSummaryData]:
        found: list[ReceiptSummaryData] = []
        page, seen_pages = 1, set()
        while page and page not in seen_pages and len(seen_pages) < max_pages:
            seen_pages.add(page)
            payload = await self._get("transactions/", params={"page": page})
            if not isinstance(payload, dict):
                break
            summaries = [
                s
                for s in (
                    summary_from_transaction(tx)
                    for tx in payload.get("transactions") or []
                    if isinstance(tx, dict)
                )
                if s is not None
            ]
            found.extend(summaries)
            if not summaries or all(s.external_id in known_ids for s in summaries):
                break
            next_page = payload.get("next_page")
            page = int(next_page) if isinstance(next_page, (int, str)) and str(next_page).isdigit() else 0
        return found

    async def fetch_receipt(self, summary: ReceiptSummaryData) -> Optional[ReceiptData]:
        tx_id = summary.external_id
        if summary.raw.get("is_e_receipt_available", True):
            try:
                payload = await self._get(
                    f"transactions/{tx_id}/e-receipt/", headers={"output-format": "json"}
                )
                if extract_lines(payload):
                    return build_receipt(summary, payload, e_receipt=True)
            except RetailProviderError as exc:
                logger.debug("Biedronka e-receipt %s unavailable: %s", tx_id, exc)
        payload = await self._get(f"transactions/{tx_id}/")
        if payload is None:
            return None
        return build_receipt(summary, payload, e_receipt=False)


class BiedronkaProvider(RetailProvider):
    merchant_keywords = ("biedronka", "jeronimo")
    redirect_prefix = REDIRECT_URI

    @property
    def name(self) -> str:
        return "biedronka"

    @property
    def display_name(self) -> str:
        return "Moja Biedronka"

    def start_authorization(self) -> PendingAuthorization:
        verifier, challenge = generate_pkce()
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return PendingAuthorization(
            auth_url=f"{AUTH_URL}?{urlencode(params)}", state={"code_verifier": verifier}
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
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": code_from_redirect(pasted),
                "code_verifier": verifier,
            },
        )

    async def credentials_from_refresh_token(
        self, refresh_token: str, http: httpx.AsyncClient
    ) -> dict:
        return await _token_request(
            http,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "refresh_token": refresh_token.strip(),
            },
        )

    def client(self, credentials: dict, http: httpx.AsyncClient) -> RetailClient:
        return BiedronkaClient(credentials, http)

    def receipt_from_raw(self, external_id: str, raw_data: dict) -> Optional[ReceiptData]:
        tx = raw_data.get("transaction")
        summary = summary_from_transaction(tx) if isinstance(tx, dict) else None
        payload = raw_data.get("receipt")
        if summary is None or payload is None:
            return None
        return build_receipt(summary, payload, e_receipt=bool(extract_lines(payload)))
