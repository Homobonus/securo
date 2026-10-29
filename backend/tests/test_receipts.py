"""Receipts: storing them, pairing them with what the bank saw, and the
products they add up to.

The pairing rule is the one that matters most: a receipt is tied to a
debit only when the evidence leaves one answer. A wrong pairing is worse
than none, because it hides the real one.
"""
import time
import uuid
from collections.abc import Collection
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.account import Account
from app.models.receipt import Receipt, RetailConnection, ShoppingProduct, ShoppingProductAlias
from app.models.transaction import Transaction
from app.providers.retail.base import ReceiptData, ReceiptLineData
from app.services import receipt_service as svc
from app.services import retail_connection_service as connections
from app.services.receipt_service import Purchase, ReceiptError
from tests.test_retail_providers import E_RECEIPT, jwt


def line(name, quantity="1", unit_price="10.00", discount="0"):
    q, p = Decimal(quantity), Decimal(unit_price)
    return ReceiptLineData(
        name=name, quantity=q, unit_price=p, total=(q * p).quantize(Decimal("0.01")), discount=Decimal(discount)
    )


def receipt_data(external_id, day, total, *lines):
    return ReceiptData(
        external_id=external_id,
        purchased_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
        date=day,
        total=Decimal(total),
        store_name="Lidl",
        lines=list(lines),
    )


@pytest_asyncio.fixture
async def ws_id(test_workspace) -> uuid.UUID:
    return test_workspace.id


@pytest_asyncio.fixture
async def account(session, test_user, ws_id) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=ws_id, name="Konto", type="checking",
        balance=Decimal("0"), currency="PLN",
    )
    session.add(acc)
    await session.commit()
    return acc


async def debit(session, account, day, amount, description="LIDL SP. Z O.O."):
    tx = Transaction(
        id=uuid.uuid4(), user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
        description=description, amount=Decimal(amount), currency="PLN", date=day, type="debit", source="sync",
    )
    session.add(tx)
    await session.commit()
    return tx


