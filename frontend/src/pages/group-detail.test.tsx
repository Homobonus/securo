import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

import GroupDetailPage from '@/pages/group-detail'
import { renderWithProviders, t } from '@/test/utils'
import type { Group, GroupSummary } from '@/types'

const api = vi.hoisted(() => ({
  groups: {
    get: vi.fn(),
    balances: vi.fn(),
    summary: vi.fn(),
    transactions: vi.fn(),
    settlements: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
    members: { create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  },
  accounts: { list: vi.fn() },
  transactions: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  groups: api.groups,
  accounts: api.accounts,
  transactions: api.transactions,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { id: 'me', preferences: { currency_display: 'PLN' } } }),
}))

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite: true }),
}))

const trip: Group = {
  id: 'trip',
  user_id: 'me',
  name: 'Włochy 2026',
  kind: 'social',
  default_currency: 'EUR',
  icon: 'users',
  color: '#6B7280',
  is_archived: false,
  is_owner: true,
  notes: null,
  created_at: '2026-07-01T00:00:00Z',
  members: [],
}

function summary(overrides: Partial<GroupSummary> = {}): GroupSummary {
  return {
    group_id: 'trip',
    currency: 'EUR',
    transaction_count: 2,
    total: '140.00',
    by_month: [{ month: '2026-07', total: '140.00' }],
    by_category: [{ category_id: null, name: null, color: null, total: '140.00' }],
    fx_fallback: false,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.groups.get.mockResolvedValue(trip)
  api.groups.balances.mockResolvedValue({ group_id: 'trip', self_member_id: null, default_currency: 'EUR', lines: [] })
  api.groups.settlements.list.mockResolvedValue([])
  api.accounts.list.mockResolvedValue([])
  // Listed rows are in złoty; the total must not be built from them.
  api.groups.transactions.mockResolvedValue([
    { id: 't1', amount: '500.00', amount_primary: '500.00', currency: 'PLN', date: '2026-07-10', type: 'debit', description: 'Hotel', splits: [] },
  ])
})

describe('GroupDetailPage', () => {
  it('shows the total the server added up in the group currency', async () => {
    api.groups.summary.mockResolvedValue(summary())
    renderWithProviders(<GroupDetailPage />, { path: '/groups/:id', route: '/groups/trip' })

    expect(await screen.findByText('€140.00')).toBeInTheDocument()
    expect(screen.queryByText('€500.00')).not.toBeInTheDocument()
    expect(screen.queryByTestId('group-fx-fallback')).not.toBeInTheDocument()
  })

  it('warns when some row had no exchange rate', async () => {
    api.groups.summary.mockResolvedValue(summary({ fx_fallback: true }))
    renderWithProviders(<GroupDetailPage />, { path: '/groups/:id', route: '/groups/trip' })

    expect(await screen.findByTestId('group-fx-fallback')).toHaveTextContent(t('transactions.fxFallbackBanner'))
  })
})
