import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2, RefreshCw, Store, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { IconAction, SectionCard } from '@/components/invoice-ui'
import { accounts as accountsApi, retail } from '@/lib/api'
import { apiErrorMessage, needsSignIn } from '@/lib/receipt-utils'
import { cn } from '@/lib/utils'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { useWorkspace } from '@/contexts/workspace-context'
import type { RetailConnection, RetailConnectionStatus } from '@/types'

const STATUS_TONE: Record<RetailConnectionStatus, string> = {
  active: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/20',
  pending: 'bg-muted text-muted-foreground border-border',
  reauth_required: 'bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/20',
  error: 'bg-rose-500/10 text-rose-700 dark:text-rose-400 border-rose-500/20',
}

const ANY_ACCOUNT = '__any__'

export function RetailConnectionsPanel({
  onConnect,
  onReconnect,
}: {
  onConnect: () => void
  onReconnect: (connection: RetailConnection) => void
}) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()

  const { data: list, isLoading } = useQuery({ queryKey: ['retail', 'connections'], queryFn: retail.connections })
  const { data: accountList } = useQuery({ queryKey: ['accounts'], queryFn: () => accountsApi.list() })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['retail'] })
    void queryClient.invalidateQueries({ queryKey: ['receipts'] })
    void queryClient.invalidateQueries({ queryKey: ['shopping-products'] })
  }
  const onError = (error: unknown) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic'))

  const sync = useMutation({
    mutationFn: (id: string) => retail.sync(id),
    onSuccess: (result) => {
      if (result.connection.status === 'reauth_required') {
        toast.error(t('receipts.connections.reauthNeeded'))
      } else {
        toast.success(
          t(result.has_more ? 'receipts.connections.syncedMore' : 'receipts.connections.synced', {
            created: result.created,
            matched: result.matched,
          }),
        )
      }
      refresh()
    },
    onError,
  })
  const update = useMutation({
    mutationFn: ({ id, accountId }: { id: string; accountId: string | null }) =>
      retail.update(id, { match_account_id: accountId }),
    onSuccess: refresh,
    onError,
  })
  const remove = useMutation({
    mutationFn: (id: string) => retail.remove(id),
    onSuccess: () => {
      toast.success(t('receipts.connections.removed'))
      refresh()
    },
    onError,
  })

  if (isLoading) {
    return (
      <SectionCard className="p-5 space-y-3">
        {[0, 1].map((i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </SectionCard>
    )
  }

  if (!list || list.length === 0) {
    return (
      <SectionCard>
        <div className="px-5 py-14 text-center" data-testid="retail-connections-empty">
          <Store className="h-8 w-8 mx-auto text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground max-w-sm mx-auto">{t('receipts.connections.empty')}</p>
          {canWrite && (
            <Button size="sm" className="mt-4" onClick={onConnect}>
              {t('receipts.connect.title')}
            </Button>
          )}
        </div>
      </SectionCard>
    )
  }

  return (
    <SectionCard>
      <ul className="divide-y divide-border">
        {list.map((connection) => (
          <li
            key={connection.id}
            className="px-4 sm:px-5 py-4 flex flex-wrap items-center gap-3"
            data-testid="retail-connection-row"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium truncate">{connection.display_name ?? connection.provider}</span>
                <span
                  className={cn(
                    'text-[11px] font-semibold px-2 py-0.5 rounded-full border whitespace-nowrap',
                    STATUS_TONE[connection.status],
                  )}
                  data-testid={`retail-status-${connection.status}`}
                >
                  {t(`receipts.connections.status.${connection.status}`)}
                </span>
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                {t('receipts.connections.summary', {
                  receipts: connection.receipt_count,
                  date: connection.last_sync_at
                    ? new Date(connection.last_sync_at).toLocaleString(locale)
                    : t('receipts.connections.never'),
                })}
              </div>
              {connection.last_error && connection.status !== 'active' && (
                <div className="text-xs text-rose-600 dark:text-rose-400 mt-0.5 truncate">{connection.last_error}</div>
              )}
            </div>

            {canWrite && (
              <div className="flex items-center gap-2">
                <Select
                  value={connection.match_account_id ?? ANY_ACCOUNT}
                  onValueChange={(value) =>
                    update.mutate({ id: connection.id, accountId: value === ANY_ACCOUNT ? null : value })
                  }
                >
                  <SelectTrigger className="h-8 w-44 text-xs" aria-label={t('receipts.connections.matchAccount')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ANY_ACCOUNT}>{t('receipts.connections.anyAccount')}</SelectItem>
                    {(accountList ?? []).map((account) => (
                      <SelectItem key={account.id} value={account.id}>
                        {account.display_name ?? account.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {needsSignIn(connection.status) ? (
                  <Button size="sm" variant="outline" onClick={() => onReconnect(connection)}>
                    <KeyRound className="h-3.5 w-3.5 mr-1.5" />
                    {t('receipts.connections.signIn')}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={sync.isPending}
                    onClick={() => sync.mutate(connection.id)}
                    data-testid="retail-sync"
                  >
                    {sync.isPending && sync.variables === connection.id ? (
                      <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
                    )}
                    {t('receipts.connections.sync')}
                  </Button>
                )}
                {!needsSignIn(connection.status) && (
                  <IconAction onClick={() => onReconnect(connection)} label={t('receipts.connections.signIn')}>
                    <KeyRound className="h-3.5 w-3.5" />
                  </IconAction>
                )}
                <IconAction
                  destructive
                  onClick={() => {
                    if (window.confirm(t('receipts.connections.confirmRemove'))) remove.mutate(connection.id)
                  }}
                  label={t('common.delete')}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </IconAction>
              </div>
            )}
          </li>
        ))}
      </ul>
    </SectionCard>
  )
}
