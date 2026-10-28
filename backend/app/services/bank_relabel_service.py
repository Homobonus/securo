"""Re-derive what synced Enable Banking rows say, from the payload they keep.

When the provider learns to read more of a bank's payload (the merchant
ING prints only in the creditor's address), rows synced before that stay
as they were: a re-sync matches them by id and never rewrites them. This
walks those rows once and applies what the provider would produce today.

It respects people: a description that was edited by hand or is managed
by a rule is left alone, and a payee already set is never replaced.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction
from app.providers.enable_banking import describe_transaction
from app.services.payee_service import get_or_create_payee


@dataclass
class RelabelResult:
    checked: int = 0
    described: int = 0
    payees: int = 0


async def relabel_enable_banking_transactions(
    session: AsyncSession, *, workspace_id: Optional[uuid.UUID] = None
) -> RelabelResult:
    statement = (
        select(Transaction, BankConnection.settings)
        .join(Account, Account.id == Transaction.account_id)
        .join(BankConnection, BankConnection.id == Account.connection_id)
        .where(
            BankConnection.provider == "enable_banking",
            Transaction.source == "sync",
            Transaction.raw_data.is_not(None),
        )
    )
    if workspace_id is not None:
        statement = statement.where(Transaction.workspace_id == workspace_id)
    result = RelabelResult()
    for tx, settings in (await session.execute(statement)).all():
        if not isinstance(tx.raw_data, dict):
            continue
        result.checked += 1
        description, payee = describe_transaction(
            tx.raw_data, (settings or {}).get("payee_source", "auto")
        )
        if payee and not tx.payee:
            tx.payee = payee[:500]
            if tx.payee_id is None:
                entity = await get_or_create_payee(
                    session, tx.user_id, tx.payee, workspace_id=tx.workspace_id
                )
                tx.payee_id = entity.id
            result.payees += 1
        untouched = tx.original_description is None or tx.description == tx.original_description
        if untouched and not tx.description_is_rule_managed and description != tx.description:
            tx.description = description
            tx.original_description = description
            result.described += 1
    await session.flush()
    return result
