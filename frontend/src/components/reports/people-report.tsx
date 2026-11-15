import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChevronLeft, ChevronRight, TrendingDown, TrendingUp } from 'lucide-react'
import { reports } from '@/lib/api'
import { formatCompactCurrency, formatCurrency } from '@/lib/format'
import { currentMonth, monthLabel, monthRange, shiftMonth } from '@/lib/month-utils'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Skeleton } from '@/components/ui/skeleton'
import type { PeopleReport, PeopleReportPerson } from '@/types'

// One colour per person, the same in the cards, the chart and the table.
const PERSON_COLORS = ['#6366F1', '#F59E0B', '#10B981', '#EC4899', '#0EA5E9', '#8B5CF6']
const TREND_MONTHS = 6

const card = 'bg-card rounded-xl border border-border shadow-sm'

/** Who spent what: each person's spending in a month, by whose account paid. */
export function PeopleReportView() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const [month, setMonth] = useState(currentMonth)

  const { data, isLoading, isError } = useQuery<PeopleReport>({
    queryKey: ['reports', 'people', month, TREND_MONTHS],
    queryFn: () => reports.people(month, TREND_MONTHS),
    placeholderData: keepPreviousData,
  })

  const currency = data?.currency ?? 'PLN'
  const money = (value: number) => mask(formatCurrency(value, currency, locale))
  const people = useMemo(() => data?.people ?? [], [data])
  const color = (index: number) => PERSON_COLORS[index % PERSON_COLORS.length]

  const totals = useMemo(() => {
    const at = (m: string, userId: string) =>
      data?.months.find((row) => row.month === m && row.user_id === userId)
    const previous = shiftMonth(month, -1)
    return people.map((person) => {
      const now = at(month, person.user_id)
      const before = at(previous, person.user_id)
      const expenses = Number(now?.expenses ?? 0)
      const prior = Number(before?.expenses ?? 0)
      return {
        person,
        expenses,
        income: Number(now?.income ?? 0),
        // No change to show when there was nothing to compare with.
        change: prior > 0 ? ((expenses - prior) / prior) * 100 : null,
      }
    })
  }, [data, people, month])

  const chartData = useMemo(() => {
    const byMonth = new Map<string, Record<string, number | string>>()
    for (const row of data?.months ?? []) {
      const entry = byMonth.get(row.month) ?? {
        month: new Date(Number(row.month.slice(0, 4)), Number(row.month.slice(5, 7)) - 1, 1)
          .toLocaleString(dateLocale, { month: 'short' }),
      }
      entry[row.user_id] = Number(row.expenses)
      byMonth.set(row.month, entry)
    }
    return [...byMonth.values()]
  }, [data, dateLocale])

  // Their spending in the month, filtered as this report counts it.
  const openTransactions = (person: PeopleReportPerson, categoryId?: string | null) => {
    if (person.account_ids.length === 0) return
    const { from, to } = monthRange(month)
    const params = new URLSearchParams({ account_id: person.account_ids.join(','), from, to, type: 'debit' })
    if (categoryId) params.set('category_id', categoryId)
    navigate(`/transactions?${params.toString()}`)
  }

  const totalSpent = totals.reduce((sum, row) => sum + row.expenses, 0)

  return (
    <div data-testid="people-report">
      <div className="flex items-center justify-between gap-3 mb-4">
        <p className="text-sm text-muted-foreground max-w-xl">{t('reports.peopleReport.hint')}</p>
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => setMonth((m) => shiftMonth(m, -1))}
            className="h-8 w-8 inline-flex items-center justify-center rounded-md hover:bg-muted text-muted-foreground"
            aria-label={t('reports.peopleReport.previousMonth')}
          >
            <ChevronLeft size={16} />
          </button>
          <span className="min-w-36 text-center text-sm font-semibold capitalize" data-testid="people-report-month">
            {monthLabel(month, dateLocale)}
          </span>
          <button
            type="button"
            onClick={() => setMonth((m) => shiftMonth(m, 1))}
            disabled={month >= currentMonth()}
            className="h-8 w-8 inline-flex items-center justify-center rounded-md hover:bg-muted text-muted-foreground disabled:opacity-40"
            aria-label={t('reports.peopleReport.nextMonth')}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      {isError ? (
        <p className="text-sm text-rose-600 dark:text-rose-400">{t('reports.loadError')}</p>
      ) : isLoading || !data ? (
        <div className="grid gap-3 sm:grid-cols-3 mb-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      ) : people.length === 0 ? (
        <p className="text-sm text-muted-foreground py-10 text-center">{t('reports.noData')}</p>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mb-5">
            {totals.map(({ person, expenses, income, change }, index) => (
              <button
                key={person.user_id}
                type="button"
                onClick={() => openTransactions(person)}
                className={`${card} p-4 text-left hover:border-primary/40 transition-colors`}
                data-testid="people-report-person"
              >
                <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color(index) }} />
                  <span className="truncate">
                    {person.name}
                    {person.is_you && ` (${t('reports.peopleReport.you')})`}
                  </span>
                </div>
                <p className="mt-1.5 text-2xl font-bold tabular-nums">{money(expenses)}</p>
                <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                  {change === null ? (
                    <span>{t('reports.peopleReport.noComparison')}</span>
                  ) : (
                    <span
                      className={`inline-flex items-center gap-1 font-semibold ${
                        change > 0 ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'
                      }`}
                    >
                      {change > 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                      {t('reports.peopleReport.vsPrevious', { change: `${change > 0 ? '+' : ''}${change.toFixed(0)}%` })}
                    </span>
                  )}
                </div>
                <div className="mt-2 flex justify-between gap-2 text-xs text-muted-foreground tabular-nums">
                  <span>{t('reports.peopleReport.income')}: {money(income)}</span>
                  <span>{t('reports.peopleReport.balance')}: {money(income - expenses)}</span>
                </div>
              </button>
            ))}
            {people.length > 1 && (
              <div className={`${card} p-4`} data-testid="people-report-total">
                <div className="text-xs font-medium text-muted-foreground">{t('reports.peopleReport.total')}</div>
                <p className="mt-1.5 text-2xl font-bold tabular-nums">{money(totalSpent)}</p>
                <div className="mt-2 flex h-2 overflow-hidden rounded-full bg-muted">
                  {totalSpent > 0 &&
                    totals.map(({ person, expenses }, index) => (
                      <span
                        key={person.user_id}
                        style={{ width: `${(expenses / totalSpent) * 100}%`, backgroundColor: color(index) }}
                      />
                    ))}
                </div>
                <div className="mt-2 space-y-0.5 text-xs text-muted-foreground tabular-nums">
                  {totals.map(({ person, expenses }) => (
                    <div key={person.user_id} className="flex justify-between gap-2">
                      <span className="truncate">{person.name}</span>
                      <span>{totalSpent > 0 ? `${((expenses / totalSpent) * 100).toFixed(0)}%` : '—'}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className={`${card} p-4 mb-5`}>
            <h3 className="text-sm font-semibold mb-3">
              {t('reports.peopleReport.trendTitle', { count: TREND_MONTHS })}
            </h3>
            <div className="h-64" data-testid="people-report-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="month" tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }} axisLine={false} tickLine={false} />
                  <YAxis
                    width={64}
                    tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(value: number) => mask(formatCompactCurrency(value, currency, locale))}
                  />
                  <Tooltip
                    cursor={{ fill: 'var(--muted)', opacity: 0.4 }}
                    contentStyle={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
                    formatter={(value) => money(Number(value))}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  {people.map((person, index) => (
                    <Bar
                      key={person.user_id}
                      dataKey={person.user_id}
                      name={person.name}
                      fill={color(index)}
                      radius={[3, 3, 0, 0]}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className={card}>
            <h3 className="text-sm font-semibold px-4 pt-4 pb-2">
              {t('reports.peopleReport.categoriesTitle', { month: monthLabel(month, dateLocale) })}
            </h3>
            {data.categories.length === 0 ? (
              <p className="px-4 pb-6 pt-2 text-sm text-muted-foreground">{t('reports.peopleReport.noSpending')}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="text-left font-medium py-2 pl-4">{t('reports.peopleReport.category')}</th>
                      {people.map((person, index) => (
                        <th key={person.user_id} className="text-right font-medium py-2 px-3 whitespace-nowrap">
                          <span className="inline-flex items-center gap-1.5">
                            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color(index) }} />
                            {person.name}
                          </span>
                        </th>
                      ))}
                      {people.length > 1 && (
                        <th className="text-right font-medium py-2 pl-3 pr-4">{t('reports.peopleReport.total')}</th>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {data.categories.map((category) => (
                      <tr key={category.category_id ?? 'none'} className="border-b border-border last:border-0" data-testid="people-report-category">
                        <td className="py-2 pl-4">
                          <span className="inline-flex items-center gap-2">
                            <span className="h-2.5 w-2.5 rounded-full shrink-0" style={{ backgroundColor: category.color ?? '#6B7280' }} />
                            {category.name ?? t('reports.peopleReport.uncategorized')}
                          </span>
                        </td>
                        {people.map((person) => {
                          const value = Number(category.amounts[person.user_id] ?? 0)
                          return (
                            <td key={person.user_id} className="py-2 px-3 text-right tabular-nums">
                              {value ? (
                                <button
                                  type="button"
                                  onClick={() => openTransactions(person, category.category_id)}
                                  className="hover:text-primary hover:underline"
                                >
                                  {money(value)}
                                </button>
                              ) : (
                                <span className="text-muted-foreground">—</span>
                              )}
                            </td>
                          )
                        })}
                        {people.length > 1 && (
                          <td className="py-2 pl-3 pr-4 text-right tabular-nums font-medium">{money(Number(category.total))}</td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
