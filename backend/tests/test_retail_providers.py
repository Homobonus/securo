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
            {"id": 77, "date": "2026-08-29T22:30:00+00:00", "total_price": "37.03", "store_name": "ul. Wielicka 1, 30-552 Kraków"}
        )
        receipt = biedronka.build_receipt(summary, E_RECEIPT, e_receipt=True)
        assert receipt.date == date(2026, 8, 30)
        assert receipt.external_id == "77"
        # The history list puts the store's address where its name would be.
        assert receipt.store_name == "Biedronka"
        assert receipt.store_address == "ul. Wielicka 1, 30-552 Kraków"

    def test_plain_details_when_there_is_no_e_receipt(self):
        summary = biedronka.summary_from_transaction({"id": 5, "date": "2026-08-29T10:00:00+02:00", "total_price": 12.5})
        payload = {"items": [{"name": "Chleb", "unit_price": 4.5, "total_price": 9.0, "quantity": 2}, {"name": "Masło", "unit_price": "3,50"}]}
        receipt = biedronka.build_receipt(summary, payload, e_receipt=False)
        assert [(i.name, i.quantity, i.total) for i in receipt.lines] == [
            ("Chleb", Decimal("2"), Decimal("9.00")),
            ("Masło", Decimal("1"), Decimal("3.50")),
        ]
        assert receipt.total == Decimal("12.50")

    def test_detail_rebates_units_and_store(self):
        # The shape the transaction detail has when there is no e-receipt.
        summary = biedronka.summary_from_transaction(
            {"id": 6, "date": "2026-10-23T14:34:22Z", "total_price": 27.48, "store_name": "ul. T. Kościuszki 229, 40-706 Katowice"}
        )
        payload = {
            "total_discount": 5.0,
            "store": {"street": "ul. T. Kościuszki 229", "zip_code": "40-706", "city": "Katowice"},
            "items": [
                {"name": "WzornikKsztaltow", "quantity": 1.0, "unit_price": 24.99, "total_discount": 5.0,
                 "total_price_without_discount": 24.99, "total_price": 19.99, "ean": "5905118875555", "measure_unit": "OPA"},
                {"name": "Banany", "quantity": 1.248, "unit_price": 6.0, "total_discount": 0.0,
                 "total_price_without_discount": 7.49, "total_price": 7.49, "measure_unit": "KG"},
            ],
        }
        receipt = biedronka.build_receipt(summary, payload, e_receipt=False)
        assert [(i.name, i.unit, i.total, i.discount, i.code) for i in receipt.lines] == [
            ("WzornikKsztaltow", "szt", Decimal("24.99"), Decimal("5.00"), "5905118875555"),
            ("Banany", "kg", Decimal("7.49"), Decimal("0.00"), None),
        ]
        assert receipt.discount_total == Decimal("5.00")
        assert sum(i.total - i.discount for i in receipt.lines) == receipt.total
        assert (receipt.store_name, receipt.store_address) == ("Biedronka Katowice", "ul. T. Kościuszki 229, 40-706 Katowice")


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


# ---------------------------------------------------------------------------
# Lidl: the Polish printed layout and the older data-only tickets
# ---------------------------------------------------------------------------
def pl_article(line, art_id, name, price, quantity_text):
    attrs = f'class="article" data-art-id="{art_id}" data-unit-price="{price}" data-tax-type="C" data-art-description="{name}"'
    return (
        f'<span id="purchase_list_line_{line}" {attrs}>{name}           </span>\n'
        f'<span id="purchase_list_line_{line + 1}" {attrs}>                       {quantity_text}</span>\n'
    )


# The structure of a real 2026 Polish e-receipt, with made-up lines.
PL_RECEIPT = (
    '<html><body><pre><span class="header" data-till-country="PL">'
    '<span id="header_line_2">Adres siedziby: Poznańska 48, Jankowice </span></span>'
    '<span class="purchase_list"><span id="purchase_list_line_1" class="currency" data-currency="zł">2026-10-19</span>\n'
    + pl_article(3, "0081", "Burrata z Włoch", "7,49", "1 * 7.49 7.49 C")
    + '<span id="purchase_list_line_5" class="discount" data-promotion-id="_DISCOUNT1">   RABAT 50%                   -3,75</span>\n'
    + pl_article(6, "0095664", "Precel z solą", "1,79", "1 * 1.79 1.79 C")
    + pl_article(8, "0012", "Banany luz", "6,99", "0,682kg x 6.99 4.77 C")
    + pl_article(10, "0095664", "Precel z solą", "1,79", "2 * 1.79 3.58 C")
    + '</span><span class="vat_info"><span id="vat_info_line_3">Suma                              0,35</span></span>'
    '<span class="purchase_summary"><span id="purchase_summary_1" class="css_big">Suma PLN</span>'
    '<span id="purchase_summary_1" class="css_big">13,88</span>\n'
    '<span id="purchase_summary_3">Opakowania zwrotne wydania             </span>\n'
    '<span id="purchase_summary_4">   Kaucja PET              6 * 0.5 3.0</span>\n'
    '<span id="purchase_summary_5">Opakowania zwrotne suma           3,00</span>\n'
    '<span id="purchase_summary_6" class="css_big css_bold">Suma</span>'
    '<span id="purchase_summary_6" class="css_big css_bold">16,88</span>\n'
    '<span id="purchase_summary_7" data-tender-description="Karta płatnicza">Płatność Karta płatnicza 16,88</span>\n'
    "</span></pre></body></html>"
)


