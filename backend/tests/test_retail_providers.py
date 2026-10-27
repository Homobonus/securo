"""Loyalty-app providers: reading what the stores send, and talking to them.

Every request is answered by an in-process transport; nothing here
reaches Lidl or Biedronka.
"""
import base64
import json
import time
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.providers.retail import biedronka, lidl_plus
from app.providers.retail.base import (
    RetailAuthError,
    RetailProviderError,
    ReceiptSummaryData,
    code_from_redirect,
    new_http_client,
    to_decimal,
)


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
class TestCodeFromRedirect:
    def test_the_bare_redirect(self):
        assert code_from_redirect("app://cma20.biedronka.pl?code=abc.def&session_state=x") == "abc.def"

    def test_the_console_line_chrome_prints(self):
        line = (
            "Failed to launch 'com.lidlplus.app://callback?code=4F2A&scope=openid' "
            "because the scheme does not have a registered handler."
        )
        assert code_from_redirect(line) == "4F2A"

    def test_the_code_alone(self):
        assert code_from_redirect("  4F2A9B  ") == "4F2A9B"

    def test_an_address_without_a_code_is_refused(self):
        with pytest.raises(RetailAuthError):
            code_from_redirect("app://cma20.biedronka.pl?error=access_denied")

    def test_nothing_is_refused(self):
        with pytest.raises(RetailAuthError):
            code_from_redirect("   ")


def test_polish_decimals_and_grosze():
    assert to_decimal("7,99") == Decimal("7.99")
    assert to_decimal("0,598") == Decimal("0.598")
    assert to_decimal(849, cents=True) == Decimal("8.49")
    assert to_decimal("abc") is None
    assert to_decimal(True) is None


# ---------------------------------------------------------------------------
# Biedronka: parsing
# ---------------------------------------------------------------------------
# Shape of a real e-receipt as served with `output-format: json`, from the
# Home Assistant integration's test fixtures, with a storno and a weighed
# line added.
E_RECEIPT = {
    "nip": "1138492354",
    "lines": [
        {"headerLine": {"date": "2026-08-29", "time": "11:24"}},
        {
            "sellLine": {
                "name": "PapierQueen8rolek",
                "vatId": "A",
                "price": 849,
                "total": 5094,
                "quantity": "6",
                "isStorno": False,
                "unit": "szt",
                "ean": "5900000000000",
            }
        },
        {
            "discountLine": {
                "base": 5094,
                "value": 1698,
                "isDiscount": True,
                "isPercent": False,
                "isStorno": False,
            }
        },
        {"sellLine": {"name": "Banany", "price": 599, "total": 307, "quantity": "0.512"}},
        {"sellLine": {"name": "Cola 2l", "price": 799, "total": 799, "quantity": "1"}},
        {"sellLine": {"name": "Cola 2l", "price": 799, "total": 799, "quantity": "1", "isStorno": True}},
        {"paymentLine": {"type": "card", "value": 3703}},
        {"discountSummary": {"discounts": 1698}},
        {"sumInCurrency": {"fiscalTotal": 3703, "currency": "PLN"}},
    ],
}


class TestBiedronkaParsing:
    def test_lines_amounts_and_totals(self):
        lines, total, discounts = biedronka.parse_e_receipt(E_RECEIPT)
        assert [(line.name, line.quantity, line.unit_price, line.total, line.discount) for line in lines] == [
            ("PapierQueen8rolek", Decimal("6"), Decimal("8.49"), Decimal("50.94"), Decimal("16.98")),
            ("Banany", Decimal("0.512"), Decimal("5.99"), Decimal("3.07"), Decimal("0")),
        ]
        assert total == Decimal("37.03")
        assert discounts == Decimal("16.98")

    def test_units_and_codes(self):
        lines, _, _ = biedronka.parse_e_receipt(E_RECEIPT)
        assert lines[0].unit == "szt" and lines[0].code == "5900000000000"
        assert lines[1].unit == "kg"

    def test_lines_are_found_when_nested(self):
        lines, total, _ = biedronka.parse_e_receipt({"receipt": json.dumps(E_RECEIPT)})
        assert len(lines) == 2 and total == Decimal("37.03")

    def test_receipt_date_is_the_warsaw_calendar_day(self):
        summary = biedronka.summary_from_transaction(
            {"id": 77, "date": "2026-08-29T22:30:00+00:00", "total_price": "37.03", "store_name": "Biedronka Kraków"}
        )
        receipt = biedronka.build_receipt(summary, E_RECEIPT, e_receipt=True)
        assert receipt.date == date(2026, 8, 30)
        assert receipt.external_id == "77"
        assert receipt.store_name == "Biedronka Kraków"

    def test_plain_details_when_there_is_no_e_receipt(self):
        summary = biedronka.summary_from_transaction({"id": 5, "date": "2026-08-29T10:00:00+02:00", "total_price": 12.5})
        payload = {"items": [{"name": "Chleb", "unit_price": 4.5, "total_price": 9.0, "quantity": 2}, {"name": "Masło", "unit_price": "3,50"}]}
        receipt = biedronka.build_receipt(summary, payload, e_receipt=False)
        assert [(i.name, i.quantity, i.total) for i in receipt.lines] == [
            ("Chleb", Decimal("2"), Decimal("9.00")),
            ("Masło", Decimal("1"), Decimal("3.50")),
        ]
        assert receipt.total == Decimal("12.50")