# ---------------------------------------------------------------------------
# Ingest and products
# ---------------------------------------------------------------------------
class TestIngest:
    @pytest.mark.asyncio
    async def test_a_receipt_is_stored_with_its_lines_and_products(self, session, ws_id):
        receipt, created = await svc.ingest_receipt(
            session, ws_id, "lidl_plus",
            receipt_data("r1", date(2026, 10, 1), "34.98", line("Kawa ziarn.", unit_price="24.99"), line("Precel", unit_price="9.99")),
        )
        await session.commit()
        await session.refresh(receipt, ["items"])
        assert created
        assert [i.name for i in receipt.items] == ["Kawa ziarn.", "Precel"]
        assert all(i.product_id for i in receipt.items)

    @pytest.mark.asyncio
    async def test_re_ingesting_rewrites_lines_and_keeps_the_pairing(self, session, ws_id, account):
        tx = await debit(session, account, date(2026, 10, 1), "24.99")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r1", date(2026, 10, 1), "24.99", line("Kawa")))
        await svc.link_transaction(session, receipt, tx.id)
        await session.commit()
        again, created = await svc.ingest_receipt(
            session, ws_id, "lidl_plus", receipt_data("r1", date(2026, 10, 1), "24.99", line("Kawa"), line("Gratis", unit_price="0.00"))
        )
        await session.commit()
        await session.refresh(again, ["items"])
        assert not created and again.id == receipt.id
        assert len(again.items) == 2
        assert again.transaction_id == tx.id

    @pytest.mark.asyncio
    async def test_the_same_printed_name_is_the_same_product(self, session, ws_id):
        for i, day in enumerate((date(2026, 9, 1), date(2026, 9, 15))):
            await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data(f"r{i}", day, "10.00", line("Kawa  ZIARN.")))
        await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r9", date(2026, 9, 20), "10.00", line("kawa ziarn.")))
        await session.commit()
        products = (await session.execute(select(ShoppingProduct).where(ShoppingProduct.workspace_id == ws_id))).scalars().all()
        assert len(products) == 1

    @pytest.mark.asyncio
    async def test_the_same_name_at_another_store_is_another_product(self, session, ws_id):
        await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("a", date(2026, 9, 1), "10", line("Mleko 2%")))
        await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("b", date(2026, 9, 1), "10", line("Mleko 2%")))
        await session.commit()
        count = len((await session.execute(select(ShoppingProduct).where(ShoppingProduct.workspace_id == ws_id))).scalars().all())
        assert count == 2

    @pytest.mark.asyncio
    async def test_a_merge_survives_the_next_sync(self, session, ws_id):
        await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("a", date(2026, 9, 1), "10", line("Kawa Bellarom")))
        await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("b", date(2026, 9, 2), "10", line("Kawa Lavazza")))
        await session.commit()
        aliases = {
            a.name: a.product_id
            for a in (await session.execute(select(ShoppingProductAlias).where(ShoppingProductAlias.workspace_id == ws_id))).scalars()
        }
        target = await session.get(ShoppingProduct, aliases["Kawa Bellarom"])
        await svc.merge_products(session, target, [aliases["Kawa Lavazza"]])
        await session.commit()

        receipt, _ = await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("c", date(2026, 9, 20), "10", line("Kawa Lavazza")))
        await session.commit()
        await session.refresh(receipt, ["items"])
        assert receipt.items[0].product_id == target.id
        purchases = await svc.purchases_by_product(session, ws_id, [target.id])
        assert len(purchases[target.id]) == 3

    @pytest.mark.asyncio
    async def test_merging_a_product_of_another_workspace_is_refused(self, session, ws_id):
        await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("a", date(2026, 9, 1), "10", line("Kawa")))
        await session.commit()
        target = (await session.execute(select(ShoppingProduct).where(ShoppingProduct.workspace_id == ws_id))).scalar_one()
        with pytest.raises(ReceiptError):
            await svc.merge_products(session, target, [uuid.uuid4()])


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
class TestPairing:
    @pytest.mark.asyncio
    async def test_the_one_debit_of_that_amount_is_paired(self, session, ws_id, account):
        tx = await debit(session, account, date(2026, 10, 3), "34.98")
        await debit(session, account, date(2026, 10, 3), "34.97")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r", date(2026, 10, 1), "34.98"))
        assert await svc.auto_match(session, ws_id) == 1
        assert receipt.transaction_id == tx.id and receipt.match_method == "auto"

    @pytest.mark.asyncio
    async def test_outside_the_window_is_not_a_candidate(self, session, ws_id, account):
        await debit(session, account, date(2026, 10, 10), "34.98")
        await debit(session, account, date(2026, 9, 28), "34.98")
        await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r", date(2026, 10, 1), "34.98"))
        assert await svc.auto_match(session, ws_id) == 0

    @pytest.mark.asyncio
    async def test_the_debit_naming_the_store_wins(self, session, ws_id, account):
        await debit(session, account, date(2026, 10, 1), "50.00", "ORLEN STACJA")
        lidl = await debit(session, account, date(2026, 10, 2), "50.00", "LIDL KRAKOW")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r", date(2026, 10, 1), "50.00"))
        await svc.auto_match(session, ws_id)
        assert receipt.transaction_id == lidl.id

    @pytest.mark.asyncio
    async def test_two_equally_good_debits_are_left_to_a_person(self, session, ws_id, account):
        await debit(session, account, date(2026, 10, 2), "50.00", "LIDL A")
        await debit(session, account, date(2026, 10, 2), "50.00", "LIDL B")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r", date(2026, 10, 1), "50.00"))
        assert await svc.auto_match(session, ws_id) == 0
        assert receipt.transaction_id is None
        assert len(await svc.find_candidates(session, receipt)) == 2

    @pytest.mark.asyncio
    async def test_a_debit_pays_one_receipt_only(self, session, ws_id, account):
        await debit(session, account, date(2026, 10, 1), "20.00")
        first, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("a", date(2026, 10, 1), "20.00"))
        second, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("b", date(2026, 10, 1), "20.00"))
        assert await svc.auto_match(session, ws_id) == 1
        assert [first.transaction_id is not None, second.transaction_id is not None].count(True) == 1

    @pytest.mark.asyncio
    async def test_a_manual_pairing_is_kept_and_exclusive(self, session, ws_id, account):
        tx = await debit(session, account, date(2026, 10, 1), "99.00", "PRZELEW")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("a", date(2026, 10, 1), "10.00"))
        other, _ = await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("b", date(2026, 10, 1), "10.00"))
        await svc.link_transaction(session, receipt, tx.id)
        await svc.auto_match(session, ws_id)
        assert receipt.transaction_id == tx.id and receipt.match_method == "manual"
        with pytest.raises(ReceiptError):
            await svc.link_transaction(session, other, tx.id)

    @pytest.mark.asyncio
    async def test_the_connection_account_limits_pairing(self, session, ws_id, account, test_user):
        card = Account(id=uuid.uuid4(), user_id=test_user.id, workspace_id=ws_id, name="Karta", type="credit_card", balance=Decimal("0"), currency="PLN")
        session.add(card)
        await session.commit()
        await debit(session, account, date(2026, 10, 1), "15.00")
        on_card = await debit(session, card, date(2026, 10, 2), "15.00")
        connection = RetailConnection(workspace_id=ws_id, provider="lidl_plus", status="active", settings={"match_account_id": str(card.id)})
        session.add(connection)
        await session.flush()
        receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data("r", date(2026, 10, 1), "15.00"), connection_id=connection.id)
        await svc.auto_match(session, ws_id)
        assert receipt.transaction_id == on_card.id


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------
def purchase(day, quantity="1", paid="20.00"):
    return Purchase(
        receipt_id=uuid.uuid4(), date=day, quantity=Decimal(quantity), unit="szt", unit_price=Decimal(paid),
        total=Decimal(paid), discount=Decimal("0"), store_name=None, source="lidl_plus",
    )


