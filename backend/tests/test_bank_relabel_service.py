"""Rows synced before the provider learned to read a merchant from the address."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.payee import Payee
from app.models.transaction import Transaction
from app.services.bank_relabel_service import relabel_enable_banking_transactions

RAW = {
    "creditor": {"name": None, "postal_address": {"address_line": ["JMP S.A. BIEDRONKA 5703   KATOWICE"]}},
    "booking_date": "2026-10-26",
    "transaction_amount": {"amount": "118.88", "currency": "PLN"},
    "credit_debit_indicator": "DBIT",
    "remittance_information": ["Płatność kartą 24.10.2026 Nr karty 4246xx4588"],
}
OLD = "Płatność kartą 24.10.2026 Nr karty 4246xx4588"


async def seed(session, user, **tx_fields) -> Transaction:
    connection = BankConnection(
        id=uuid.uuid4(), user_id=user.id, provider="enable_banking", external_id="s1",
        institution_name="ING", settings={},
    )
    account = Account(
        id=uuid.uuid4(), user_id=user.id, connection_id=connection.id, name="ING",
        type="checking", balance=Decimal("0"), currency="PLN",
    )
    fields = {"description": OLD, "original_description": OLD, "payee": "", **tx_fields}
    tx = Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, amount=Decimal("118.88"),
        currency="PLN", date=date(2026, 10, 26), type="debit", source="sync", raw_data=RAW, **fields,
    )
    session.add_all([connection, account, tx])
    await session.commit()
    return tx


@pytest.mark.asyncio
async def test_old_rows_get_the_merchant(session, test_user):
    tx = await seed(session, test_user)
    result = await relabel_enable_banking_transactions(session)
    await session.commit()
    await session.refresh(tx)
    assert (result.checked, result.described, result.payees) == (1, 1, 1)
    assert tx.description == f"JMP S.A. BIEDRONKA 5703 · {OLD}"
    assert tx.payee == "JMP S.A. BIEDRONKA 5703"
    payee = await session.get(Payee, tx.payee_id)
    assert payee is not None and payee.name == "JMP S.A. BIEDRONKA 5703"


@pytest.mark.asyncio
async def test_hand_edits_and_rules_are_left_alone(session, test_user):
    edited = await seed(session, test_user, description="Zakupy spożywcze")
    ruled = await seed(session, test_user, description_is_rule_managed=True)
    await relabel_enable_banking_transactions(session)
    await session.commit()
    await session.refresh(edited)
    await session.refresh(ruled)
    assert edited.description == "Zakupy spożywcze"
    assert ruled.description == OLD
    # The payee is still filled in: it was empty, nobody chose it.
    assert edited.payee == ruled.payee == "JMP S.A. BIEDRONKA 5703"


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing_more(session, test_user):
    await seed(session, test_user)
    await relabel_enable_banking_transactions(session)
    await session.commit()
    again = await relabel_enable_banking_transactions(session)
    assert (again.described, again.payees) == (0, 0)
    count = len((await session.execute(select(Payee).where(Payee.name == "JMP S.A. BIEDRONKA 5703"))).scalars().all())
    assert count == 1
