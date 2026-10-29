import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import ReceiptsPage from '@/pages/receipts'
import ShoppingProductDetailPage from '@/pages/shopping-product-detail'
import { renderWithProviders, t } from '@/test/utils'
import type { Receipt, ReceiptDetail, RetailConnection, ShoppingProduct, ShoppingProductDetail } from '@/types'

const api = vi.hoisted(() => ({
  receipts: {
    list: vi.fn(),
    get: vi.fn(),
    candidates: vi.fn(),
    link: vi.fn(),
    unlink: vi.fn(),
    autoMatch: vi.fn(),
    remove: vi.fn(),
  },
  shoppingProducts: { list: vi.fn(), get: vi.fn(), update: vi.fn(), merge: vi.fn() },
  retail: {
    providers: vi.fn(),
    connections: vi.fn(),
    connect: vi.fn(),
    authorize: vi.fn(),
    reauthorize: vi.fn(),
    update: vi.fn(),
    sync: vi.fn(),
    remove: vi.fn(),
  },
  accounts: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  receipts: api.receipts,
  shoppingProducts: api.shoppingProducts,
  retail: api.retail,
  accounts: api.accounts,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

let canWrite = true
vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite }),
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

function receipt(overrides: Partial<Receipt> = {}): Receipt {
  return {
    id: 'r1',
    source: 'lidl_plus',
    connection_id: 'c1',
    external_id: 't1',
    purchased_at: '2026-10-01T09:00:00Z',
    date: '2026-10-01',
    store_name: 'Lidl Kraków',
    store_address: null,
    total: '34.98',
    discount_total: '0',
    currency: 'PLN',
    transaction_id: null,
    match_method: null,
    transaction: null,
    item_count: 2,
    ...overrides,
  }
}

function product(overrides: Partial<ShoppingProduct> = {}): ShoppingProduct {
  return {
    id: 'p1',
    name: 'Kawa ziarn.',
    category_id: null,
    track_consumption: true,
    notes: null,
    purchase_count: 3,
    total_quantity: '3',
    unit: 'szt',
    total_spent: '74.97',
    first_purchased_at: '2026-09-01',
    last_purchased_at: '2026-09-29',
    last_unit_price: '26.99',
    min_unit_price: '22.99',
    max_unit_price: '26.99',
    price_change_pct: 17.4,
    sources: ['lidl_plus'],
    consumption: {
      purchase_count: 3,
      avg_days_between: 14,
      days_per_unit: 14,
      units_per_month: 2.14,
      monthly_cost: '53.55',
      estimated_depletion: '2026-10-13',
      days_left: 2,
    },
    ...overrides,
  }
}

const connection: RetailConnection = {
  id: 'c1',
  provider: 'biedronka',
  display_name: 'Moja Biedronka',
  status: 'reauth_required',
  last_error: 'token_rejected',
  last_sync_at: null,
  created_at: '2026-10-01T00:00:00Z',
  match_account_id: null,
  receipt_count: 4,
}

beforeEach(() => {
  vi.clearAllMocks()
  canWrite = true
  api.receipts.list.mockResolvedValue({
    items: [
      receipt(),
      receipt({
        id: 'r2',
        source: 'biedronka',
        store_name: null,
        total: '12.00',
        transaction_id: 'tx',
        match_method: 'auto',
        transaction: { id: 'tx', account_id: 'a', description: 'BIEDRONKA 1234', amount: '12.00', currency: 'PLN', date: '2026-10-02' },
      }),
    ],
    total: 2,
  })
  api.shoppingProducts.list.mockResolvedValue([
    product({ id: 'p2', name: 'Chleb', track_consumption: false, consumption: null, purchase_count: 9 }),
    product(),
  ])
  api.retail.providers.mockResolvedValue([
    { name: 'lidl_plus', display_name: 'Lidl Plus', redirect_prefix: 'com.lidlplus.app://callback' },
    { name: 'biedronka', display_name: 'Moja Biedronka', redirect_prefix: 'app://cma20.biedronka.pl' },
  ])
  api.retail.connections.mockResolvedValue([connection])
  api.accounts.list.mockResolvedValue([])
})

describe('ReceiptsPage', () => {
  it('lists receipts with their pairing state', async () => {
    renderWithProviders(<ReceiptsPage />, { route: '/receipts' })
    const rows = await screen.findAllByTestId('receipt-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Lidl Kraków')).toBeInTheDocument()
    expect(within(rows[0]).getByText(t('receipts.list.unpaired'))).toBeInTheDocument()
    expect(within(rows[1]).getByText('BIEDRONKA 1234')).toBeInTheDocument()
  })

  it('opens a receipt and pairs it with a candidate', async () => {
    const detail: ReceiptDetail = {
      ...receipt(),
      items: [
        {
          id: 'i1', position: 0, name: 'Kawa ziarn.', code: '101', quantity: '1.000', unit: 'szt',
          unit_price: '24.99', total: '24.99', discount: '5.00', product_id: 'p1', product_name: 'Kawa',
        },
      ],
    }
    api.receipts.get.mockResolvedValue(detail)
    api.receipts.candidates.mockResolvedValue([
      { id: 'tx9', account_id: 'a', description: 'LIDL KRAKOW', amount: '34.98', currency: 'PLN', date: '2026-10-02' },
    ])
    api.receipts.link.mockResolvedValue({ ...detail, transaction_id: 'tx9' })
    const { user } = renderWithProviders(<ReceiptsPage />, { route: '/receipts' })

    await user.click((await screen.findAllByTestId('receipt-row'))[0])
    const dialog = await screen.findByTestId('receipt-dialog')
    expect(await within(dialog).findByText('Kawa ziarn.')).toBeInTheDocument()
    const candidate = await within(dialog).findByTestId('receipt-candidate')
    await user.click(within(candidate).getByRole('button', { name: t('receipts.detail.link') }))
    await waitFor(() => expect(api.receipts.link).toHaveBeenCalledWith('r1', 'tx9'))
  })

  it('puts the product about to run out first', async () => {
    const { user } = renderWithProviders(<ReceiptsPage />, { route: '/receipts' })
    await user.click(await screen.findByTestId('receipts-tab-products'))
    const rows = await screen.findAllByTestId('shopping-product-row')
    expect(within(rows[0]).getByText('Kawa ziarn.')).toBeInTheDocument()
    expect(within(rows[0]).getByText(t('receipts.products.stock.daysLeft', { days: 2 }))).toBeInTheDocument()
  })

  it('offers a new sign-in for a store that asks for one', async () => {
    api.retail.reauthorize.mockResolvedValue({ connection, auth_url: 'https://konto.biedronka.pl/auth?code_challenge=x' })
    api.retail.authorize.mockResolvedValue({ ...connection, status: 'active' })
    api.retail.sync.mockResolvedValue({ created: 3, updated: 0, matched: 1, has_more: false, connection: { ...connection, status: 'active' } })
    const { user } = renderWithProviders(<ReceiptsPage />, { route: '/receipts?tab=stores' })

    const row = await screen.findByTestId('retail-connection-row')
    expect(within(row).getByTestId('retail-status-reauth_required')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: t('receipts.connections.signIn') }))
    await user.click(await screen.findByRole('button', { name: t('receipts.connect.getLink') }))

    const link = await screen.findByTestId('retail-auth-link')
    expect(link).toHaveAttribute('href', 'https://konto.biedronka.pl/auth?code_challenge=x')
    await user.type(screen.getByTestId('retail-auth-value'), "app://cma20.biedronka.pl?code=abc")
    await user.click(screen.getByTestId('retail-auth-submit'))
    await waitFor(() =>
      expect(api.retail.authorize).toHaveBeenCalledWith('c1', { redirect_url: 'app://cma20.biedronka.pl?code=abc' }),
    )
    await waitFor(() => expect(api.retail.sync).toHaveBeenCalledWith('c1'))
  })

  it('asks Allegro for the session cookie instead of a redirect', async () => {
    api.retail.providers.mockResolvedValue([
      { name: 'allegro', display_name: 'Allegro', redirect_prefix: 'QXLSESSID', auth_kind: 'cookie' },
    ])
    const allegroConnection = { ...connection, provider: 'allegro', display_name: 'Allegro', status: 'pending' as const }
    api.retail.connect.mockResolvedValue({ connection: allegroConnection, auth_url: 'https://allegro.pl/logowanie' })
    api.retail.authorize.mockResolvedValue({ ...allegroConnection, status: 'active' })
    api.retail.sync.mockResolvedValue({ created: 12, updated: 0, matched: 3, failed: 0, has_more: false, connection: { ...allegroConnection, status: 'active' } })
    const { user } = renderWithProviders(<ReceiptsPage />, { route: '/receipts' })

    await user.click(await screen.findByTestId('retail-connect-button'))
    await user.click(await screen.findByTestId('retail-provider-allegro'))
    expect(await screen.findByText(t('receipts.connect.cookieLabel', { name: 'QXLSESSID' }))).toBeInTheDocument()
    expect(screen.queryByText(t('receipts.connect.mode.token'))).not.toBeInTheDocument()
    await user.type(screen.getByTestId('retail-auth-value'), 'QXLSESSID=abc')
    await user.click(screen.getByTestId('retail-auth-submit'))
    await waitFor(() => expect(api.retail.authorize).toHaveBeenCalledWith('c1', { redirect_url: 'QXLSESSID=abc' }))
  })

  it('hides writes from viewers', async () => {
    canWrite = false
    renderWithProviders(<ReceiptsPage />, { route: '/receipts' })
    await screen.findAllByTestId('receipt-row')
    expect(screen.queryByTestId('retail-connect-button')).not.toBeInTheDocument()
  })
})

