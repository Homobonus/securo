import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link2, Plus, ShoppingBasket, TrendingDown, TrendingUp } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { PageHeader } from '@/components/page-header'
import { SectionCard, Segmented, TH } from '@/components/invoice-ui'
import { ReceiptDialog } from '@/components/receipts/receipt-dialog'
import { RetailConnectDialog } from '@/components/receipts/retail-connect-dialog'
import { RetailConnectionsPanel } from '@/components/receipts/retail-connections-panel'
import { receipts as receiptsApi, shoppingProducts } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { formatDate } from '@/lib/date-range-format'
import { apiErrorMessage, formatQuantity, sourceLabel, stockLevel, type StockLevel } from '@/lib/receipt-utils'
import { cn } from '@/lib/utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useWorkspace } from '@/contexts/workspace-context'
import type { RetailConnection } from '@/types'

/**
 * Store receipts read from loyalty apps, what they add up to as
 * products, and the loyalty accounts they come from.
 *
 * A receipt is worth most next to the bank transaction that paid it:
 * the card line says "LIDL 104,37", the receipt says which twelve
 * things those were. Pairing runs on every sync; what it could not
 * decide is left for a person, in the receipt dialog.
 */
type Tab = 'receipts' | 'products' | 'stores'
type MatchFilter = 'all' | 'unmatched' | 'matched'
type ProductFilter = 'all' | 'tracked'

const PAGE = 50

const STOCK_TONE: Record<StockLevel, string> = {
  unknown: 'text-muted-foreground',
  ok: 'text-foreground',
  low: 'text-amber-600 dark:text-amber-400 font-medium',
  out: 'text-rose-600 dark:text-rose-400 font-medium',
}

export default function ReceiptsPage() {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const tab = (['receipts', 'products', 'stores'] as const).find((v) => v === params.get('tab')) ?? 'receipts'
  const [connectOpen, setConnectOpen] = useState(false)
  const [reconnect, setReconnect] = useState<RetailConnection | null>(null)

  const setTab = (next: Tab) => {
    const copy = new URLSearchParams(params)
    if (next === 'receipts') copy.delete('tab')
    else copy.set('tab', next)
    setParams(copy, { replace: true })
  }

  return (
    <div>
      <PageHeader
        section={t('receipts.section')}
        title={t('receipts.title')}
        action={
          canWrite ? (
            <Button size="sm" onClick={() => setConnectOpen(true)} data-testid="retail-connect-button">
              <Plus className="h-4 w-4 mr-1.5" />
              {t('receipts.connect.title')}
            </Button>
          ) : undefined
        }
      />

      <div className="mb-4">
        <Segmented<Tab>
          value={tab}
          onChange={setTab}
          testIdPrefix="receipts-tab"
          options={(['receipts', 'products', 'stores'] as const).map((value) => ({
            value,
            label: t(`receipts.tabs.${value}`),
          }))}
        />
      </div>

      {tab === 'receipts' && <ReceiptsTab />}
      {tab === 'products' && <ProductsTab />}
      {tab === 'stores' && (
        <RetailConnectionsPanel onConnect={() => setConnectOpen(true)} onReconnect={(c) => setReconnect(c)} />
      )}

      <RetailConnectDialog
        open={connectOpen || reconnect !== null}
        reconnect={reconnect}
        onClose={() => {
          setConnectOpen(false)
          setReconnect(null)
        }}
      />
    </div>
  )
}

