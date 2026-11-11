"""Group balance computation.

Each split transaction was paid by one member: the member linked to the
Securo user who recorded it (`Transaction.user_id`), falling back to the
group's stored `is_self` member and then to the member linked to the group
owner. Every other member's split share is a debt from that member to the
payer. Settlements reduce the debt of `from` towards `to`.

That gives one pairwise ledger per group, the same for everyone. The
response projects it onto the caller (`self_member`, the caller's own
member): for each other member M, per currency, a positive amount means M
owes the caller and a negative amount means the caller owes M. Pairs that
don't involve the caller are left out, and so are a member's pairs with
an owner who isn't a member of the group.

Every share and every settlement is converted to the group's default
currency at its own date before it is added, so debts in different
currencies net out: someone owed 6.91 EUR who owes 8.95 PLN back is owed
the difference, in EUR, as one line. A share or settlement without a
rate counts 1:1 and sets `fx_fallback`.
"""

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit

# Stands in for a group owner who isn't one of the group's members.
_GROUP_OWNER = uuid.UUID(int=0)


async def compute_balances(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[dict]:
    # Visible to anyone with read access to the group — workspace members
    # plus cross-workspace linked members. They all see the same
    # who-owes-whom view.
    from app.services.group_service import get_group_visible

    group = await get_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None

    members_result = await session.execute(
        select(GroupMember).where(GroupMember.group_id == group_id)
    )
    members = list(members_result.scalars().all())

    # get_group_visible projects `is_self` onto the caller in memory, so
    # read the stored flag with a column select (it bypasses the identity
    # map): it marks the owner's own member.
    stored_self_ids = set(
        (
            await session.execute(
                select(GroupMember.id).where(
                    GroupMember.group_id == group_id,
                    GroupMember.is_self.is_(True),
                )
            )
        ).scalars().all()
    )
    member_by_user = {m.linked_user_id: m.id for m in members if m.linked_user_id is not None}
    owner_self = next((m for m in members if m.id in stored_self_ids), None)

    # The caller's own member, by the same rule as group_service's "(you)"
    # tag: the member linked to the caller, else the owner's stored
    # unlinked self member when the caller is the owner.
    self_member = next((m for m in members if m.linked_user_id == user_id), None)
    if self_member is None and user_id == group.user_id and owner_self is not None:
        if owner_self.linked_user_id is None:
            self_member = owner_self

    default_payer = owner_self.id if owner_self is not None else None
    if default_payer is None:
        default_payer = member_by_user.get(group.user_id)
    if default_payer is None:
        # The owner recorded it but isn't a member of the group: the shares
        # are owed to the owner, who sees them as before.
        default_payer = _GROUP_OWNER

    from app.services.fx_rate_service import convert_or_none

    default_currency = group.default_currency
    fx_fallback = False

    async def in_group_currency(amount: Decimal, currency: str, on) -> Decimal:
        nonlocal fx_fallback
        converted, _ = await convert_or_none(session, amount, currency, default_currency, on)
        if converted is None:
            fx_fallback = True
            return amount
        return converted

    # (debtor, creditor) -> currency -> Decimal; everything is kept in the
    # group's currency, so the inner key is that one currency.
    owed: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )

    # Splits: each member's share of a transaction someone else paid is
    # what they owe the payer.
    splits_result = await session.execute(
        select(
            TransactionSplit, Transaction.currency, Transaction.user_id, Transaction.date, Transaction.type
        )
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .join(GroupMember, TransactionSplit.group_member_id == GroupMember.id)
        .where(GroupMember.group_id == group_id)
    )
    for split, currency, recorder_id, tx_date, tx_type in splits_result.all():
        recorder = member_by_user.get(recorder_id, default_payer)
        if split.group_member_id == recorder:
            continue
        share = await in_group_currency(Decimal(str(split.share_amount)), currency, tx_date)
        if tx_type == "credit":
            # Income: the recorder received the money, so they owe each
            # other member that member's share.
            owed[(recorder, split.group_member_id)][default_currency] += share
        else:
            owed[(split.group_member_id, recorder)][default_currency] += share

    # Settlements: `from` paid `to`, which reduces what `from` owes `to`.
    settlements_result = await session.execute(
        select(GroupSettlement).where(GroupSettlement.group_id == group_id)
    )
    for s in settlements_result.scalars().all():
        paid = await in_group_currency(Decimal(str(s.amount)), s.currency, s.date)
        owed[(s.from_member_id, s.to_member_id)][default_currency] -= paid

    # Project onto the caller: member_id -> currency -> what they owe the caller.
    totals: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )
    caller = self_member.id if self_member is not None else _GROUP_OWNER
    for (debtor, creditor), by_currency in owed.items():
        # A member can't be shown a debt to or from an owner who has no
        # member row: there is no name or GroupMember id to show it under.
        if self_member is not None and _GROUP_OWNER in (debtor, creditor):
            continue
        for currency, amount in by_currency.items():
            if creditor == caller:
                totals[debtor][currency] += amount
            elif debtor == caller:
                totals[creditor][currency] -= amount

    # One line per member, already netted, in the group's currency.
    lines = []
    for member_id, by_currency in totals.items():
        for currency, amount in by_currency.items():
            amount = amount.quantize(Decimal("0.01"))
            if amount == 0:
                continue
            lines.append(
                {
                    "member_id": member_id,
                    "currency": currency,
                    "amount": amount,
                    "amount_in_default_currency": amount,
                }
            )

    return {
        "group_id": group_id,
        "self_member_id": self_member.id if self_member else None,
        "default_currency": default_currency,
        "lines": lines,
        "fx_fallback": fx_fallback,
    }