class TestConsumption:
    def test_one_shopping_day_says_nothing(self):
        result = svc.consumption([purchase(date(2026, 10, 1)), purchase(date(2026, 10, 1))], date(2026, 10, 5))
        assert result.purchase_count == 2 and result.days_per_unit is None

    def test_a_pack_every_two_weeks(self):
        days = [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 29)]
        result = svc.consumption([purchase(d) for d in days], date(2026, 10, 6))
        assert result.avg_days_between == 14.0
        assert result.days_per_unit == 14.0
        assert result.estimated_depletion == date(2026, 10, 13)
        assert result.days_left == 7
        assert result.monthly_cost == Decimal("42.86")

    def test_quantity_bought_stretches_the_interval(self):
        # Two packs on day 0 lasted 30 days: one pack lasts 15.
        result = svc.consumption([purchase(date(2026, 9, 1), "2"), purchase(date(2026, 10, 1), "1")], date(2026, 10, 1))
        assert result.days_per_unit == 15.0
        assert result.estimated_depletion == date(2026, 10, 16)


@pytest.mark.asyncio
async def test_the_last_paid_price_counts_every_line_of_that_day(client, auth_headers, session, ws_id):
    # "60% off the second one", booked by the store on one of the two lines.
    await svc.ingest_receipt(session, ws_id, "lidl_plus", receipt_data(
        "r1", date(2026, 3, 12), "97.99",
        line("Kimbo kawa", unit_price="69.99", discount="41.99"), line("Kimbo kawa", unit_price="69.99"),
    ))
    await session.commit()
    product = (await client.get("/api/shopping-products", headers=auth_headers)).json()[0]
    assert product["last_unit_price"] == "69.99"
    assert product["last_paid_unit_price"] == "49.00"


# ---------------------------------------------------------------------------
# Connections and the API
# ---------------------------------------------------------------------------
LIDL_HTML = (
    '<span class="article" data-art-id="1" data-art-description="Kawa ziarn." data-unit-price="24,99">Kawa</span>'
    '<span class="article" data-art-id="1" data-art-description="Kawa ziarn." data-unit-price="24,99">24,99</span>'
    '<span class="css_bold">SUMA PLN</span>'
)


