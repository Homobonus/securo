import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.fx_rate import FxRate
from app.models.transaction import Transaction
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.group_settlement import GroupSettlementCreate
from app.schemas.transaction_split import (
    TransactionSplitInput,
    TransactionSplitsInput,
)
from app.services import (
    balance_service,
    group_service,
    settlement_service,
    split_service,
)


async def _make_account(session: AsyncSession, user_id) -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=user_id,
        name="W",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    return account


async def _make_tx(session, user_id, account_id, amount, currency="USD") -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        account_id=account_id,
        description="t",
        amount=Decimal(amount),
        currency=currency,
        date=date.today(),
        type="debit",
        source="manual",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _setup(session, test_user, workspace_id, n_others=2):
    group = await group_service.create_group(
        session, workspace_id, test_user.id, GroupCreate(name="Trip")
    )
    self_member = await group_service.create_member(
        session,
        group.id,
        workspace_id,
        GroupMemberCreate(name="Me", is_self=True),
    )
    others = []
    for i in range(n_others):
        m = await group_service.create_member(
            session,
            group.id,
            workspace_id,
            GroupMemberCreate(name=f"Friend{i}"),
        )
        others.append(m)
    return group, self_member, others


@pytest.mark.asyncio
async def test_balances_each_friend_owes_their_share(
    session: AsyncSession, test_user, test_workspace
):
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=3
    )
    account = await _make_account(session, test_user.id)
    tx = await _make_tx(session, test_user.id, account.id, "120.00")

    payload = TransactionSplitsInput(
        share_type="equal",
        splits=[
            TransactionSplitInput(group_member_id=self_m.id),
            *[TransactionSplitInput(group_member_id=f.id) for f in friends],
        ],
    )
    await split_service.replace_splits(session, tx, payload, test_user.id)
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    by_member = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    # Each friend owes 30.00, self balance line excluded.
    for f in friends:
        assert by_member[f.id] == Decimal("30.00")
    assert self_m.id not in by_member


@pytest.mark.asyncio
async def test_settlement_reduces_balance(
    session: AsyncSession, test_user, test_workspace
):
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=2
    )
    friend = friends[0]
    account = await _make_account(session, test_user.id)
    tx = await _make_tx(session, test_user.id, account.id, "100.00")

    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="exact",
            splits=[
                TransactionSplitInput(
                    group_member_id=self_m.id, share_amount=Decimal("60.00")
                ),
                TransactionSplitInput(
                    group_member_id=friend.id, share_amount=Decimal("40.00")
                ),
            ],
        ),
        test_user.id,
    )
    # Friend pays back 25 of the 40 they owe.
    await settlement_service.create_settlement(
        session,
        group.id,
        test_workspace.id,
        test_user.id,
        GroupSettlementCreate(
            from_member_id=friend.id,
            to_member_id=self_m.id,
            amount=Decimal("25.00"),
            currency="USD",
            date=date.today(),
        ),
    )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    by_member = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    assert by_member[friend.id] == Decimal("15.00")


@pytest.mark.asyncio
async def test_self_loan_increases_balance_owed(
    session: AsyncSession, test_user, test_workspace
):
    """Self lending money to a friend should make them owe more, even
    without an associated split (e.g. a direct cash advance)."""
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=1
    )
    friend = friends[0]

    await settlement_service.create_settlement(
        session,
        group.id,
        test_workspace.id,
        test_user.id,
        GroupSettlementCreate(
            from_member_id=self_m.id,
            to_member_id=friend.id,
            amount=Decimal("50.00"),
            currency="USD",
            date=date.today(),
        ),
    )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    by_member = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    assert by_member[friend.id] == Decimal("50.00")


@pytest.mark.asyncio
async def test_balances_in_two_currencies_add_up_in_the_group_currency(
    session: AsyncSession, test_user, test_workspace
):
    """Shares are converted at their date's rate and summed into one line,
    so debts in different currencies can net out against each other."""
    session.add(FxRate(base_currency="USD", quote_currency="EUR", date=date.today(), rate=Decimal("0.80"), source="test"))
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=1
    )
    friend = friends[0]
    account = await _make_account(session, test_user.id)

    tx_usd = await _make_tx(session, test_user.id, account.id, "60.00", currency="USD")
    tx_eur = await _make_tx(session, test_user.id, account.id, "40.00", currency="EUR")

    for tx in (tx_usd, tx_eur):
        await split_service.replace_splits(
            session,
            tx,
            TransactionSplitsInput(
                share_type="equal",
                splits=[
                    TransactionSplitInput(group_member_id=self_m.id),
                    TransactionSplitInput(group_member_id=friend.id),
                ],
            ),
            test_user.id,
        )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    lines = [ln for ln in balances["lines"] if ln["member_id"] == friend.id]
    # 30 USD + 20 EUR (= 25 USD at 0.80) = 55 USD, one line.
    assert [(ln["currency"], ln["amount"]) for ln in lines] == [("USD", Decimal("55.00"))]
    assert balances["fx_fallback"] is False