describe('ShoppingProductDetailPage', () => {
  it('shows the price history and the consumption forecast', async () => {
    const detail: ShoppingProductDetail = {
      ...product(),
      aliases: [{ id: 'a1', source: 'lidl_plus', name: 'Kawa ziarn.' }],
      price_history: [
        { receipt_id: 'r1', date: '2026-09-01', unit_price: '22.99', effective_unit_price: '22.99', quantity: '1', unit: 'szt', store_name: 'Lidl', source: 'lidl_plus' },
        { receipt_id: 'r2', date: '2026-09-29', unit_price: '26.99', effective_unit_price: '21.99', quantity: '1', unit: 'szt', store_name: 'Lidl', source: 'lidl_plus' },
      ],
    }
    api.shoppingProducts.get.mockResolvedValue(detail)
    renderWithProviders(<ShoppingProductDetailPage />, { path: '/receipts/products/:id', route: '/receipts/products/p1' })

    expect(await screen.findByTestId('shopping-product-consumption')).toBeInTheDocument()
    expect(screen.getAllByTestId('price-point')).toHaveLength(2)
    expect(screen.getByText(t('receipts.product.daysPerUnit', { days: '14', unit: 'szt' }))).toBeInTheDocument()
  })

  it('turns tracking on', async () => {
    const detail: ShoppingProductDetail = {
      ...product({ track_consumption: false, consumption: null }),
      aliases: [],
      price_history: [],
    }
    api.shoppingProducts.get.mockResolvedValue(detail)
    api.shoppingProducts.update.mockResolvedValue({ ...detail, track_consumption: true })
    const { user } = renderWithProviders(<ShoppingProductDetailPage />, { path: '/receipts/products/:id', route: '/receipts/products/p1' })

    await user.click(await screen.findByRole('switch'))
    await waitFor(() => expect(api.shoppingProducts.update).toHaveBeenCalledWith('p1', { track_consumption: true }))
  })
})
