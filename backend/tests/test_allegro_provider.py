"""Allegro purchases, read from the buyer's myorders list.

The payload shape follows the Home Assistant integration's parser; totals
are looked for in several places because the endpoint is undocumented.
Every request is answered in-process.
"""
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.providers.retail import allegro
from app.providers.retail.base import RetailAuthError, new_http_client


def offer(title, price, quantity=1, offer_id="1"):
    return {"id": offer_id, "title": title, "quantity": quantity, "unitPrice": {"amount": price, "currency": "PLN"}}


def order(seller, offers, status="DELIVERED", date_="2026-10-20T10:15:00.000Z", **extra):
    return {
        "seller": {"login": seller},
        "offers": offers,
        "orderDate": date_,
        "status": {"primary": {"status": status}},
        **extra,
    }


def group(group_id, orders, **extra):
    return {"groupId": group_id, "myorders": orders, **extra}


class TestCookie:
    @pytest.mark.parametrize(
        "pasted",
        [
            "abc123",
            "QXLSESSID=abc123",
            "  QXLSESSID=abc123; other=1  ",
            "curl 'https://allegro.pl/' -H 'cookie: x=1; QXLSESSID=abc123; y=2'",
        ],
    )
    def test_every_way_of_pasting_it(self, pasted):
        assert allegro.cookie_from_paste(pasted) == "abc123"

    @pytest.mark.parametrize("pasted", ["", "some words here", "other=1"])
    def test_nothing_usable_is_refused(self, pasted):
        with pytest.raises(RetailAuthError):
            allegro.cookie_from_paste(pasted)


class TestReceipt:
    def test_one_checkout_is_one_receipt_with_every_offer(self):
        receipt = allegro.build_receipt(
            group("g1", [
                order("shop_a", [offer("Kabel USB-C", "19.99", 2, "11")]),
                order("shop_b", [offer("Etui", "29.00")]),
            ])
        )
        assert receipt is not None
        assert receipt.external_id == "g1"
        assert [(i.name, i.quantity, i.total, i.code) for i in receipt.lines] == [
            ("Kabel USB-C", Decimal("2"), Decimal("39.98"), "11"),
            ("Etui", Decimal("1"), Decimal("29.00"), "1"),
        ]
        assert receipt.total == Decimal("68.98")
        assert receipt.store_name == "Allegro"
        assert receipt.store_address == "shop_a, shop_b"
        # 10:15 UTC on 20 October is still the 20th in Warsaw.
        assert receipt.date == date(2026, 10, 20)

    def test_delivery_on_top_becomes_a_line_so_it_matches_the_bank(self):
        receipt = allegro.build_receipt(
            group("g1", [order("shop", [offer("Kawa 1kg", "59.99")])], totalCost={"amount": "68.98", "currency": "PLN"})
        )
        assert receipt is not None
        assert receipt.total == Decimal("68.98")
        assert receipt.lines[-1].name == "Dostawa i opłaty" and receipt.lines[-1].total == Decimal("8.99")
        assert sum(i.total - i.discount for i in receipt.lines) == receipt.total

    def test_per_order_delivery_costs_add_up(self):
        receipt = allegro.build_receipt(
            group("g1", [
                order("a", [offer("A", "10.00")], delivery={"cost": {"amount": "5.00"}}),
                order("b", [offer("B", "20.00")], totalCost={"amount": "23.99"}),
            ])
        )
        assert receipt is not None and receipt.total == Decimal("38.99")

    def test_paying_less_than_the_offers_is_a_discount(self):
        receipt = allegro.build_receipt(
            group("g1", [order("shop", [offer("Smart!", "50.00")])], summary={"totalToPay": {"amount": "45.00"}})
        )
        assert receipt is not None
        assert (receipt.total, receipt.discount_total) == (Decimal("45.00"), Decimal("5.00"))

    def test_cancelled_orders_are_left_out(self):
        assert allegro.build_receipt(group("g1", [order("s", [offer("X", "1.00")], status="CANCELLED")])) is None
        receipt = allegro.build_receipt(
            group("g2", [order("s", [offer("X", "1.00")], status="CANCELLED"), order("t", [offer("Y", "2.00")])])
        )
        assert receipt is not None and [i.name for i in receipt.lines] == ["Y"]

    def test_raw_payload_rebuilds_the_same_receipt(self):
        raw = group("g1", [order("shop", [offer("Kawa", "59.99")])])
        receipt = allegro.build_receipt(raw)
        assert receipt is not None and receipt.raw_data is not None
        again = allegro.AllegroProvider().receipt_from_raw("g1", receipt.raw_data)
        assert again is not None and again.total == receipt.total


def store(pages: list[list[dict]], *, status: int = 200, ignore_offset: bool = False):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Cookie"] == "QXLSESSID=abc"
        if status != 200:
            return httpx.Response(status)
        if request.url.path == "/users":
            assert request.headers["Accept"] == "application/vnd.allegro.public.v2+json"
            return httpx.Response(200, json={"accounts": {"allegro": {"login": "kupujacy"}}})
        assert request.headers["Accept"] == "application/vnd.allegro.public.v3+json"
        offset = 0 if ignore_offset else int(request.url.params["offset"])
        index = offset // allegro.PAGE_SIZE
        return httpx.Response(200, json={"orderGroups": pages[index] if index < len(pages) else []})

    return handler, calls


def full_page(prefix: str) -> list[dict]:
    return [group(f"{prefix}{i}", [order("s", [offer("X", "1.00")])]) for i in range(allegro.PAGE_SIZE)]


class TestClient:
    @pytest.mark.asyncio
    async def test_pages_through_the_whole_history(self):
        handler, calls = store([full_page("a"), [group("b0", [order("s", [offer("X", "1.00")])])]])
        async with new_http_client(httpx.MockTransport(handler)) as http:
            found = await allegro.AllegroClient({"cookie": "abc"}, http).list_receipts(known_ids=set())
        assert len(found) == allegro.PAGE_SIZE + 1
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_stops_when_the_offset_is_ignored(self):
        handler, calls = store([full_page("a")], ignore_offset=True)
        async with new_http_client(httpx.MockTransport(handler)) as http:
            found = await allegro.AllegroClient({"cookie": "abc"}, http).list_receipts(known_ids=set())
        assert len(found) == allegro.PAGE_SIZE and len(calls) == 2

    @pytest.mark.asyncio
    async def test_stops_at_a_page_of_known_purchases(self):
        handler, calls = store([full_page("a"), full_page("b")])
        known = {f"a{i}" for i in range(allegro.PAGE_SIZE)}
        async with new_http_client(httpx.MockTransport(handler)) as http:
            await allegro.AllegroClient({"cookie": "abc"}, http).list_receipts(known_ids=known)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_an_ended_session_needs_a_new_cookie(self):
        handler, _ = store([], status=401)
        async with new_http_client(httpx.MockTransport(handler)) as http:
            with pytest.raises(RetailAuthError):
                await allegro.AllegroClient({"cookie": "abc"}, http).list_receipts(known_ids=set())

    @pytest.mark.asyncio
    async def test_signing_in_checks_the_cookie(self):
        handler, _ = store([])
        async with new_http_client(httpx.MockTransport(handler)) as http:
            credentials = await allegro.AllegroProvider().complete_authorization({}, "QXLSESSID=abc", http)
        assert credentials == {"cookie": "abc", "login": "kupujacy"}