@pytest.mark.asyncio
async def test_what_two_people_owe_each_other_nets_out(
    session: AsyncSession, test_user, test_workspace
):
    """A trip kept in EUR: my partner paid in EUR, I paid with a PLN card.
    Each owes the other half; only the difference remains, in EUR, for
    both of us."""
    from app.models.group import GroupMember
    from app.models.user import User

    day = date.today()
    # 1 USD = 0.80 EUR = 4.00 PLN, so 1 EUR = 5.00 PLN.
    session.add_all([
        FxRate(base_currency="USD", quote_currency="EUR", date=day, rate=Decimal("0.80"), source="test"),
        FxRate(base_currency="USD", quote_currency="PLN", date=day, rate=Decimal("4.00"), source="test"),
    ])
    from app.models.workspace import WorkspaceMember

    partner = User(id=uuid.uuid4(), email="ola@example.com", hashed_password="x", is_active=True, is_verified=True)
    session.add(partner)
    await session.flush()
    session.add(WorkspaceMember(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=partner.id, role="editor"))
    group = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Włochy", default_currency="EUR")
    )
    me = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=test_workspace.id, name="Ja", is_self=True, linked_user_id=test_user.id)
    ola = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=test_workspace.id, name="Ola", linked_user_id=partner.id)
    session.add_all([me, ola])
    await session.flush()

    my_account = await _make_account(session, test_user.id)
    her_account = await _make_account(session, partner.id)
    halves = TransactionSplitsInput(
        share_type="equal",
        splits=[TransactionSplitInput(group_member_id=me.id), TransactionSplitInput(group_member_id=ola.id)],
    )
    # She paid 13.82 EUR: I owe her 6.91 EUR.
    hers = await _make_tx(session, partner.id, her_account.id, "13.82", currency="EUR")
    await split_service.replace_splits(session, hers, halves, partner.id)
    # I paid 17.90 PLN: she owes me 8.95 PLN = 1.79 EUR.
    mine = await _make_tx(session, test_user.id, my_account.id, "17.90", currency="PLN")
    await split_service.replace_splits(session, mine, halves, test_user.id)
    await session.commit()

    for viewer, other, expected in ((test_user.id, ola.id, "-5.12"), (partner.id, me.id, "5.12")):
        balances = await balance_service.compute_balances(session, group.id, test_workspace.id, viewer)
        assert balances is not None
        assert [(ln["member_id"], ln["currency"], ln["amount"]) for ln in balances["lines"]] == [
            (other, "EUR", Decimal(expected))
        ]


@pytest.mark.asyncio
async def test_balance_line_dropped_when_settlement_zeroes_out_split(
    session: AsyncSession, test_user, test_workspace
):
    """A friend who pays back exactly their share should drop off the
    balance ledger — no zero-amount line should be returned."""
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=1
    )
    friend = friends[0]
    account = await _make_account(session, test_user.id)
    tx = await _make_tx(session, test_user.id, account.id, "40.00")

    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[
                TransactionSplitInput(group_member_id=self_m.id),
                TransactionSplitInput(group_member_id=friend.id),
            ],
        ),
        test_user.id,
    )
    # Friend owes 20; pays back exactly 20.
    await settlement_service.create_settlement(
        session,
        group.id,
        test_workspace.id,
        test_user.id,
        GroupSettlementCreate(
            from_member_id=friend.id,
            to_member_id=self_m.id,
            amount=Decimal("20.00"),
            currency="USD",
            date=date.today(),
        ),
    )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    friend_lines = [ln for ln in balances["lines"] if ln["member_id"] == friend.id]
    assert friend_lines == []


@pytest.mark.asyncio
async def test_cross_member_settlements_are_ignored_in_owner_ledger(
    session: AsyncSession, test_user, test_workspace
):
    """Settlements between two non-self members don't affect the
    owner-ledger view (balance_service is owner-centric in v1)."""
    group, self_m, friends = await _setup(
        session, test_user, test_workspace.id, n_others=2
    )
    a, b = friends
    account = await _make_account(session, test_user.id)

    # Owner pays $30 split equally with A — A owes $15.
    tx = await _make_tx(session, test_user.id, account.id, "30.00")
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[
                TransactionSplitInput(group_member_id=self_m.id),
                TransactionSplitInput(group_member_id=a.id),
            ],
        ),
        test_user.id,
    )
    # A "settles" with B — neither side is self. Should be a no-op for
    # the owner's balance ledger.
    await settlement_service.create_settlement(
        session,
        group.id,
        test_workspace.id,
        test_user.id,
        GroupSettlementCreate(
            from_member_id=a.id,
            to_member_id=b.id,
            amount=Decimal("100.00"),
            currency="USD",
            date=date.today(),
        ),
    )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    by_member = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    # A still owes their original $15 — not adjusted by the cross-member tx.
    assert by_member[a.id] == Decimal("15.00")
    # B has no obligation to the owner — no line.
    assert b.id not in by_member


@pytest.mark.asyncio
async def test_no_self_member_means_only_split_totals(
    session: AsyncSession, test_user, test_workspace
):
    """Without a self member, settlements can't be applied (cross-member
    settlements are out of scope for v1) — only split shares accumulate."""
    group = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="NoSelf")
    )
    a = await group_service.create_member(
        session, group.id, test_workspace.id, GroupMemberCreate(name="A")
    )
    b = await group_service.create_member(
        session, group.id, test_workspace.id, GroupMemberCreate(name="B")
    )
    assert a is not None
    assert b is not None
    account = await _make_account(session, test_user.id)
    tx = await _make_tx(session, test_user.id, account.id, "20.00")
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[
                TransactionSplitInput(group_member_id=a.id),
                TransactionSplitInput(group_member_id=b.id),
            ],
        ),
        test_user.id,
    )
    await session.commit()

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    by_member = {ln["member_id"]: ln["amount"] for ln in balances["lines"]}
    assert by_member[a.id] == Decimal("10.00")
    assert by_member[b.id] == Decimal("10.00")