# ---------------------------------------------------------------------------
# Lidl: parsing
# ---------------------------------------------------------------------------
def article(art_id, name, price, quantity=None):
    qty = f' data-art-quantity="{quantity}"' if quantity is not None else ""
    attrs = f'data-art-id="{art_id}" data-art-description="{name}" data-unit-price="{price}"{qty} data-tax-type="A"'
    # Lidl prints every article twice: the name line and the price line.
    return (
        f'<span class="article" {attrs}>{name}</span>'
        f'<span class="article css_right" {attrs}>{price}</span><br/>'
    )


def receipt_html(*parts: str) -> str:
    return (
        '<div class="receipt"><span class="css_bold">Lidl sp. z o.o.</span><br/>'
        + "".join(parts)
        + '<span class="css_bold">SUMA PLN</span><span id="purchase_summary_3" class="css_bold">0,00</span>'
        '<span>Rabaty razem -9,99</span></div>'
    )


class TestLidlParsing:
    def test_each_article_is_printed_twice_but_counted_once(self):
        html = receipt_html(article("101", "Kawa ziarn.", "24,99"), article("102", "Precel z solą", "1,79"))
        lines, _ = lidl_plus.parse_receipt_html(html, Decimal("26.78"))
        assert [(line.name, line.quantity, line.total) for line in lines] == [
            ("Kawa ziarn.", Decimal("1"), Decimal("24.99")),
            ("Precel z solą", Decimal("1"), Decimal("1.79")),
        ]

    def test_the_same_product_scanned_twice_is_two_products(self):
        html = receipt_html(
            article("101", "Jogurt", "3,29"),
            article("102", "Bułka", "0,59"),
            article("101", "Jogurt", "3,29"),
        )
        lines, _ = lidl_plus.parse_receipt_html(html, Decimal("7.17"))
        assert [(line.name, line.quantity, line.total) for line in lines] == [
            ("Jogurt", Decimal("2"), Decimal("6.58")),
            ("Bułka", Decimal("1"), Decimal("0.59")),
        ]

    def test_the_total_settles_an_ambiguous_count(self):
        # One product, two spans: one article printed twice, or the same
        # article bought twice and printed once each? The total says.
        once = article("101", "Mango", "12,99")
        lines, _ = lidl_plus.parse_receipt_html(receipt_html(once), Decimal("12.99"))
        assert lines[0].quantity == Decimal("1")
        single = once.split("<br/>")[0].replace('class="article css_right"', 'class="article"')
        lines, _ = lidl_plus.parse_receipt_html(receipt_html(single + "<br/>" + single), Decimal("25.98"))
        assert lines[0].quantity == Decimal("2")

    def test_weighed_items(self):
        html = receipt_html(article("201", "Winogrona jas.bezp.", "7,99", "0,598"))
        lines, _ = lidl_plus.parse_receipt_html(html, Decimal("4.78"))
        assert (lines[0].quantity, lines[0].unit, lines[0].total) == (Decimal("0.598"), "kg", Decimal("4.78"))

    def test_a_rebate_below_an_article_belongs_to_it(self):
        html = receipt_html(
            article("101", "Kawa ziarn.", "24,99"),
            "<span>Lidl Plus kupon</span><span>-5,00</span><br/>",
            article("102", "Precel", "1,79"),
        )
        lines, discounts = lidl_plus.parse_receipt_html(html, Decimal("21.78"))
        assert [line.discount for line in lines] == [Decimal("5.00"), Decimal("0")]
        assert discounts == Decimal("5.00")

    def test_the_summary_is_not_a_rebate(self):
        lines, _ = lidl_plus.parse_receipt_html(receipt_html(article("1", "Ser", "5,00")), Decimal("5.00"))
        assert lines[0].discount == Decimal("0")

    def test_an_online_ticket_without_html(self):
        assert lidl_plus.parse_receipt_html("", Decimal("10")) == ([], Decimal("0"))

    def test_ticket_detail_to_receipt(self):
        summary = lidl_plus.summary_from_ticket({"id": "2400191683", "date": "2026-10-22T10:04:09+00:00", "totalAmount": 26.78})
        detail = {
            "id": "2400191683",
            "date": "2026-10-22T10:04:09+00:00",
            "totalAmount": 26.78,
            "currency": {"code": "PLN"},
            "store": {"name": "Lidl Kraków", "address": "ul. Wielicka 1", "postalCode": "30-552", "locality": "Kraków"},
            "htmlPrintedReceipt": receipt_html(article("101", "Kawa ziarn.", "24,99"), article("102", "Precel", "1,79")),
        }
        receipt = lidl_plus.build_receipt(summary, detail)
        assert receipt.total == Decimal("26.78")
        assert receipt.store_name == "Lidl Kraków"
        assert receipt.store_address == "ul. Wielicka 1, 30-552, Kraków"
        assert receipt.date == date(2026, 10, 22)
        assert len(receipt.lines) == 2

    def test_the_web_shop_shape_is_read_too(self):
        summary = ReceiptSummaryData(external_id="x")
        receipt = lidl_plus.build_receipt(
            summary, {"ticket": {"date": "2026-10-22T10:04:09", "totalAmount": "1,79", "htmlPrintedReceipt": receipt_html(article("1", "Precel", "1,79"))}}
        )
        assert receipt.total == Decimal("1.79") and receipt.lines[0].name == "Precel"


