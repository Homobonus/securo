import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'

import { PeopleReportView } from '@/components/reports/people-report'
import { currentMonth, shiftMonth } from '@/lib/month-utils'
import { renderWithProviders, t } from '@/test/utils'
import type { PeopleReport } from '@/types'

const api = vi.hoisted(() => ({ reports: { people: vi.fn() } }))
vi.mock('@/lib/api', () => ({ reports: api.reports }))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

const month = currentMonth()
const previous = shiftMonth(month, -1)

function report(): PeopleReport {
  return {
    currency: 'PLN',
    month,
    people: [
      { user_id: 'me', name: 'Mikołaj', is_you: true, account_ids: ['ing', 'rev'] },
      { user_id: 'ola', name: 'Ola', is_you: false, account_ids: ['mbank'] },
    ],
    months: [
      { month: previous, user_id: 'me', income: '0', expenses: '400.00' },
      { month: previous, user_id: 'ola', income: '0', expenses: '0' },
      { month, user_id: 'me', income: '5000.00', expenses: '500.00' },
      { month, user_id: 'ola', income: '0', expenses: '300.00' },
    ],
    categories: [
      { category_id: 'food', name: 'Jedzenie', color: '#f00', amounts: { me: '200.00', ola: '300.00' }, total: '500.00' },
      { category_id: null, name: null, color: null, amounts: { me: '300.00' }, total: '300.00' },
    ],
  }
}

function Where() {
  const location = useLocation()
  return <div data-testid="where">{location.pathname + location.search}</div>
}

function render() {
  return renderWithProviders(
    <Routes>
      <Route path="/reports" element={<PeopleReportView />} />
      <Route path="/transactions" element={<Where />} />
    </Routes>,
    { route: '/reports' },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.reports.people.mockResolvedValue(report())
})

describe('PeopleReportView', () => {
  it('shows what each person spent, with the change against last month', async () => {
    render()
    const cards = await screen.findAllByTestId('people-report-person')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveTextContent('Mikołaj')
    expect(cards[0]).toHaveTextContent('PLN 500.00')
    expect(cards[0]).toHaveTextContent(t('reports.peopleReport.vsPrevious', { change: '+25%' }))
    // Nothing to compare with: no percentage made up from a zero.
    expect(cards[1]).toHaveTextContent(t('reports.peopleReport.noComparison'))
    expect(screen.getByTestId('people-report-total')).toHaveTextContent('PLN 800.00')

    const rows = screen.getAllByTestId('people-report-category')
    expect(rows[0]).toHaveTextContent('Jedzenie')
    expect(rows[1]).toHaveTextContent(t('reports.peopleReport.uncategorized'))
    expect(within(rows[1]).getAllByText('—')).toHaveLength(1)
    expect(api.reports.people).toHaveBeenCalledWith(month, 6)
  })

  it('opens the transactions behind an amount', async () => {
    const { user } = render()
    const rows = await screen.findAllByTestId('people-report-category')
    await user.click(within(rows[0]).getByRole('button', { name: /300.00/ }))
    const where = await screen.findByTestId('where')
    expect(where.textContent).toContain('/transactions?account_id=mbank')
    expect(where.textContent).toContain('category_id=food')
    expect(where.textContent).toContain('type=debit')
  })

  it('steps back a month', async () => {
    const { user } = render()
    await screen.findAllByTestId('people-report-person')
    await user.click(screen.getByRole('button', { name: t('reports.peopleReport.previousMonth') }))
    await waitFor(() => expect(api.reports.people).toHaveBeenLastCalledWith(previous, 6))
  })
})
