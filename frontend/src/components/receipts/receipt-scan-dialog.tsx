import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Loader2, Plus, RotateCcw, Trash2, X } from 'lucide-react'
import { toast } from 'sonner'

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { IconAction } from '@/components/invoice-ui'
import { receiptScans } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { apiErrorMessage, scanErrorText, scanLinesSum } from '@/lib/receipt-utils'
import { cn } from '@/lib/utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import type { ReceiptScan, ReceiptScanDraft, ReceiptScanLine } from '@/types'

const EMPTY_DRAFT: ReceiptScanDraft = {
  store_name: null,
  store_address: null,
  tax_id: null,
  date: null,
  time: null,
  currency: 'PLN',
  total: null,
  items: [],
}

const EMPTY_LINE: ReceiptScanLine = { name: '', quantity: '1', unit: null, unit_price: '', total: '', discount: '0' }

function money2(value: number): string {
  return (Math.round(value * 100) / 100).toFixed(2)
}

/** The stored photo of a scan, fetched with the session's credentials. */
export function ScanPhoto({ scanId, className }: { scanId: string; className?: string }) {
  const { t } = useTranslation()
  const { data: blob, isLoading, isError } = useQuery({
    queryKey: ['receipt-scans', 'image', scanId],
    queryFn: () => receiptScans.image(scanId),
    staleTime: Infinity,
  })
  const url = useMemo(
    () => (blob && typeof URL.createObjectURL === 'function' ? URL.createObjectURL(blob) : null),
    [blob],
  )
  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url)
  }, [url])

  if (isLoading) return <Skeleton className={cn('h-64 w-full', className)} />
  if (isError || !url) {
    return <p className={cn('text-xs text-muted-foreground', className)}>{t('receipts.scan.photoMissing')}</p>
  }
  return (
    <a href={url} target="_blank" rel="noreferrer" className={cn('block', className)} title={t('receipts.scan.openPhoto')}>
      <img src={url} alt={t('receipts.scan.photoAlt')} className="w-full rounded-md border border-border" data-testid="scan-photo" />
    </a>
  )
}

/**
 * Review what the model read off a receipt photo, fix it, and accept it.
 *
 * The photo sits next to the draft so every price can be checked against
 * the paper. The line sum is compared with the total as you type: a
 * mismatch is the quickest sign of a misread digit.
 */