function ReceiptsTab() {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<MatchFilter>('all')
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [openId, setOpenId] = useState<string | null>(null)

  const query = search.trim()
  const { data, isLoading } = useQuery({
    queryKey: ['receipts', 'list', filter, query, limit],
    queryFn: () =>
      receiptsApi.list({
        limit,
        ...(filter === 'all' ? {} : { matched: filter === 'matched' }),
        ...(query ? { q: query } : {}),
      }),
    placeholderData: keepPreviousData,
  })

  const autoMatch = useMutation({
    mutationFn: receiptsApi.autoMatch,
    onSuccess: ({ matched }) => {
      toast.success(t('receipts.list.autoMatched', { matched }))
      void queryClient.invalidateQueries({ queryKey: ['receipts'] })
    },
    onError: (error) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic')),
  })

  const items = data?.items ?? []
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Segmented<MatchFilter>
          value={filter}
          onChange={(value) => {
            setFilter(value)
            setLimit(PAGE)
          }}
          testIdPrefix="receipt-filter"
          options={(['all', 'unmatched', 'matched'] as const).map((value) => ({
            value,
            label: t(`receipts.list.filter.${value}`),
          }))}
        />
        <div className="flex items-center gap-2 w-full sm:w-auto">
          {canWrite && (
            <Button size="sm" variant="outline" onClick={() => autoMatch.mutate()} disabled={autoMatch.isPending}>
              <Link2 className="h-3.5 w-3.5 mr-1.5" />
              {t('receipts.list.autoMatch')}
            </Button>
          )}
          <Input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setLimit(PAGE)
            }}
            placeholder={t('receipts.list.searchPlaceholder')}
            className="h-9 w-full sm:w-64"
            data-testid="receipt-search"
          />
        </div>
      </div>

      <SectionCard>
        {isLoading ? (
          <div className="p-5 space-y-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="px-5 py-14 text-center" data-testid="receipts-empty">
            <ShoppingBasket className="h-8 w-8 mx-auto text-muted-foreground/50" />
            <p className="mt-3 text-sm text-muted-foreground max-w-sm mx-auto">
              {query || filter !== 'all' ? t('receipts.list.noneFound') : t('receipts.list.empty')}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border">
                  <th className={`${TH} pl-4 sm:pl-5 text-left w-32`}>{t('receipts.list.column.date')}</th>
                  <th className={`${TH} text-left`}>{t('receipts.list.column.store')}</th>
                  <th className={`${TH} px-4 text-center w-24 hidden sm:table-cell`}>{t('receipts.list.column.items')}</th>
                  <th className={`${TH} pl-4 pr-6 text-left hidden md:table-cell`}>{t('receipts.list.column.payment')}</th>
                  <th className={`${TH} pr-4 sm:pr-5 text-right w-28`}>{t('receipts.list.column.total')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((receipt) => (
                  <tr
                    key={receipt.id}
                    onClick={() => setOpenId(receipt.id)}
                    className="border-b border-border last:border-0 cursor-pointer hover:bg-muted/50"
                    data-testid="receipt-row"
                  >
                    <td className="py-3 pl-4 sm:pl-5 text-sm tabular-nums">{formatDate(receipt.date, locale)}</td>
                    <td className="py-3">
                      <div className="text-sm font-medium truncate">{receipt.store_name ?? sourceLabel(receipt.source)}</div>
                      <div className="text-xs text-muted-foreground">{sourceLabel(receipt.source)}</div>
                    </td>
                    <td className="py-3 px-4 text-center text-xs text-muted-foreground tabular-nums hidden sm:table-cell">
                      {receipt.item_count}
                    </td>
                    <td className="py-3 pl-4 pr-6 hidden md:table-cell text-xs">
                      {receipt.transaction ? (
                        <span
                          className="block max-w-xs lg:max-w-sm truncate text-muted-foreground"
                          title={receipt.transaction.description}
                        >
                          {receipt.transaction.description}
                        </span>
                      ) : (
                        <span className="text-amber-600 dark:text-amber-400">{t('receipts.list.unpaired')}</span>
                      )}
                    </td>
                    <td className="py-3 pr-4 sm:pr-5 text-right text-sm font-medium tabular-nums">
                      {mask(formatCurrency(Number(receipt.total), receipt.currency, locale))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>
      {data && data.total > items.length && (
        <div className="mt-3 flex justify-center">
          <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + PAGE)}>
            {t('receipts.list.loadMore', { shown: items.length, total: data.total })}
          </Button>
        </div>
      )}

      <ReceiptDialog receiptId={openId} onClose={() => setOpenId(null)} />
    </>
  )
}

function ProductsTab() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const [filter, setFilter] = useState<ProductFilter>('all')
  const [search, setSearch] = useState('')

  const { data: list, isLoading } = useQuery({
    queryKey: ['shopping-products', filter],
    queryFn: () => shoppingProducts.list(filter === 'tracked' ? { tracked: true } : undefined),
  })

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    const rows = (list ?? []).filter((p) => !q || p.name.toLowerCase().includes(q))
    // Tracked consumables first, soonest to run out on top; then the most bought.
    return rows.sort((a, b) => {
      const da = a.consumption?.days_left ?? Number.POSITIVE_INFINITY
      const db = b.consumption?.days_left ?? Number.POSITIVE_INFINITY
      if (da !== db) return da - db
      return b.purchase_count - a.purchase_count
    })
  }, [list, search])

  const money = (value: string | null) => (value === null ? '—' : mask(formatCurrency(Number(value), 'PLN', locale)))

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Segmented<ProductFilter>
          value={filter}
          onChange={setFilter}
          testIdPrefix="shopping-product-filter"
          options={(['all', 'tracked'] as const).map((value) => ({
            value,
            label: t(`receipts.products.filter.${value}`),
          }))}
        />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('receipts.products.searchPlaceholder')}
          className="h-9 w-full sm:w-64"
          data-testid="shopping-product-search"
        />
      </div>

      <SectionCard>
        {isLoading ? (
          <div className="p-5 space-y-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <div className="px-5 py-14 text-center" data-testid="shopping-products-empty">
            <ShoppingBasket className="h-8 w-8 mx-auto text-muted-foreground/50" />
            <p className="mt-3 text-sm text-muted-foreground max-w-sm mx-auto">
              {filter === 'tracked' ? t('receipts.products.emptyTracked') : t('receipts.products.empty')}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border">
                  <th className={`${TH} pl-4 sm:pl-5 text-left`}>{t('receipts.products.column.name')}</th>
                  <th className={`${TH} text-right w-24 hidden sm:table-cell`}>{t('receipts.products.column.purchases')}</th>
                  <th className={`${TH} text-right w-28`}>{t('receipts.products.column.lastPrice')}</th>
                  <th className={`${TH} text-right w-24 hidden md:table-cell`}>{t('receipts.products.column.change')}</th>
                  <th className={`${TH} text-right w-32 hidden lg:table-cell`}>{t('receipts.products.column.lastBought')}</th>
                  <th className={`${TH} pr-4 sm:pr-5 text-right w-36`}>{t('receipts.products.column.stock')}</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((product) => {
                  const level = stockLevel(product.consumption)
                  const change = product.price_change_pct
                  return (
                    <tr
                      key={product.id}
                      onClick={() => navigate(`/receipts/products/${product.id}`)}
                      className="border-b border-border last:border-0 cursor-pointer hover:bg-muted/50"
                      data-testid="shopping-product-row"
                    >
                      <td className="py-3 pl-4 sm:pl-5">
                        <div className="text-sm font-medium truncate">{product.name}</div>
                        <div className="text-xs text-muted-foreground">
                          {product.sources.map(sourceLabel).join(', ')}
                          {product.purchase_count > 0 &&
                            ` · ${formatQuantity(product.total_quantity, locale)} ${product.unit ?? ''}`}
                        </div>
                      </td>
                      <td className="py-3 text-right text-xs text-muted-foreground tabular-nums hidden sm:table-cell">
                        {product.purchase_count}
                      </td>
                      <td className="py-3 text-right text-sm tabular-nums">
                        {money(product.last_unit_price)}
                        {product.last_paid_unit_price !== null &&
                          product.last_paid_unit_price !== product.last_unit_price && (
                            <div className="text-[11px] text-emerald-600 dark:text-emerald-400" data-testid="last-paid-price">
                              {t('receipts.products.paidPerUnit', { price: money(product.last_paid_unit_price) })}
                            </div>
                          )}
                      </td>
                      <td className="py-3 text-right text-xs tabular-nums hidden md:table-cell">
                        {change === null || change === 0 ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <span
                            className={cn(
                              'inline-flex items-center gap-1',
                              change > 0 ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400',
                            )}
                          >
                            {change > 0 ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                            {change > 0 ? '+' : ''}
                            {change.toLocaleString(locale)}%
                          </span>
                        )}
                      </td>
                      <td className="py-3 text-right text-xs text-muted-foreground tabular-nums hidden lg:table-cell">
                        {product.last_purchased_at ? formatDate(product.last_purchased_at, locale) : '—'}
                      </td>
                      <td className={cn('py-3 pr-4 sm:pr-5 text-right text-xs', STOCK_TONE[level])}>
                        {!product.track_consumption
                          ? '—'
                          : level === 'unknown'
                            ? t('receipts.products.stock.unknown')
                            : level === 'out'
                              ? t('receipts.products.stock.out')
                              : t('receipts.products.stock.daysLeft', { days: product.consumption!.days_left })}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>
    </>
  )
}
