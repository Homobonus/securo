import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

import { BulkAddToGroupDialog } from '@/components/bulk-add-to-group-dialog'
import { renderWithProviders, t } from '@/test/utils'
import type { Group, GroupMember } from '@/types'

const api = vi.hoisted(() => ({
  groups: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    members: { create: vi.fn() },
  },
}))

vi.mock('@/lib/api', () => ({ groups: api.groups }))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { id: 'me', preferences: { currency_display: 'PLN' } } }),
}))

function member(overrides: Partial<GroupMember> = {}): GroupMember {
  return {
    id: 'm1',
    group_id: 'trip',
    name: 'Owner',
    linked_user_id: null,
    email: null,
    is_self: false,
    created_at: '2026-11-01T00:00:00Z',
    ...overrides,
  }
}

function group(overrides: Partial<Group> = {}): Group {
  return {
    id: 'trip',
    user_id: 'owner',
    name: 'Włochy 2026',
    kind: 'social',
    default_currency: 'PLN',
    icon: 'users',
    color: '#6B7280',
    is_archived: false,
    is_owner: false,
    notes: null,
    created_at: '2026-11-01T00:00:00Z',
    members: [member({ id: 'owner-m', linked_user_id: 'owner' }), member({ id: 'me-m', name: 'Me', linked_user_id: 'me' })],
    ...overrides,
  }
}

function open() {
  return renderWithProviders(
    <BulkAddToGroupDialog open onClose={() => {}} selectedCount={2} onSubmit={() => {}} isPending={false} />,
  )
}

describe('BulkAddToGroupDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('offers a group shared with the viewer as a linked member', async () => {
    const trip = group()
    api.groups.list.mockResolvedValue([trip])
    api.groups.get.mockResolvedValue(trip)
    open()

    expect(await screen.findByRole('option', { name: 'Włochy 2026' })).toBeInTheDocument()
    expect(screen.queryByText(t('splitGroups.splitNoGroups'))).not.toBeInTheDocument()
    // Members are the owner's to manage.
    expect(screen.queryByText(`+ ${t('splitGroups.addMember')}`)).not.toBeInTheDocument()
  })

  it('leaves out groups the viewer only sees from the workspace', async () => {
    api.groups.list.mockResolvedValue([group({ members: [member({ linked_user_id: 'owner' })] })])
    open()

    expect(await screen.findByText(t('splitGroups.splitNoGroups'))).toBeInTheDocument()
  })

  it('lets the owner add members', async () => {
    const own = group({ is_owner: true, user_id: 'me' })
    api.groups.list.mockResolvedValue([own])
    api.groups.get.mockResolvedValue(own)
    open()

    expect(await screen.findByText(`+ ${t('splitGroups.addMember')}`)).toBeInTheDocument()
  })
})