def lidl_store(state: dict):
    """A fake Lidl Plus: one token endpoint, a ticket list, ticket details."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.lidl.com":
            form = parse_qs(request.content.decode())
            if state.get("reject"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            state.setdefault("grants", []).append(form["grant_type"][0])
            return httpx.Response(200, json={"access_token": "a", "refresh_token": f"r{len(state['grants'])}", "expires_in": 3600})
        if request.url.path == "/api/v2/PL/tickets":
            tickets = [{"id": t, "date": "2026-10-01T09:00:00+00:00", "totalAmount": 24.99} for t in state["tickets"]]
            return httpx.Response(200, json={"tickets": tickets, "totalCount": len(tickets), "size": 50})
        ticket_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": ticket_id, "date": "2026-10-01T09:00:00+00:00", "totalAmount": 24.99, "htmlPrintedReceipt": LIDL_HTML})

    return handler


@pytest.fixture
def fake_lidl(monkeypatch):
    state: dict = {"tickets": ["t2", "t1"]}
    transport = httpx.MockTransport(lidl_store(state))
    original = connections.new_http_client
    monkeypatch.setattr(connections, "new_http_client", lambda _transport=None: original(transport))
    return state


class TestApi:
    @pytest.mark.asyncio
    async def test_connect_sync_and_read_back(self, client, auth_headers, session, account, fake_lidl):
        await debit(session, account, date(2026, 10, 2), "24.99")
        resp = await client.post("/api/retail/connections", headers=auth_headers, json={"provider": "lidl_plus"})
        assert resp.status_code == 201, resp.text
        started = resp.json()
        assert started["connection"]["status"] == "pending"
        assert "code_challenge=" in started["auth_url"]
        assert "credentials" not in started["connection"]
        conn_id = started["connection"]["id"]

        resp = await client.post(
            f"/api/retail/connections/{conn_id}/authorize", headers=auth_headers,
            json={"redirect_url": "Failed to launch 'com.lidlplus.app://callback?code=ABC' because..."},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

        resp = await client.post(f"/api/retail/connections/{conn_id}/sync", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["created"], body["matched"], body["has_more"]) == (2, 1, False)
        assert body["connection"]["receipt_count"] == 2

        # Nothing new the second time: one list call, no detail calls.
        resp = await client.post(f"/api/retail/connections/{conn_id}/sync", headers=auth_headers)
        assert resp.json()["created"] == 0

        listing = (await client.get("/api/receipts", headers=auth_headers)).json()
        assert listing["total"] == 2
        assert {r["item_count"] for r in listing["items"]} == {1}
        paired = [r for r in listing["items"] if r["transaction"]]
        assert len(paired) == 1 and paired[0]["transaction"]["description"] == "LIDL SP. Z O.O."

        detail = (await client.get(f"/api/receipts/{listing['items'][0]['id']}", headers=auth_headers)).json()
        assert detail["items"][0]["product_name"] == "Kawa ziarn."

        products = (await client.get("/api/shopping-products", headers=auth_headers)).json()
        assert len(products) == 1 and products[0]["purchase_count"] == 2
        assert products[0]["last_unit_price"] == products[0]["last_paid_unit_price"] == "24.99"
        product_id = products[0]["id"]
        resp = await client.patch(f"/api/shopping-products/{product_id}", headers=auth_headers, json={"track_consumption": True, "name": "Kawa"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Kawa" and resp.json()["consumption"]["purchase_count"] == 2
        assert len(resp.json()["price_history"]) == 2

        assert (await client.get("/api/receipts", headers=auth_headers, params={"q": "kawa"})).json()["total"] == 2
        assert (await client.get("/api/receipts", headers=auth_headers, params={"matched": "false"})).json()["total"] == 1

    @pytest.mark.asyncio
    async def test_a_revoked_token_asks_for_a_new_sign_in(self, client, auth_headers, session, fake_lidl):
        resp = await client.post("/api/retail/connections", headers=auth_headers, json={"provider": "lidl_plus"})
        conn_id = resp.json()["connection"]["id"]
        await client.post(f"/api/retail/connections/{conn_id}/authorize", headers=auth_headers, json={"refresh_token": "r0"})
        fake_lidl["reject"] = True
        # Force a refresh on the next call.
        connection = await session.get(RetailConnection, uuid.UUID(conn_id))
        await session.refresh(connection)
        connections._write_credentials(connection, {"tokens": {"refresh_token": "r1"}})
        await session.commit()
        body = (await client.post(f"/api/retail/connections/{conn_id}/sync", headers=auth_headers)).json()
        assert body["connection"]["status"] == "reauth_required"

        resp = await client.post(f"/api/retail/connections/{conn_id}/reauthorize", headers=auth_headers)
        assert resp.status_code == 200 and "code_challenge=" in resp.json()["auth_url"]

    @pytest.mark.asyncio
    async def test_a_rejected_code_is_a_400_and_keeps_the_connection_pending(self, client, auth_headers, fake_lidl):
        fake_lidl["reject"] = True
        conn_id = (await client.post("/api/retail/connections", headers=auth_headers, json={"provider": "lidl_plus"})).json()["connection"]["id"]
        resp = await client.post(f"/api/retail/connections/{conn_id}/authorize", headers=auth_headers, json={"redirect_url": "code=x"})
        assert resp.status_code == 400
        listed = (await client.get("/api/retail/connections", headers=auth_headers)).json()
        assert listed[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_an_unknown_provider_is_refused(self, client, auth_headers):
        resp = await client.post("/api/retail/connections", headers=auth_headers, json={"provider": "kaufland"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_manual_pairing_over_http(self, client, auth_headers, session, ws_id, account):
        tx = await debit(session, account, date(2026, 10, 1), "37.03", "BIEDRONKA 1234")
        await debit(session, account, date(2026, 10, 1), "37.03", "BIEDRONKA 5678")
        receipt, _ = await svc.ingest_receipt(session, ws_id, "biedronka", receipt_data("b1", date(2026, 10, 1), "37.03"))
        await session.commit()
        candidates = (await client.get(f"/api/receipts/{receipt.id}/candidates", headers=auth_headers)).json()
        assert len(candidates) == 2
        resp = await client.put(f"/api/receipts/{receipt.id}/transaction", headers=auth_headers, json={"transaction_id": str(tx.id)})
        assert resp.status_code == 200 and resp.json()["match_method"] == "manual"
        by_tx = (await client.get("/api/receipts", headers=auth_headers, params={"transaction_id": str(tx.id)})).json()
        assert by_tx["total"] == 1
        resp = await client.delete(f"/api/receipts/{receipt.id}/transaction", headers=auth_headers)
        assert resp.json()["transaction_id"] is None

    @pytest.mark.asyncio
    async def test_viewers_read_but_do_not_write(self, client, viewer_auth_headers):
        assert (await client.get("/api/receipts", headers=viewer_auth_headers)).status_code == 200
        resp = await client.post("/api/retail/connections", headers=viewer_auth_headers, json={"provider": "biedronka"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_the_biedronka_e_receipt_end_to_end(self, session, ws_id, monkeypatch):
        def handler(request):
            if request.url.host == "konto.biedronka.pl":
                return httpx.Response(200, json={"access_token": jwt(time.time() + 3600), "refresh_token": "r"})
            if request.url.path.endswith("/transactions/"):
                return httpx.Response(200, json={"transactions": [{"id": 1, "date": "2026-08-29T09:24:00+00:00", "total_price": 37.03, "is_e_receipt_available": True}], "next_page": None})
            return httpx.Response(200, json=E_RECEIPT)

        original = connections.new_http_client
        monkeypatch.setattr(connections, "new_http_client", lambda _t=None: original(httpx.MockTransport(handler)))
        connection = RetailConnection(workspace_id=ws_id, provider="biedronka", status="active")
        connections._write_credentials(connection, {"tokens": {"refresh_token": "r"}})
        session.add(connection)
        await session.commit()
        result = await connections.sync_connection(session, connection)
        assert result.created == 1
        receipt = (await session.execute(select(Receipt).where(Receipt.workspace_id == ws_id))).scalar_one()
        await session.refresh(receipt, ["items"])
        assert receipt.total == Decimal("37.03") and receipt.discount_total == Decimal("16.98")
        assert [i.name for i in receipt.items] == ["PapierQueen8rolek", "Banany"]
        assert connection.last_sync_at is not None and connection.last_sync_at > datetime.now(timezone.utc) - timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Syncing a long history
# ---------------------------------------------------------------------------
def dated_lidl(tickets: dict[str, str], failing: Collection[str] = (), page_size: int = 2):
    """A fake Lidl Plus with dated tickets, newest first, `page_size` per page."""
    ordered = sorted(tickets, key=lambda t: tickets[t], reverse=True)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.lidl.com":
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
        if request.url.path == "/api/v2/PL/tickets":
            page = int(request.url.params["pageNumber"])
            chunk = ordered[(page - 1) * page_size : page * page_size]
            return httpx.Response(200, json={
                "tickets": [{"id": t, "date": tickets[t], "totalAmount": 24.99} for t in chunk],
                "totalCount": len(ordered), "size": page_size,
            })
        ticket_id = request.url.path.rsplit("/", 1)[-1]
        calls.append(ticket_id)
        if ticket_id in failing:
            return httpx.Response(400)
        return httpx.Response(200, json={"id": ticket_id, "date": tickets[ticket_id], "totalAmount": 24.99, "htmlPrintedReceipt": LIDL_HTML})

    return handler, calls


def settings_of(connection: RetailConnection) -> dict:
    return connection.settings or {}


async def lidl_connection(session, ws_id) -> RetailConnection:
    connection = RetailConnection(workspace_id=ws_id, provider="lidl_plus", status="active", settings={})
    connections._write_credentials(connection, {"tokens": {"refresh_token": "r"}})
    session.add(connection)
    await session.commit()
    return connection


async def stored_ids(session, ws_id) -> set[str]:
    return set((await session.execute(select(Receipt.external_id).where(Receipt.workspace_id == ws_id))).scalars())


TICKETS = {
    "t2020": "2020-02-27T10:00:00+00:00",
    "t2023": "2023-05-01T10:00:00+00:00",
    "t2025": "2025-12-01T10:00:00+00:00",
    "t2026": "2026-10-22T10:00:00+00:00",
}


class TestHistorySync:
    @pytest.mark.asyncio
    async def test_newest_receipts_come_first_and_the_rest_follows(self, session, ws_id):
        handler, _ = dated_lidl(TICKETS)
        connection = await lidl_connection(session, ws_id)
        transport = httpx.MockTransport(handler)

        first = await connections.sync_connection(session, connection, transport=transport, max_receipts=2)
        assert (first.created, first.has_more) == (2, True)
        assert await stored_ids(session, ws_id) == {"t2026", "t2025"}
        assert settings_of(connection)["backfill_complete"] is False

        # The next run still reads every page, though page one holds nothing new.
        second = await connections.sync_connection(session, connection, transport=transport, max_receipts=2)
        assert (second.created, second.has_more) == (2, False)
        assert await stored_ids(session, ws_id) == set(TICKETS)
        assert settings_of(connection)["backfill_complete"] is True

    @pytest.mark.asyncio
    async def test_once_caught_up_only_new_pages_are_read(self, session, ws_id):
        handler, calls = dated_lidl(TICKETS)
        connection = await lidl_connection(session, ws_id)
        transport = httpx.MockTransport(handler)
        await connections.sync_connection(session, connection, transport=transport)
        calls.clear()
        result = await connections.sync_connection(session, connection, transport=transport)
        assert result.created == 0 and calls == []

    @pytest.mark.asyncio
    async def test_a_receipt_the_store_refuses_is_skipped_not_fatal(self, session, ws_id):
        handler, calls = dated_lidl(TICKETS, failing={"t2023"})
        connection = await lidl_connection(session, ws_id)
        transport = httpx.MockTransport(handler)

        result = await connections.sync_connection(session, connection, transport=transport)
        assert (result.created, result.failed, connection.status) == (3, 1, "active")
        assert settings_of(connection)["failed_receipts"] == {"t2023": 1}

        # Retried a limited number of times, then left alone.
        for _ in range(connections.MAX_ATTEMPTS_PER_RECEIPT):
            connection.settings = {**settings_of(connection), "backfill_complete": False}
            await connections.sync_connection(session, connection, transport=transport)
        calls.clear()
        connection.settings = {**settings_of(connection), "backfill_complete": False}
        await connections.sync_connection(session, connection, transport=transport)
        assert "t2023" not in calls

    @pytest.mark.asyncio
    async def test_a_store_that_refuses_everything_is_an_error(self, session, ws_id):
        many = {f"t{i}": f"2026-01-{i + 1:02d}T10:00:00+00:00" for i in range(8)}
        handler, _ = dated_lidl(many, failing=set(many))
        connection = await lidl_connection(session, ws_id)
        result = await connections.sync_connection(session, connection, transport=httpx.MockTransport(handler))
        assert connection.status == "error"
        assert result.failed == connections.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_stored_receipts_are_rebuilt_from_their_payload(session, ws_id, account):
    from tests.test_retail_providers import NATIVE_TICKET

    tx = await debit(session, account, date(2026, 3, 4), "26.80")
    stale = receipt_data(NATIVE_TICKET["id"], date(2026, 3, 4), "26.80", line("old parse"))
    stale.raw_data = {"ticket": NATIVE_TICKET}
    receipt, _ = await svc.ingest_receipt(session, ws_id, "lidl_plus", stale)
    await svc.link_transaction(session, receipt, tx.id)
    await session.commit()

    assert await svc.reparse_receipts(session, ws_id) == 1
    await session.commit()
    await session.refresh(receipt, ["items"])
    assert [i.name for i in receipt.items][:2] == ["Rzodkiewki pęczek", "Bataty luz"]
    assert receipt.discount_total == Decimal("4.65")
    assert receipt.transaction_id == tx.id