export function ReceiptScanDialog({ scanId, onClose }: { scanId: string | null; onClose: () => void }) {
  const queryClient = useQueryClient()

  const { data: scan, isLoading } = useQuery({
    queryKey: ['receipt-scans', 'detail', scanId],
    queryFn: () => receiptScans.get(scanId!),
    enabled: scanId !== null,
    refetchInterval: (query) => (query.state.data?.status === 'processing' ? 3000 : false),
  })

  return (
    <Dialog open={scanId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-5xl max-h-[92vh] overflow-y-auto" data-testid="receipt-scan-dialog">
        {isLoading || !scan ? (
          <div className="space-y-3">
            <Skeleton className="h-6 w-1/2" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : (
          <ScanReview
            // Re-seed the form whenever the model produced a new draft.
            key={`${scan.id}:${scan.status}:${scan.error ?? ''}`}
            scan={scan}
            onDone={() => {
              void queryClient.invalidateQueries({ queryKey: ['receipt-scans'] })
              onClose()
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function ScanReview({ scan, onDone }: { scan: ReceiptScan; onDone: () => void }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<ReceiptScanDraft>(() => ({
    ...EMPTY_DRAFT,
    ...(scan.draft ?? {}),
    items: (scan.draft?.items ?? []).map((line) => ({ ...line })),
  }))
  const accepted = scan.status === 'accepted'
  const processing = scan.status === 'processing'

  const sum = useMemo(() => scanLinesSum(draft.items), [draft.items])
  const total = Number(draft.total)
  const hasTotal = draft.total !== null && draft.total !== '' && total > 0
  const mismatch = hasTotal && draft.items.length > 0 && Math.round(sum * 100) !== Math.round(total * 100)
  const missing = !draft.date
    ? t('receipts.scan.needDate')
    : !hasTotal
      ? t('receipts.scan.needTotal')
      : draft.items.length === 0 || draft.items.some((line) => !line.name.trim() || line.total === '')
        ? t('receipts.scan.needLines')
        : null

  const onError = (error: unknown) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic'))
  const refreshAll = () => {
    for (const key of ['receipts', 'receipt-scans', 'shopping-products']) {
      void queryClient.invalidateQueries({ queryKey: [key] })
    }
  }

  const accept = useMutation({
    mutationFn: () =>
      receiptScans.accept(scan.id, {
        ...draft,
        store_name: draft.store_name?.trim() || null,
        store_address: draft.store_address?.trim() || null,
        time: draft.time || null,
        currency: (draft.currency || 'PLN').toUpperCase(),
        items: draft.items.map((line) => ({
          ...line,
          name: line.name.trim(),
          unit: line.unit?.trim() || null,
          unit_price: line.unit_price === '' ? line.total : line.unit_price,
          discount: line.discount === '' ? '0' : line.discount,
        })),
      }),
    onSuccess: (receipt) => {
      toast.success(receipt.transaction_id ? t('receipts.scan.savedAndPaired') : t('receipts.scan.saved'))
      refreshAll()
      onDone()
    },
    onError,
  })
  const retry = useMutation({
    mutationFn: () => receiptScans.retry(scan.id),
    onSuccess: (next) => {
      queryClient.setQueryData(['receipt-scans', 'detail', scan.id], next)
      void queryClient.invalidateQueries({ queryKey: ['receipt-scans'] })
      if (next.status === 'failed') toast.error(scanErrorText(next.error))
    },
    onError,
  })
  const discard = useMutation({
    mutationFn: () => receiptScans.remove(scan.id),
    onSuccess: () => {
      refreshAll()
      onDone()
    },
    onError,
  })

  const setField = <K extends keyof ReceiptScanDraft>(key: K, value: ReceiptScanDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const setLine = (index: number, patch: Partial<ReceiptScanLine>) =>
    setDraft((current) => {
      const items = current.items.map((line, i) => {
        if (i !== index) return line
        const next = { ...line, ...patch }
        // Quantity or unit price changed: the line value follows.
        if (('quantity' in patch || 'unit_price' in patch) && next.quantity !== '' && next.unit_price !== '') {
          next.total = money2(Number(next.quantity) * Number(next.unit_price))
        }
        return next
      })
      return { ...current, items }
    })

  const removeLine = (index: number) =>
    setDraft((current) => ({ ...current, items: current.items.filter((_, i) => i !== index) }))
  const addLine = () => setDraft((current) => ({ ...current, items: [...current.items, { ...EMPTY_LINE }] }))

  const money = (value: number) => formatCurrency(value, draft.currency || 'PLN', locale)
  const busy = accept.isPending || retry.isPending || discard.isPending

  return (
    <>
      <DialogHeader>
        <DialogTitle>{accepted ? t('receipts.scan.editTitle') : t('receipts.scan.reviewTitle')}</DialogTitle>
        <DialogDescription>{t('receipts.scan.reviewHint')}</DialogDescription>
      </DialogHeader>

      {scan.status === 'failed' && (
        <div
          className="flex items-start gap-2 rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200"
          data-testid="scan-error"
        >
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>
            {scanErrorText(scan.error)} {t('receipts.scan.manualHint')}
          </span>
        </div>
      )}
      {processing && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t('receipts.scan.reading')}
        </div>
      )}

      <div className="grid gap-5 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="md:sticky md:top-0 md:self-start md:max-h-[75vh] md:overflow-y-auto">
          <ScanPhoto scanId={scan.id} />
        </div>

        <div className="space-y-4 min-w-0">
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2 sm:col-span-1 space-y-1">
              <Label htmlFor="scan-store">{t('receipts.scan.store')}</Label>
              <Input
                id="scan-store"
                value={draft.store_name ?? ''}
                onChange={(e) => setField('store_name', e.target.value)}
                data-testid="scan-store"
              />
            </div>
            <div className="col-span-2 sm:col-span-1 space-y-1">
              <Label htmlFor="scan-address">{t('receipts.scan.address')}</Label>
              <Input
                id="scan-address"
                value={draft.store_address ?? ''}
                onChange={(e) => setField('store_address', e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="scan-date">{t('receipts.scan.date')}</Label>
              <Input
                id="scan-date"
                type="date"
                value={draft.date ?? ''}
                onChange={(e) => setField('date', e.target.value || null)}
                data-testid="scan-date"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="scan-time">{t('receipts.scan.time')}</Label>
              <Input
                id="scan-time"
                type="time"
                value={draft.time ?? ''}
                onChange={(e) => setField('time', e.target.value || null)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="scan-total">{t('receipts.scan.total')}</Label>
              <Input
                id="scan-total"
                inputMode="decimal"
                value={draft.total ?? ''}
                onChange={(e) => setField('total', e.target.value.replace(',', '.') || null)}
                data-testid="scan-total"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="scan-currency">{t('receipts.scan.currency')}</Label>
              <Input
                id="scan-currency"
                maxLength={3}
                value={draft.currency}
                onChange={(e) => setField('currency', e.target.value.toUpperCase())}
              />
            </div>
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-sm font-medium">{t('receipts.scan.lines', { count: draft.items.length })}</span>
              <Button type="button" size="sm" variant="ghost" onClick={addLine}>
                <Plus className="h-3.5 w-3.5 mr-1" />
                {t('receipts.scan.addLine')}
              </Button>
            </div>
            <div className="hidden sm:grid grid-cols-[minmax(0,1fr)_4.5rem_5.5rem_5.5rem_5rem_2rem] gap-1.5 px-1 pb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
              <span>{t('receipts.detail.column.product')}</span>
              <span className="text-right">{t('receipts.detail.column.quantity')}</span>
              <span className="text-right">{t('receipts.detail.column.unitPrice')}</span>
              <span className="text-right">{t('receipts.scan.lineValue')}</span>
              <span className="text-right">{t('receipts.scan.discount')}</span>
              <span />
            </div>
            <ul className="space-y-2 sm:space-y-1">
              {draft.items.map((line, index) => (
                <li
                  key={index}
                  className="grid grid-cols-4 sm:grid-cols-[minmax(0,1fr)_4.5rem_5.5rem_5.5rem_5rem_2rem] gap-1.5 items-center rounded-md border border-border p-2 sm:border-0 sm:p-0"
                  data-testid="scan-line"
                >
                  <Input
                    className="col-span-3 sm:col-span-1 h-8 text-sm"
                    value={line.name}
                    aria-label={t('receipts.detail.column.product')}
                    onChange={(e) => setLine(index, { name: e.target.value })}
                  />
                  <div className="col-start-4 row-start-1 flex justify-end sm:col-start-auto sm:row-start-auto sm:order-last">
                    <IconAction onClick={() => removeLine(index)} label={t('receipts.scan.removeLine')} destructive>
                      <X className="h-3.5 w-3.5" />
                    </IconAction>
                  </div>
                  <Input
                    className="h-8 text-sm text-right tabular-nums"
                    inputMode="decimal"
                    value={line.quantity}
                    aria-label={t('receipts.detail.column.quantity')}
                    onChange={(e) => setLine(index, { quantity: e.target.value.replace(',', '.') })}
                  />
                  <Input
                    className="h-8 text-sm text-right tabular-nums"
                    inputMode="decimal"
                    value={line.unit_price}
                    aria-label={t('receipts.detail.column.unitPrice')}
                    onChange={(e) => setLine(index, { unit_price: e.target.value.replace(',', '.') })}
                  />
                  <Input
                    className="h-8 text-sm text-right tabular-nums"
                    inputMode="decimal"
                    value={line.total}
                    aria-label={t('receipts.scan.lineValue')}
                    onChange={(e) => setLine(index, { total: e.target.value.replace(',', '.') })}
                  />
                  <Input
                    className="h-8 text-sm text-right tabular-nums"
                    inputMode="decimal"
                    value={line.discount}
                    aria-label={t('receipts.scan.discount')}
                    onChange={(e) => setLine(index, { discount: e.target.value.replace(',', '.').replace('-', '') })}
                  />
                </li>
              ))}
            </ul>
          </div>

          <div
            className={cn(
              'flex flex-wrap items-center justify-between gap-2 rounded-md px-3 py-2 text-sm',
              mismatch
                ? 'border border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200'
                : 'bg-muted/50',
            )}
            data-testid="scan-sum"
          >
            <span>
              {t('receipts.scan.linesSum')}: <span className="font-medium tabular-nums">{money(sum)}</span>
            </span>
            {mismatch ? (
              <span className="flex items-center gap-1.5" data-testid="scan-mismatch">
                <AlertTriangle className="h-3.5 w-3.5" />
                {t('receipts.scan.mismatch', { total: money(total), difference: money(sum - total) })}
              </span>
            ) : (
              hasTotal && (
                <span className="flex items-center gap-1.5 text-emerald-700 dark:text-emerald-400">
                  <Check className="h-3.5 w-3.5" />
                  {t('receipts.scan.sumMatches')}
                </span>
              )
            )}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <div className="flex gap-2">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-destructive hover:text-destructive"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(accepted ? t('receipts.scan.confirmDeleteReceipt') : t('receipts.scan.confirmDiscard'))) {
                    discard.mutate()
                  }
                }}
                data-testid="scan-discard"
              >
                <Trash2 className="h-3.5 w-3.5 mr-1.5" />
                {accepted ? t('receipts.scan.deleteReceipt') : t('receipts.scan.discard')}
              </Button>
              {!accepted && (
                <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => retry.mutate()} data-testid="scan-retry">
                  {retry.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                  )}
                  {t('receipts.scan.retry')}
                </Button>
              )}
            </div>
            <div className="flex items-center gap-3">
              {missing && <span className="text-xs text-muted-foreground">{missing}</span>}
              <Button
                type="button"
                size="sm"
                disabled={busy || processing || missing !== null}
                onClick={() => accept.mutate()}
                data-testid="scan-accept"
              >
                {accept.isPending && <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />}
                {accepted ? t('receipts.scan.saveChanges') : t('receipts.scan.accept')}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}