class TestLidlPolishLayout:
    def test_quantity_lines_rebates_and_deposits(self):
        lines, discounts = lidl_plus.parse_receipt_html(PL_RECEIPT, Decimal("16.88"))
        assert [(i.name, i.quantity, i.unit, i.total, i.discount) for i in lines] == [
            ("Burrata z Włoch", Decimal("1"), "szt", Decimal("7.49"), Decimal("3.75")),
            ("Precel z solą", Decimal("1"), "szt", Decimal("1.79"), Decimal("0")),
            ("Banany luz", Decimal("0.682"), "kg", Decimal("4.77"), Decimal("0")),
            ("Precel z solą", Decimal("2"), "szt", Decimal("3.58"), Decimal("0")),
            ("Kaucja PET", Decimal("6"), "szt", Decimal("3.00"), Decimal("0")),
        ]
        assert discounts == Decimal("3.75")
        # What was paid adds up to the penny, deposit included.
        assert sum(i.total - i.discount for i in lines) == Decimal("16.88")

    def test_article_codes_are_kept(self):
        lines, _ = lidl_plus.parse_receipt_html(PL_RECEIPT, Decimal("16.88"))
        assert lines[0].code == "0081"


NATIVE_TICKET = {
    "ticketType": "NATIVE",
    "id": "240010578320260304189215",
    "date": "2026-03-04T10:21:13",
    "totalAmount": 26.80,
    "currency": {"code": "PLN"},
    "store": {"name": "Kraków", "address": "ul. Wielicka 1", "postalCode": "30-552", "locality": "Kraków"},
    "itemsLine": [
        {"currentUnitPrice": "2,99", "quantity": "2", "isWeight": False, "originalAmount": "5,98", "name": "Rzodkiewki pęczek",
         "codeInput": "82275", "discounts": [{"description": "950_82275", "amount": "3,00"}], "deposit": None},
        {"currentUnitPrice": "4,99", "quantity": "3,406", "isWeight": True, "originalAmount": "17,00", "name": "Bataty luz",
         "codeInput": "80755", "discounts": [], "deposit": None},
        {"currentUnitPrice": "5,49", "quantity": "1", "isWeight": False, "originalAmount": "5,49", "name": "Rukola 125g",
         "codeInput": "20205775", "discounts": [{"description": "Lidl Plus rabat", "amount": "1,65"}], "deposit": None},
        {"currentUnitPrice": "2,48", "quantity": "1", "isWeight": False, "originalAmount": "2,48", "name": "Woda 1,5l",
         "codeInput": "590", "discounts": [], "deposit": {"description": "Kaucja PET", "amount": "0,50", "quantity": "1"}},
    ],
}


class TestLidlNativeTickets:
    def test_items_rebates_weights_and_deposits(self):
        summary = lidl_plus.summary_from_ticket({"id": NATIVE_TICKET["id"], "date": NATIVE_TICKET["date"], "totalAmount": 26.80})
        receipt = lidl_plus.build_receipt(summary, NATIVE_TICKET)
        assert [(i.name, i.quantity, i.unit, i.discount, i.code) for i in receipt.lines] == [
            ("Rzodkiewki pęczek", Decimal("2"), "szt", Decimal("3.00"), "82275"),
            ("Bataty luz", Decimal("3.406"), "kg", Decimal("0"), "80755"),
            ("Rukola 125g", Decimal("1"), "szt", Decimal("1.65"), "20205775"),
            ("Woda 1,5l", Decimal("1"), "szt", Decimal("0"), "590"),
            ("Kaucja PET", Decimal("1"), "szt", Decimal("0"), None),
        ]
        assert receipt.discount_total == Decimal("4.65")
        assert sum(i.total - i.discount for i in receipt.lines) == receipt.total == Decimal("26.80")
        assert receipt.store_name == "Kraków"


def test_the_tickets_api_gets_a_real_app_version():
    # A made-up version makes the API hold the request until it times out.
    assert lidl_plus.APP_HEADERS["App-Version"] != "999.99.9"
    assert biedronka.APP_VERSION


@pytest.mark.asyncio
async def test_a_timeout_names_itself():
    def handler(request):
        if request.url.host == "accounts.lidl.com":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
        raise httpx.ReadTimeout("", request=request)

    async with new_http_client(httpx.MockTransport(handler)) as http:
        client = lidl_plus.LidlPlusProvider().client({"refresh_token": "r"}, http)
        with pytest.raises(RetailProviderError, match="ReadTimeout"):
            await client.list_receipts(known_ids=set())