# ---------------------------------------------------------------------------
# Authorization links
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "provider, host, client_id",
    [
        (biedronka.BiedronkaProvider(), "konto.biedronka.pl", "cma20"),
        (lidl_plus.LidlPlusProvider(), "accounts.lidl.com", "LidlPlusNativeClient"),
    ],
)
def test_sign_in_link_carries_a_pkce_challenge(provider, host, client_id):
    pending = provider.start_authorization()
    url = urlparse(pending.auth_url)
    query = parse_qs(url.query)
    assert url.hostname == host
    assert query["client_id"] == [client_id]
    assert query["code_challenge_method"] == ["S256"]
    assert pending.state["code_verifier"] not in pending.auth_url


# ---------------------------------------------------------------------------
# Talking to the stores
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


class TestBiedronkaClient:
    @pytest.mark.asyncio
    async def test_pages_until_it_reaches_known_receipts(self):
        def handler(request):
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, json={"transactions": [{"id": 3}, {"id": 2}], "next_page": 2})
            return httpx.Response(200, json={"transactions": [{"id": 1}], "next_page": None})

        rec = Recorder(handler)
        async with new_http_client(httpx.MockTransport(rec)) as http:
            client = biedronka.BiedronkaProvider().client({"access_token": jwt(time.time() + 3600), "refresh_token": "r"}, http)
            found = await client.list_receipts(known_ids={"2"})
        assert [s.external_id for s in found] == ["3", "2", "1"]

        rec = Recorder(handler)
        async with new_http_client(httpx.MockTransport(rec)) as http:
            client = biedronka.BiedronkaProvider().client({"access_token": jwt(time.time() + 3600), "refresh_token": "r"}, http)
            found = await client.list_receipts(known_ids={"3", "2"})
        assert [s.external_id for s in found] == ["3", "2"]
        assert len(rec.requests) == 1

    @pytest.mark.asyncio
    async def test_an_expired_token_is_refreshed_and_kept(self):
        def handler(request):
            if request.url.host == "konto.biedronka.pl":
                form = parse_qs(request.content.decode())
                assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["old"]
                return httpx.Response(200, json={"access_token": jwt(time.time() + 3600), "refresh_token": "new"})
            assert request.headers["User-Agent"].startswith("Android/")
            return httpx.Response(200, json={"transactions": [], "next_page": None})

        credentials = {"access_token": jwt(time.time() - 10), "refresh_token": "old"}
        async with new_http_client(httpx.MockTransport(handler)) as http:
            client = biedronka.BiedronkaProvider().client(credentials, http)
            await client.list_receipts(known_ids=set())
        assert credentials["refresh_token"] == "new"

    @pytest.mark.asyncio
    async def test_a_revoked_refresh_token_needs_a_new_sign_in(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        async with new_http_client(transport) as http:
            client = biedronka.BiedronkaProvider().client({"access_token": "", "refresh_token": "old"}, http)
            with pytest.raises(RetailAuthError):
                await client.list_receipts(known_ids=set())

    @pytest.mark.asyncio
    async def test_an_outdated_app_version_is_upgraded(self):
        def handler(request):
            if request.headers["User-Agent"] == "Android/2.56.0":
                return httpx.Response(426, headers={"min-supported-version-android": "2.70.1"})
            return httpx.Response(200, json={"transactions": []})

        credentials = {"access_token": jwt(time.time() + 3600), "refresh_token": "r"}
        async with new_http_client(httpx.MockTransport(handler)) as http:
            await biedronka.BiedronkaProvider().client(credentials, http).list_receipts(known_ids=set())
        assert credentials["app_version"] == "2.70.1"

    @pytest.mark.asyncio
    async def test_e_receipt_first_then_details(self):
        def handler(request):
            if request.url.path.endswith("/e-receipt/"):
                assert request.headers["output-format"] == "json"
                return httpx.Response(404)
            return httpx.Response(200, json={"items": [{"name": "Chleb", "unit_price": 4.5}]})

        summary = biedronka.summary_from_transaction({"id": 9, "date": "2026-08-29T10:00:00+02:00", "total_price": 4.5})
        async with new_http_client(httpx.MockTransport(handler)) as http:
            client = biedronka.BiedronkaProvider().client({"access_token": jwt(time.time() + 3600), "refresh_token": "r"}, http)
            receipt = await client.fetch_receipt(summary)
        assert receipt is not None and receipt.lines[0].name == "Chleb"

    @pytest.mark.asyncio
    async def test_the_pasted_redirect_is_exchanged_with_the_verifier(self):
        provider = biedronka.BiedronkaProvider()
        pending = provider.start_authorization()

        def handler(request):
            form = parse_qs(request.content.decode())
            assert form["code"] == ["the-code"]
            assert form["code_verifier"] == [pending.state["code_verifier"]]
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})

        async with new_http_client(httpx.MockTransport(handler)) as http:
            tokens = await provider.complete_authorization(pending.state, "app://cma20.biedronka.pl?code=the-code", http)
        assert tokens == {"access_token": "a", "refresh_token": "r"}


