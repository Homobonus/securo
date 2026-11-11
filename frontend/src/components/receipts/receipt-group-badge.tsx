import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import type { ReceiptTransaction } from '@/types'

/** The group the paying transaction is split into: the same violet pill
 *  the transaction list shows, opening the group on click. */
export function ReceiptGroupBadge({
  transaction,
  className,
}: {
  transaction: ReceiptTransaction | null | undefined
  className?: string
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  if (!transaction?.group_id) return null
  const groupId = transaction.group_id
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation()
        navigate(`/groups/${groupId}`)
      }}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-violet-700 bg-violet-50 border border-violet-200 dark:bg-violet-950/40 dark:text-violet-300 dark:border-violet-900 px-1.5 py-0.5 rounded-full hover:bg-violet-100 dark:hover:bg-violet-900/40',
        className,
      )}
      title={t('receipts.list.groupTooltip')}
      data-testid="receipt-group-badge"
    >
      {t('splitGroups.ownerRowBadge', { group: transaction.group_name ?? '' })}
    </button>
  )
}
