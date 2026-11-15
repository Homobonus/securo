"""Who spent what: a workspace's income and expenses per person.

A household shares one workspace while each person keeps their own
accounts, so "how much did each of us spend this month" is a question of
whose account the money left. Every account has an owner (the user who
added or connected it), and a transaction counts for that owner.

The rules are the income/expenses report's, so the people add up to its
totals: posted rows only, no opening balances, no transfers between
accounts (including between two people's accounts) and nothing a category
marks as ignored; a row split across categories counts part by part.
Amounts are what was paid: a trip's group split does not move money
between people here.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.services._query_filters import (
    counts_as_user_pnl,
    line_category_id,
    line_primary_or_amount,
    reporting_date_col,
    with_category_lines,
)
from app.services.admin_service import get_credit_card_accounting_mode

CENT = Decimal("0.01")


def month_start(day: date) -> date:
    return day.replace(day=1)


def shift_months(first: date, months: int) -> date:
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(first: date) -> date:
    return shift_months(first, 1) - timedelta(days=1)


def _label(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).copy_abs().quantize(CENT)


def display_name(user: User) -> str:
    prefs = user.preferences or {}
    name = prefs.get("display_name") if isinstance(prefs, dict) else None
    return name or user.email.split("@", 1)[0]


@dataclass
class _Window:
    first: date
    last: date


def _pnl_rows(statement, workspace_id: uuid.UUID, report_date, window: _Window):
    return (
        with_category_lines(statement.select_from(Transaction))
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.workspace_id == workspace_id,
            report_date >= window.first,
            report_date <= window.last,
            Transaction.source != "opening_balance",
            Transaction.status == "posted",
            counts_as_user_pnl(line_category_id()),
        )
    )


async def people_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    viewer_id: uuid.UUID,
    currency: str,
    month: date,
    months: int,
) -> dict:
    """`months` months of income and expenses per person, ending with `month`,
    plus that month's expenses by category."""
    selected = month_start(month)
    trend = _Window(shift_months(selected, -(months - 1)), month_end(selected))
    focus = _Window(selected, month_end(selected))

    accounts = (
        await session.execute(
            select(Account.id, Account.user_id)
            .where(Account.workspace_id == workspace_id)
            .order_by(Account.name)
        )
    ).all()
    account_ids: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for account_id, owner_id in accounts:
        account_ids[owner_id].append(account_id)
    users = (
        await session.execute(select(User).where(User.id.in_(list(account_ids))))
    ).scalars().all()
    # The viewer first, then by name: "you" is the column read most.
    users = sorted(users, key=lambda user: (user.id != viewer_id, display_name(user).casefold()))
    people = [
        {
            "user_id": user.id,
            "name": display_name(user),
            "is_you": user.id == viewer_id,
            "account_ids": account_ids[user.id],
        }
        for user in users
    ]

    report_date = reporting_date_col(await get_credit_card_accounting_mode(session))
    amount = line_primary_or_amount()

    # By day rather than by month in SQL: the bucket is cut here, the same
    # on PostgreSQL and on the SQLite the tests run on.
    totals: dict[tuple[str, uuid.UUID], dict[str, Decimal]] = defaultdict(
        lambda: {"income": Decimal("0"), "expenses": Decimal("0")}
    )
    rows = await session.execute(
        _pnl_rows(
            select(Account.user_id, report_date, Transaction.type, func.sum(amount)),
            workspace_id,
            report_date,
            trend,
        ).group_by(Account.user_id, report_date, Transaction.type)
    )
    for owner_id, day, kind, value in rows.all():
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        key = "income" if kind == "credit" else "expenses" if kind == "debit" else None
        if key is not None:
            totals[(_label(day), owner_id)][key] += _money(value)

    series = []
    cursor = trend.first
    while cursor <= trend.last:
        for person in people:
            bucket = totals.get((_label(cursor), person["user_id"]))
            series.append(
                {
                    "month": _label(cursor),
                    "user_id": person["user_id"],
                    "income": bucket["income"] if bucket else Decimal("0"),
                    "expenses": bucket["expenses"] if bucket else Decimal("0"),
                }
            )
        cursor = shift_months(cursor, 1)

    category = line_category_id().label("category_id")
    by_category: dict[Optional[uuid.UUID], dict[uuid.UUID, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    rows = await session.execute(
        _pnl_rows(
            select(Account.user_id, category, func.sum(amount)),
            workspace_id,
            report_date,
            focus,
        )
        .where(Transaction.type == "debit")
        .group_by(Account.user_id, category)
    )
    for owner_id, category_id, value in rows.all():
        by_category[category_id][owner_id] += _money(value)

    names = {
        row.id: row
        for row in (
            await session.execute(
                select(Category.id, Category.name, Category.color).where(
                    Category.id.in_([cid for cid in by_category if cid is not None])
                )
            )
        ).all()
    }
    spent = {
        category_id: (sum(amounts.values(), Decimal("0")), amounts)
        for category_id, amounts in by_category.items()
    }
    categories = [
        {
            "category_id": category_id,
            "name": names[category_id].name if category_id in names else None,
            "color": names[category_id].color if category_id in names else None,
            "amounts": {str(owner): value for owner, value in amounts.items() if value},
            "total": total,
        }
        for category_id, (total, amounts) in sorted(
            spent.items(), key=lambda item: item[1][0], reverse=True
        )
        if total
    ]

    return {
        "currency": currency,
        "month": _label(selected),
        "people": people,
        "months": series,
        "categories": categories,
    }
