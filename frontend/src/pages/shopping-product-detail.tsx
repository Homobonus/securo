import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Combine, Pencil } from 'lucide-react'
import { toast } from 'sonner'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { PageHeader } from '@/components/page-header'
import { SectionCard, SectionHeader, TH } from '@/components/invoice-ui'
import { shoppingProducts } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { formatDate } from '@/lib/date-range-format'
import { apiErrorMessage, formatQuantity, sourceLabel, stockLevel } from '@/lib/receipt-utils'
import { cn } from '@/lib/utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useWorkspace } from '@/contexts/workspace-context'
import type { ShoppingProductDetail } from '@/types'

/**
 * One thing the person buys: what it cost each time, and, for
 * consumables they chose to track, how long one lasts and when the
 * current one runs out.
 */
export default function ShoppingProductDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const navigate = useNavigate()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [merging, setMerging] = useState(false)

  const { data: product, isLoading } = useQuery({
    queryKey: ['shopping-products', 'detail', id],
    queryFn: () => shoppingProducts.get(id!),
    enabled: Boolean(id),
  })

  const onSaved = (updated: ShoppingProductDetail) => {
    queryClient.setQueryData(['shopping-products', 'detail', id], updated)
    void queryClient.invalidateQueries({ queryKey: ['shopping-products'] })
  }
  const onError = (error: unknown) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic'))
  const track = useMutation({
    mutationFn: (value: boolean) => shoppingProducts.update(id!, { track_consumption: value }),
    onSuccess: onSaved,
    onError,
  })

  const currency = 'PLN'
  const money = (value: string | number | null | undefined) =>
    value === null || value === undefined ? '—' : mask(formatCurrency(Number(value), currency, locale))

  const chartData = useMemo(
    () =>
      (product?.price_history ?? []).map((p) => ({
        date: p.date,
        price: Number(p.unit_price),
        paid: Number(p.effective_unit_price),
      })),
    [product],
  )

  const back = (
    <button
      onClick={() => navigate('/receipts?tab=products')}
      className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors mb-3"
      data-testid="shopping-product-back"
    >
      <ArrowLeft className="h-3.5 w-3.5" />
      {t('receipts.product.back')}
    </button>
  )

  if (isLoading || !product) {
    return (
      <div>
        {back}
        <Skeleton className="h-8 w-64 mb-4" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  const consumption = product.consumption
  const level = stockLevel(consumption)
  const unit = product.unit ?? t('receipts.product.unitFallback')

  return (
    <div>
      {back}
      <PageHeader
        section={t('receipts.title')}
        title={product.name}
        action={
          canWrite ? (
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setMerging(true)} data-testid="shopping-product-merge">
                <Combine className="h-4 w-4 mr-1.5" />
                {t('receipts.product.merge')}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                <Pencil className="h-4 w-4 mr-1.5" />
                {t('common.edit')}
              </Button>
            </div>
          ) : undefined
        }
      />

      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4 mb-4">
        <Stat
          label={t('receipts.product.lastPrice')}
          value={money(product.last_unit_price)}
          hint={
            product.last_paid_unit_price !== null && product.last_paid_unit_price !== product.last_unit_price
              ? t('receipts.products.paidPerUnit', { price: money(product.last_paid_unit_price) })
              : undefined
          }
        />
        <Stat
          label={t('receipts.product.priceRange')}
          value={`${money(product.min_unit_price)} – ${money(product.max_unit_price)}`}
        />
        <Stat label={t('receipts.product.totalSpent')} value={money(product.total_spent)} />
        <Stat
          label={t('receipts.product.purchases')}
          value={`${product.purchase_count} · ${formatQuantity(product.total_quantity, locale)} ${product.unit ?? ''}`}
        />
      </div>

      <SectionCard className="mb-4">
        <SectionHeader
          title={t('receipts.product.consumption')}
          description={t('receipts.product.consumptionHint')}
          action={
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              {t('receipts.product.track')}
              <Switch
                id="shopping-product-track"
                checked={product.track_consumption}
                onCheckedChange={(value) => track.mutate(value)}
                disabled={!canWrite || track.isPending}
              />
            </label>
          }
        />
        {!product.track_consumption ? (
          <p className="px-4 sm:px-5 py-4 text-sm text-muted-foreground">{t('receipts.product.trackOff')}</p>
        ) : !consumption || consumption.days_per_unit === null ? (
          <p className="px-4 sm:px-5 py-4 text-sm text-muted-foreground">{t('receipts.product.notEnoughData')}</p>
        ) : (
          <div className="grid gap-3 grid-cols-2 lg:grid-cols-4 p-4 sm:p-5" data-testid="shopping-product-consumption">
            <Stat
              label={t('receipts.product.lasts')}
              value={t('receipts.product.daysPerUnit', { days: consumption.days_per_unit.toLocaleString(locale), unit })}
            />
            <Stat
              label={t('receipts.product.interval')}
              value={t('receipts.product.everyDays', { days: consumption.avg_days_between?.toLocaleString(locale) })}
            />
            <Stat label={t('receipts.product.monthlyCost')} value={money(consumption.monthly_cost)} />
            <Stat
              label={t('receipts.product.runsOut')}
              value={consumption.estimated_depletion ? formatDate(consumption.estimated_depletion, locale) : '—'}
              hint={
                level === 'out'
                  ? t('receipts.products.stock.out')
                  : t('receipts.products.stock.daysLeft', { days: consumption.days_left })
              }
              tone={level === 'out' ? 'text-rose-600 dark:text-rose-400' : level === 'low' ? 'text-amber-600 dark:text-amber-400' : undefined}
            />
          </div>
        )}
      </SectionCard>

      {chartData.length > 1 && (
        <SectionCard className="mb-4">
          <SectionHeader title={t('receipts.product.priceHistory')} />
          <div className="h-64 p-4" data-testid="shopping-product-chart">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis
                  dataKey="date"
                  tickFormatter={(value: string) => formatDate(value, locale, { compact: true })}
                  tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  width={56}
                  tickFormatter={(value: number) => mask(formatCurrency(value, currency, locale))}
                  tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                  axisLine={false}
                  tickLine={false}
                  domain={['auto', 'auto']}
                />
                <Tooltip
                  contentStyle={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
                  labelFormatter={(value) => formatDate(String(value), locale)}
                  formatter={(value, name) => [
                    mask(formatCurrency(Number(value), currency, locale)),
                    name === 'price' ? t('receipts.product.shelfPrice') : t('receipts.product.paidPrice'),
                  ]}
                />
                <Line type="stepAfter" dataKey="price" stroke="var(--primary)" strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
                <Line type="stepAfter" dataKey="paid" stroke="var(--muted-foreground)" strokeDasharray="4 3" dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </SectionCard>
      )}

      <SectionCard className="mb-4">
        <SectionHeader title={t('receipts.product.history')} />
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className={`${TH} pl-4 sm:pl-5 text-left w-32`}>{t('receipts.list.column.date')}</th>
                <th className={`${TH} text-left`}>{t('receipts.list.column.store')}</th>
                <th className={`${TH} text-right w-24`}>{t('receipts.detail.column.quantity')}</th>
                <th className={`${TH} text-right w-28`}>{t('receipts.detail.column.unitPrice')}</th>
                <th className={`${TH} pr-4 sm:pr-5 text-right w-28`}>{t('receipts.product.paidPrice')}</th>
              </tr>
            </thead>
            <tbody>
              {[...product.price_history].reverse().map((p, index) => (
                <tr key={`${p.receipt_id}-${index}`} className="border-b border-border last:border-0" data-testid="price-point">
                  <td className="py-2.5 pl-4 sm:pl-5 text-sm tabular-nums">{formatDate(p.date, locale)}</td>
                  <td className="py-2.5 text-xs text-muted-foreground">{p.store_name ?? sourceLabel(p.source)}</td>
                  <td className="py-2.5 text-right text-xs tabular-nums">
                    {formatQuantity(p.quantity, locale)} {p.unit ?? ''}
                  </td>
                  <td className="py-2.5 text-right text-sm tabular-nums">{money(p.unit_price)}</td>
                  <td className="py-2.5 pr-4 sm:pr-5 text-right text-sm tabular-nums">{money(p.effective_unit_price)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SectionCard>

      <SectionCard>
        <SectionHeader title={t('receipts.product.aliases')} description={t('receipts.product.aliasesHint')} />
        <ul className="px-4 sm:px-5 py-3 flex flex-wrap gap-2">
          {product.aliases.map((alias) => (
            <li key={alias.id} className="text-xs rounded-full border border-border px-2.5 py-1">
              {alias.name} <span className="text-muted-foreground">· {sourceLabel(alias.source)}</span>
            </li>
          ))}
        </ul>
      </SectionCard>

      {editing && <EditDialog product={product} onClose={() => setEditing(false)} onSaved={onSaved} />}
      {merging && (
        <MergeDialog
          product={product}
          onClose={() => setMerging(false)}
          onMerged={(updated) => {
            onSaved(updated)
            setMerging(false)
          }}
        />
      )}
    </div>
  )
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className="bg-card rounded-xl border border-border shadow-sm px-4 py-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('text-base font-semibold tabular-nums mt-0.5', tone)}>{value}</div>
      {hint && <div className={cn('text-xs mt-0.5', tone ?? 'text-muted-foreground')}>{hint}</div>}
    </div>
  )
}

function EditDialog({
  product,
  onClose,
  onSaved,
}: {
  product: ShoppingProductDetail
  onClose: () => void
  onSaved: (product: ShoppingProductDetail) => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState(product.name)
  const [notes, setNotes] = useState(product.notes ?? '')
  const save = useMutation({
    mutationFn: () => shoppingProducts.update(product.id, { name: name.trim(), notes: notes.trim() || null }),
    onSuccess: (updated) => {
      onSaved(updated)
      onClose()
    },
    onError: (error) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic')),
  })
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('receipts.product.editTitle')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="shopping-product-name">{t('common.name')}</Label>
            <Input id="shopping-product-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="shopping-product-notes">{t('receipts.product.notes')}</Label>
            <Input id="shopping-product-notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => save.mutate()} disabled={!name.trim() || save.isPending}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function MergeDialog({
  product,
  onClose,
  onMerged,
}: {
  product: ShoppingProductDetail
  onClose: () => void
  onMerged: (product: ShoppingProductDetail) => void
}) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const { data: list } = useQuery({ queryKey: ['shopping-products', 'all'], queryFn: () => shoppingProducts.list() })

  const options = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (list ?? [])
      .filter((p) => p.id !== product.id && (!q || p.name.toLowerCase().includes(q)))
      .slice(0, 50)
  }, [list, product.id, search])

  const merge = useMutation({
    mutationFn: () => shoppingProducts.merge(product.id, selected),
    onSuccess: (updated) => {
      toast.success(t('receipts.product.merged'))
      onMerged(updated)
    },
    onError: (error) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic')),
  })

  const toggle = (id: string) =>
    setSelected((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id]))

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg" data-testid="shopping-product-merge-dialog">
        <DialogHeader>
          <DialogTitle>{t('receipts.product.mergeTitle', { name: product.name })}</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">{t('receipts.product.mergeHint')}</p>
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('receipts.products.searchPlaceholder')}
          className="h-9"
        />
        <ul className="max-h-72 overflow-y-auto divide-y divide-border rounded-lg border border-border">
          {options.map((p) => (
            <li key={p.id}>
              <label className="flex items-center gap-3 px-3 py-2 text-sm cursor-pointer hover:bg-muted/50">
                <input
                  type="checkbox"
                  checked={selected.includes(p.id)}
                  onChange={() => toggle(p.id)}
                  data-testid="merge-option"
                />
                <span className="flex-1 truncate">{p.name}</span>
                <span className="text-xs text-muted-foreground">
                  {p.sources.map(sourceLabel).join(', ')} · {p.purchase_count}×
                </span>
              </label>
            </li>
          ))}
        </ul>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => merge.mutate()} disabled={selected.length === 0 || merge.isPending} data-testid="merge-submit">
            {t('receipts.product.mergeSubmit', { selected: selected.length })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
