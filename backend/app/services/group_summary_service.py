"""Group spending aggregates.

The group page shows three figures that describe the group as a whole:
how much has moved through it, how that splits by month, and how it
splits by category. They are computed here, over every transaction of
the group, for the same reason balances are: the recent-transactions list
is capped at 20 rows, so deriving totals from it describes the last few
weeks while claiming to describe the group.

Amounts are in the group's own currency: each row is converted at the
rate of its own date, so a trip kept in EUR shows EUR whatever currency
the cards were charged in, and whoever looks at it. A row with no rate
counts 1:1 and sets ``fx_fallback``. (``amount_primary`` would be the
recording user's currency, labelled with the group's.)
"""

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.group import GroupMember
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services.fx_rate_service import convert_or_none

CENT = Decimal("0.01")


def _group_transactions(group_id: uuid.UUID):
    """Transactions carrying a split for any member of this group."""
    member_ids = select(GroupMember.id).where(GroupMember.group_id == group_id)
    return (
        select(TransactionSplit.transaction_id)
        .where(TransactionSplit.group_member_id.in_(member_ids))
        .distinct()
    )


async def compute_summary(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[dict]:
    # Same visibility rule as balances: workspace members plus
    # cross-workspace linked members all see the same totals.
    from app.services.group_service import get_group_visible

    group = await get_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None
    currency = group.default_currency

    transactions = (
        await session.execute(
            select(Transaction)
            .where(
                Transaction.id.in_(_group_transactions(group_id)),
                Transaction.is_ignored.is_(False),
            )
            .options(selectinload(Transaction.category))
        )
    ).scalars().all()

    by_month: dict[str, Decimal] = defaultdict(Decimal)
    by_category: dict[Optional[uuid.UUID], dict] = {}
    fallback = False
    for tx in transactions:
        amount = Decimal(str(tx.amount)).copy_abs()
        converted, _ = await convert_or_none(session, amount, tx.currency, currency, tx.date)
        if converted is None:
            converted, fallback = amount, True
        # "YYYY-MM": the client formats it for display.
        by_month[tx.date.strftime("%Y-%m")] += converted
        # Spending only: credits coming back into the group aren't spend.
        if tx.type != "debit":
            continue
        category = tx.category
        key = category.id if category else None
        entry = by_category.setdefault(
            key,
            {
                "category_id": key,
                "name": category.name if category else None,
                "color": category.color if category else None,
                "total": Decimal("0"),
            },
        )
        entry["total"] += converted

    months = [
        {"month": month, "total": value.quantize(CENT)} for month, value in sorted(by_month.items())
    ]
    # The headline figure is the same row set, bucketed differently;
    # summing the buckets keeps the two from ever disagreeing.
    total = sum((m["total"] for m in months), Decimal("0"))
    categories = sorted(
        ({**entry, "total": entry["total"].quantize(CENT)} for entry in by_category.values()),
        key=lambda entry: entry["total"],
        reverse=True,
    )
    return {
        "group_id": group_id,
        "default_currency": currency,
        "total_moved": total,
        "months": months,
        "categories": categories,
        "fx_fallback": fallback,
    }
