"""A group's totals are in the group's currency, over all of its rows.

The page used to add each row's amount in the viewer's own primary
currency and label the sum with the group's, and only over the last 20
rows it lists, so a EUR trip paid by a PLN card showed złoty marked "EUR".
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.account import Account
from app.models.category import Category
from app.models.fx_rate import FxRate
from app.models.group import Group, GroupMember
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit


async def seed_trip(session, user, workspace, *, rows):
    group = Group(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="Włochy", default_currency="EUR")
    me = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=workspace.id, name="Ja", is_self=True, linked_user_id=user.id)
    partner = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=workspace.id, name="Ona")
    account = Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="ING", type="checking",
        balance=Decimal("0"), currency="PLN",
    )
    food = Category(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="Jedzenie", color="#ff0000")
    session.add_all([group, me, partner, account, food])
    for i, (day, amount, currency, kind) in enumerate(rows):
        tx = Transaction(
            id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, account_id=account.id,
            description=f"row {i}", amount=Decimal(amount), currency=currency, date=day, type=kind,
            source="manual", category_id=food.id if i == 0 else None,
        )
        session.add(tx)
        for member in (me, partner):
            session.add(TransactionSplit(
                id=uuid.uuid4(), transaction_id=tx.id, workspace_id=workspace.id, group_member_id=member.id,
                share_amount=Decimal(amount) / 2, share_type="equal",
            ))
    await session.commit()
    return group


def rate(day, quote, value):
    return FxRate(base_currency="USD", quote_currency=quote, date=day, rate=Decimal(value), source="test")


@pytest.mark.asyncio
async def test_rows_in_another_currency_are_converted_to_the_groups(client, auth_headers, session, test_user, test_workspace):
    day = date(2026, 7, 10)
    # 1 USD = 4.00 PLN = 0.80 EUR, so 1 EUR = 5.00 PLN.
    session.add_all([rate(day, "PLN", "4.00"), rate(day, "EUR", "0.80")])
    group = await seed_trip(session, test_user, test_workspace, rows=[
        (day, "500.00", "PLN", "debit"),   # 100 EUR
        (day, "40.00", "EUR", "debit"),    # 40 EUR
    ])

    resp = await client.get(f"/api/groups/{group.id}/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == "EUR"
    assert Decimal(body["total"]) == Decimal("140.00")
    assert body["by_month"] == [{"month": "2026-07", "total": "140.00"}]
    assert [(c["name"], c["total"]) for c in body["by_category"]] == [("Jedzenie", "100.00"), (None, "40.00")]
    assert body["fx_fallback"] is False


@pytest.mark.asyncio
async def test_every_row_counts_not_just_the_listed_ones(client, auth_headers, session, test_user, test_workspace):
    rows = [(date(2026, 7, 1 + i % 28), "10.00", "EUR", "debit") for i in range(30)]
    group = await seed_trip(session, test_user, test_workspace, rows=rows)

    body = (await client.get(f"/api/groups/{group.id}/summary", headers=auth_headers)).json()
    assert body["transaction_count"] == 30
    assert Decimal(body["total"]) == Decimal("300.00")
    listed = (await client.get(f"/api/groups/{group.id}/transactions", headers=auth_headers)).json()
    assert len(listed) == 20


@pytest.mark.asyncio
async def test_a_row_without_a_rate_counts_one_to_one_and_says_so(client, auth_headers, session, test_user, test_workspace):
    group = await seed_trip(session, test_user, test_workspace, rows=[(date(2020, 1, 1), "50.00", "PLN", "debit")])

    body = (await client.get(f"/api/groups/{group.id}/summary", headers=auth_headers)).json()
    assert Decimal(body["total"]) == Decimal("50.00")
    assert body["fx_fallback"] is True


@pytest.mark.asyncio
async def test_an_unknown_group_is_not_found(client, auth_headers):
    assert (await client.get(f"/api/groups/{uuid.uuid4()}/summary", headers=auth_headers)).status_code == 404
