"""Who spent what: a shared workspace's income and expenses per person."""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.transaction_category_line import TransactionCategoryLine
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.services.people_report_service import shift_months

async def household(session, test_user, test_workspace):
    """Me with two accounts, Ola with one, both in one workspace."""
    ola = User(
        id=uuid.uuid4(), email="ola@example.com", hashed_password="x", is_active=True, is_verified=True,
        preferences={"display_name": "Ola"},
    )
    session.add(ola)
    await session.flush()
    session.add(WorkspaceMember(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=ola.id, role="editor"))

    def account(owner, name, currency="PLN"):
        return Account(
            id=uuid.uuid4(), user_id=owner.id, workspace_id=test_workspace.id, name=name, type="checking",
            balance=Decimal("0"), currency=currency,
        )

    mine, my_card, hers = account(test_user, "ING"), account(test_user, "Revolut"), account(ola, "mBank")
    food = Category(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Jedzenie", color="#ff0000")
    transport = Category(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Transport", color="#00ff00")
    session.add_all([mine, my_card, hers, food, transport])
    await session.flush()
    return ola, mine, my_card, hers, food, transport


def tx(account, day, amount, kind="debit", **extra):
    return Transaction(
        id=uuid.uuid4(), user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
        description="row", amount=Decimal(amount), currency=account.currency, date=day, type=kind,
        source="sync", status=extra.pop("status", "posted"), **extra,
    )


@pytest.mark.asyncio
async def test_each_person_is_charged_for_their_own_accounts(client, auth_headers, session, test_user, test_workspace):
    ola, mine, my_card, hers, food, transport = await household(session, test_user, test_workspace)
    nov, oct_ = date(2026, 11, 5), date(2026, 10, 12)
    pair = uuid.uuid4()
    split = tx(mine, nov, "60.00", has_category_lines=True)
    session.add_all([
        tx(mine, nov, "100.00", category_id=food.id),
        tx(my_card, nov, "50.00"),                                   # no category
        tx(mine, nov, "3000.00", kind="credit"),                     # salary
        tx(hers, nov, "80.00", category_id=food.id),
        tx(hers, oct_, "20.00", category_id=food.id),
        # Money moved from me to Ola is nobody's spending.
        tx(mine, nov, "500.00", transfer_pair_id=pair),
        tx(hers, nov, "500.00", kind="credit", transfer_pair_id=pair),
        tx(mine, nov, "999.00", status="pending"),
        split,
    ])
    await session.flush()
    session.add_all([
        TransactionCategoryLine(workspace_id=test_workspace.id, transaction_id=split.id, category_id=food.id, amount=Decimal("40.00"), position=0),
        TransactionCategoryLine(workspace_id=test_workspace.id, transaction_id=split.id, category_id=transport.id, amount=Decimal("20.00"), position=1),
    ])
    await session.commit()

    resp = await client.get("/api/reports/people", headers=auth_headers, params={"month": "2026-11", "months": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    me, her = body["people"]
    assert (me["is_you"], me["name"], her["name"]) == (True, "test", "Ola")
    assert set(me["account_ids"]) == {str(mine.id), str(my_card.id)}
    assert her["account_ids"] == [str(hers.id)]

    series = {(m["month"], m["user_id"]): (Decimal(m["income"]), Decimal(m["expenses"])) for m in body["months"]}
    assert series == {
        ("2026-10", me["user_id"]): (Decimal("0"), Decimal("0")),
        ("2026-10", her["user_id"]): (Decimal("0"), Decimal("20.00")),
        ("2026-11", me["user_id"]): (Decimal("3000.00"), Decimal("210.00")),
        ("2026-11", her["user_id"]): (Decimal("0"), Decimal("80.00")),
    }

    categories = {c["name"]: {k: Decimal(v) for k, v in c["amounts"].items()} for c in body["categories"]}
    # The split row counts part by part: 40 food, 20 transport.
    assert categories == {
        "Jedzenie": {me["user_id"]: Decimal("140.00"), her["user_id"]: Decimal("80.00")},
        None: {me["user_id"]: Decimal("50.00")},
        "Transport": {me["user_id"]: Decimal("20.00")},
    }
    assert [c["name"] for c in body["categories"]] == ["Jedzenie", None, "Transport"]


@pytest.mark.asyncio
async def test_foreign_currency_counts_in_the_primary_amount(client, auth_headers, session, test_user, test_workspace):
    _, mine, *_ = await household(session, test_user, test_workspace)
    euro = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="EUR", type="checking",
        balance=Decimal("0"), currency="EUR",
    )
    session.add(euro)
    session.add(tx(euro, date(2026, 11, 3), "10.00", amount_primary=Decimal("43.00")))
    await session.commit()

    body = (await client.get("/api/reports/people", headers=auth_headers, params={"month": "2026-11", "months": 1})).json()
    mine_row = next(m for m in body["months"] if m["user_id"] == str(test_user.id))
    assert Decimal(mine_row["expenses"]) == Decimal("43.00")


@pytest.mark.asyncio
async def test_a_month_must_be_a_month(client, auth_headers):
    resp = await client.get("/api/reports/people", headers=auth_headers, params={"month": "2026-13"})
    assert resp.status_code == 422


def test_months_step_across_years():
    assert shift_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert shift_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
