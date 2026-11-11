import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Camera, ChevronRight, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { SectionCard } from '@/components/invoice-ui'
import { receiptScans } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { formatDate } from '@/lib/date-range-format'
import { apiErrorMessage, scanErrorText, shrinkPhoto } from '@/lib/receipt-utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'

/**
 * Take or pick photos of paper receipts. Each is shrunk in the browser,
 * uploaded and read one after another; reading takes a while, so the
 * button says so. A single photo opens for review as soon as it is read.
 */
export function ReceiptPhotoButton({ onScanned }: { onScanned: (scanId: string) => void }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [pending, setPending] = useState(0)

  const upload = async (files: File[]) => {
    setPending(files.length)
    const toastId = toast.loading(t('receipts.scan.reading'))
    let lastRead: string | null = null
    let failed = 0
    for (const file of files) {
      try {
        const scan = await receiptScans.upload(await shrinkPhoto(file), file.name || 'receipt.jpg')
        if (scan.status === 'failed') failed += 1
        lastRead = scan.id
      } catch (error) {
        failed += 1
        const code = apiErrorMessage(error)
        toast.error(code ? scanErrorText(code) : t('receipts.errors.generic'))
      }
      setPending((n) => n - 1)
    }
    toast.dismiss(toastId)
    void queryClient.invalidateQueries({ queryKey: ['receipt-scans'] })
    if (files.length === 1 && lastRead) {
      onScanned(lastRead)
    } else if (files.length > 1) {
      toast.success(t('receipts.scan.batchDone', { count: files.length - failed }))
    }
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        data-testid="receipt-photo-input"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? [])
          e.target.value = ''
          if (files.length > 0) void upload(files)
        }}
      />
      <Button
        size="sm"
        variant="outline"
        disabled={pending > 0}
        onClick={() => input.current?.click()}
        data-testid="receipt-photo-button"
      >
        {pending > 0 ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <Camera className="h-4 w-4 mr-1.5" />}
        {pending > 0 ? t('receipts.scan.readingCount', { count: pending }) : t('receipts.scan.button')}
      </Button>
    </>
  )
}

/** Photos read but not yet accepted. Nothing shows when there are none. */
export function ReceiptScansPanel({ onOpen }: { onOpen: (scanId: string) => void }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const { data: scans } = useQuery({ queryKey: ['receipt-scans', 'list'], queryFn: receiptScans.list })

  if (!scans || scans.length === 0) return null
  return (
    <SectionCard className="mb-4">
      <div className="px-4 sm:px-5 pt-3 pb-1 text-xs font-medium text-muted-foreground">
        {t('receipts.scan.waiting', { count: scans.length })}
      </div>
      <ul>
        {scans.map((scan) => {
          const draft = scan.draft
          return (
            <li key={scan.id} className="border-t border-border first:border-0">
              <button
                type="button"
                onClick={() => onOpen(scan.id)}
                className="w-full flex items-center gap-3 px-4 sm:px-5 py-2.5 text-left hover:bg-muted/50"
                data-testid="receipt-scan-row"
              >
                {scan.status === 'failed' ? (
                  <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600 dark:text-rose-400" />
                ) : scan.status === 'processing' ? (
                  <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
                ) : (
                  <Camera className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium truncate">
                    {draft?.store_name ?? t('receipts.scan.unknownStore')}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">
                    {scan.status === 'failed'
                      ? scanErrorText(scan.error)
                      : [
                          draft?.date ? formatDate(draft.date, locale) : null,
                          draft ? t('receipts.scan.lines', { count: draft.items.length }) : null,
                        ]
                          .filter(Boolean)
                          .join(' · ')}
                  </div>
                </div>
                {draft?.total && (
                  <span className="text-sm tabular-nums">
                    {mask(formatCurrency(Number(draft.total), draft.currency, locale))}
                  </span>
                )}
                <ChevronRight className="h-4 w-4 text-muted-foreground" />
              </button>
            </li>
          )
        })}
      </ul>
    </SectionCard>
  )
}
