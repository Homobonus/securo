import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link2, Link2Off, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { TH } from '@/components/invoice-ui'
import { receipts as receiptsApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { formatDate } from '@/lib/date-range-format'
import { apiErrorMessage, formatQuantity, sourceLabel } from '@/lib/receipt-utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useWorkspace } from '@/contexts/workspace-context'

/** One receipt: its lines, and which bank transaction paid for it. */
export function ReceiptDialog({ receiptId, onClose }: { receiptId: string | null; onClose: () => void }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()

  const { data: receipt, isLoading } = useQuery({
    queryKey: ['receipts', 'detail', receiptId],
    queryFn: () => receiptsApi.get(receiptId!),
    enabled: receiptId !== null,
  })
  const unpaired = receipt !== undefined && receipt.transaction === null
  const { data: candidates, isLoading: loadingCandidates } = useQuery({
    queryKey: ['receipts', 'candidates', receiptId],
    queryFn: () => receiptsApi.candidates(receiptId!),
    enabled: receiptId !== null && unpaired,
  })

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['receipts'] })
  const onError = (error: unknown) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic'))
  const link = useMutation({
    mutationFn: (transactionId: string) => receiptsApi.link(receiptId!, transactionId),
    onSuccess: () => {
      toast.success(t('receipts.detail.linked'))
      refresh()
    },
    onError,
  })
  const unlink = useMutation({
    mutationFn: () => receiptsApi.unlink(receiptId!),
    onSuccess: refresh,
    onError,
  })

  const money = (value: string | number, currency = receipt?.currency ?? 'PLN') =>
    mask(formatCurrency(Number(value), currency, locale))

  return (
    <Dialog open={receiptId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto" data-testid="receipt-dialog">
        {isLoading || !receipt ? (
          <div className="space-y-3">
            <Skeleton className="h-6 w-1/2" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>
                {receipt.store_name ?? sourceLabel(receipt.source)} · {money(receipt.total)}
              </DialogTitle>
              <DialogDescription>
                {formatDate(receipt.date, locale)}
                {receipt.purchased_at &&
                  ` ${new Date(receipt.purchased_at).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}`}
                {receipt.store_address && ` · ${receipt.store_address}`}
              </DialogDescription>
            </DialogHeader>

            <div className="rounded-lg border border-border p-3" data-testid="receipt-pairing">
              <div className="text-xs font-medium text-muted-foreground mb-2">{t('receipts.detail.payment')}</div>
              {receipt.transaction ? (
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm truncate">{receipt.transaction.description}</div>
                    <div className="text-xs text-muted-foreground">
                      {formatDate(receipt.transaction.date, locale)} ·{' '}
                      {t(`receipts.detail.method.${receipt.match_method ?? 'manual'}`)}
                    </div>
                  </div>
                  {canWrite && (
                    <Button size="sm" variant="outline" onClick={() => unlink.mutate()} disabled={unlink.isPending}>
                      <Link2Off className="h-3.5 w-3.5 mr-1.5" />
                      {t('receipts.detail.unlink')}
                    </Button>
                  )}
                </div>
              ) : loadingCandidates ? (
                <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              ) : candidates && candidates.length > 0 ? (
                <ul className="space-y-1.5">
                  {candidates.map((tx) => (
                    <li key={tx.id} className="flex items-center justify-between gap-3" data-testid="receipt-candidate">
                      <div className="min-w-0">
                        <div className="text-sm truncate">{tx.description}</div>
                        <div className="text-xs text-muted-foreground">
                          {formatDate(tx.date, locale)} · {money(tx.amount, tx.currency)}
                        </div>
                      </div>
                      {canWrite && (
                        <Button size="sm" variant="outline" onClick={() => link.mutate(tx.id)} disabled={link.isPending}>
                          <Link2 className="h-3.5 w-3.5 mr-1.5" />
                          {t('receipts.detail.link')}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">{t('receipts.detail.noCandidates')}</p>
              )}
            </div>

            {receipt.items.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t('receipts.detail.noItems')}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-border">
                      <th className={`${TH} text-left`}>{t('receipts.detail.column.product')}</th>
                      <th className={`${TH} text-right w-20`}>{t('receipts.detail.column.quantity')}</th>
                      <th className={`${TH} text-right w-24 hidden sm:table-cell`}>{t('receipts.detail.column.unitPrice')}</th>
                      <th className={`${TH} text-right w-24`}>{t('receipts.detail.column.total')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {receipt.items.map((item) => (
                      <tr key={item.id} className="border-b border-border last:border-0" data-testid="receipt-item">
                        <td className="py-2 text-sm">
                          {item.product_id ? (
                            <Link to={`/receipts/products/${item.product_id}`} className="hover:underline" onClick={onClose}>
                              {item.name}
                            </Link>
                          ) : (
                            item.name
                          )}
                          {item.product_name && item.product_name !== item.name && (
                            <div className="text-xs text-muted-foreground">{item.product_name}</div>
                          )}
                        </td>
                        <td className="py-2 text-right text-xs tabular-nums">
                          {formatQuantity(item.quantity, locale)} {item.unit ?? ''}
                        </td>
                        <td className="py-2 text-right text-xs tabular-nums hidden sm:table-cell">{money(item.unit_price)}</td>
                        <td className="py-2 text-right text-sm tabular-nums">
                          {money(Number(item.total) - Number(item.discount))}
                          {Number(item.discount) > 0 && (
                            <div className="text-[11px] text-emerald-600 dark:text-emerald-400">−{money(item.discount)}</div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="flex justify-end gap-6 pt-3 text-sm">
                  {Number(receipt.discount_total) > 0 && (
                    <span className="text-emerald-600 dark:text-emerald-400">
                      {t('receipts.detail.discounts')}: −{money(receipt.discount_total)}
                    </span>
                  )}
                  <span className="font-semibold">
                    {t('receipts.detail.total')}: {money(receipt.total)}
                  </span>
                </div>
              </div>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