class TestLidlClient:
    @pytest.mark.asyncio
    async def test_lists_every_page_and_fetches_v3_detail(self):
        def handler(request):
            if request.url.host == "accounts.lidl.com":
                assert request.headers["Authorization"].startswith("Basic ")
                return httpx.Response(200, json={"access_token": "a", "refresh_token": "r2", "expires_in": 3600})
            assert request.headers["App"] == "com.lidl.eci.lidl.plus"
            if request.url.path == "/api/v2/PL/tickets":
                page = int(request.url.params["pageNumber"])
                tickets = [{"id": "t1"}, {"id": "t2"}] if page == 1 else [{"id": "t3"}]
                return httpx.Response(200, json={"tickets": tickets, "totalCount": 3, "size": 2})
            assert request.url.path == "/api/v3/PL/tickets/t1"
            return httpx.Response(200, json={"id": "t1", "totalAmount": 1.79, "date": "2026-10-22T10:00:00+00:00", "htmlPrintedReceipt": receipt_html(article("1", "Precel", "1,79"))})

        credentials = {"refresh_token": "r1"}
        async with new_http_client(httpx.MockTransport(handler)) as http:
            client = lidl_plus.LidlPlusProvider().client(credentials, http)
            found = await client.list_receipts(known_ids=set())
            receipt = await client.fetch_receipt(found[0])
        assert [s.external_id for s in found] == ["t1", "t2", "t3"]
        assert receipt.lines[0].name == "Precel"
        assert credentials["refresh_token"] == "r2"

    @pytest.mark.asyncio
    async def test_a_server_error_is_transient(self):
        def handler(request):
            if request.url.host == "accounts.lidl.com":
                return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
            return httpx.Response(503)

        async with new_http_client(httpx.MockTransport(handler)) as http:
            client = lidl_plus.LidlPlusProvider().client({"refresh_token": "r"}, http)
            with pytest.raises(RetailProviderError):
                await client.list_receipts(known_ids=set())
