import uuid
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ReportBreakdown(BaseModel):
    key: str
    label: str
    value: float
    color: str


class ReportDataRange(BaseModel):
    """Oldest and newest transaction dates the workspace has (issue #765)."""

    min_date: str | None = None
    max_date: str | None = None


class ReportSummary(BaseModel):
    primary_value: float
    change_amount: float
    change_percent: float | None
    breakdowns: list[ReportBreakdown]


class ReportCompositionItem(BaseModel):
    key: str
    label: str
    value: float
    color: str
    group: str


class ReportDataPoint(BaseModel):
    date: str
    value: float
    breakdowns: dict[str, float]
    change: float | None = None
    composition: list[ReportCompositionItem] = []


class ReportMeta(BaseModel):
    type: str
    series_keys: list[str]
    currency: str
    interval: str
    forecast_start_date: str | None = None
    baseline_active: bool = False
    baseline_lookback_days: int | None = None
    # Cash flow only. ``runway_date`` is the first day (YYYY-MM-DD) the
    # projected balance goes below zero, or None when it stays at or above
    # zero through the horizon. ``lowest_balance`` is the minimum projected
    # balance from today to the end of the horizon, on ``lowest_balance_date``.
    runway_date: str | None = None
    lowest_balance: float | None = None
    lowest_balance_date: str | None = None


class CategoryTrendItem(BaseModel):
    key: str
    label: str
    color: str
    total: float
    group: str
    series: list[ReportDataPoint]


class ReportResponse(BaseModel):
    summary: ReportSummary
    trend: list[ReportDataPoint]
    meta: ReportMeta
    composition: list[ReportCompositionItem] = []
    category_trend: list[CategoryTrendItem] = []


# ---------------------------------------------------------------------------
# Who spent what (people report)
# ---------------------------------------------------------------------------
class PeopleReportPerson(BaseModel):
    user_id: uuid.UUID
    name: str
    #: The person looking at the report.
    is_you: bool = False
    #: Their accounts in this workspace, to open their transactions.
    account_ids: list[uuid.UUID] = []


class PeopleReportMonth(BaseModel):
    month: str  # YYYY-MM
    user_id: uuid.UUID
    income: Decimal
    expenses: Decimal


class PeopleReportCategory(BaseModel):
    #: None for expenses without a category.
    category_id: Optional[uuid.UUID] = None
    name: Optional[str] = None
    color: Optional[str] = None
    #: Spent in the category, by user id; people who spent nothing are absent.
    amounts: dict[str, Decimal] = {}
    total: Decimal


class PeopleReport(BaseModel):
    """A workspace's income and expenses per person, by whose account paid."""

    currency: str
    month: str  # the month the categories describe, YYYY-MM
    people: list[PeopleReportPerson]
    #: One row per month and person, oldest first, zeros included.
    months: list[PeopleReportMonth]
    #: The selected month's expenses by category, largest first.
    categories: list[PeopleReportCategory]
class CategoryStatementRow(BaseModel):
    """One line of the income & expense statement.

    ``values`` are signed net amounts in the primary currency (credits
    positive, debits negative), aligned with ``CategoryStatementResponse.months``.
    """

    key: str
    kind: str  # "group" | "category" | "uncategorized"
    label: str | None
    icon: str | None = None
    color: str | None = None
    category_ids: list[str] = []
    # Set on the uncategorized rows only: they are split by transaction type,
    # so a drill-down has to filter by it to match the row.
    txn_type: str | None = None
    values: list[float]
    children: list["CategoryStatementRow"] = []


class CategoryStatementSection(BaseModel):
    totals: list[float]
    rows: list[CategoryStatementRow]


class CategoryStatementResponse(BaseModel):
    currency: str
    # "YYYY-MM", newest first: the selected month, then the ones before it.
    months: list[str]
    income: CategoryStatementSection
    expenses: CategoryStatementSection
