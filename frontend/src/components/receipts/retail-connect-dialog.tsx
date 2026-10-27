import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { retail } from '@/lib/api'
import { apiErrorMessage } from '@/lib/receipt-utils'
import { cn } from '@/lib/utils'
import type { RetailConnection, RetailConnectionStarted } from '@/types'

interface RetailConnectDialogProps {
  open: boolean
  onClose: () => void
  /** Sign an existing connection in again instead of adding a new one. */
  reconnect?: RetailConnection | null
}

/**
 * Two steps, because neither store lets a server sign in: their login
 * pages carry a captcha. Securo makes the sign-in link (it holds the PKCE
 * verifier), the person signs in in their own browser, and the store's
 * final redirect to the app's `app://` address, which the browser cannot
 * open, is pasted back here.
 */
export function RetailConnectDialog({ open, onClose, reconnect }: RetailConnectDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      {open && <ConnectFlow key={reconnect?.id ?? 'new'} onClose={onClose} reconnect={reconnect ?? null} />}
    </Dialog>
  )
}

function ConnectFlow({ onClose, reconnect }: { onClose: () => void; reconnect: RetailConnection | null }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [started, setStarted] = useState<RetailConnectionStarted | null>(null)
  const [mode, setMode] = useState<'redirect' | 'token'>('redirect')
  const [value, setValue] = useState('')

  const { data: providers } = useQuery({ queryKey: ['retail', 'providers'], queryFn: retail.providers })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['retail'] })
    void queryClient.invalidateQueries({ queryKey: ['receipts'] })
    void queryClient.invalidateQueries({ queryKey: ['shopping-products'] })
  }
  const onError = (error: unknown) => toast.error(apiErrorMessage(error) ?? t('receipts.errors.generic'))

  const start = useMutation({
    mutationFn: (provider: string) =>
      reconnect ? retail.reauthorize(reconnect.id) : retail.connect(provider),
    onSuccess: (result) => {
      setStarted(result)
      refresh()
    },
    onError,
  })

  const finish = useMutation({
    mutationFn: async () => {
      const id = started!.connection.id
      const payload = mode === 'token' ? { refresh_token: value.trim() } : { redirect_url: value.trim() }
      await retail.authorize(id, payload)
      // The first sync runs right away so the person sees receipts, not an empty list.
      return retail.sync(id)
    },
    onSuccess: (result) => {
      toast.success(
        t('receipts.connect.connected', { created: result.created, matched: result.matched }),
      )
      refresh()
      onClose()
    },
    onError,
  })

  const provider = providers?.find((p) => p.name === (started?.connection.provider ?? reconnect?.provider))

  return (
    <DialogContent className="sm:max-w-lg" data-testid="retail-connect-dialog">
      <DialogHeader>
        <DialogTitle>
          {reconnect ? t('receipts.connect.reconnectTitle') : t('receipts.connect.title')}
        </DialogTitle>
        <DialogDescription>{t('receipts.connect.description')}</DialogDescription>
      </DialogHeader>

      {!started ? (
        reconnect ? (
          <div className="space-y-3 text-sm">
            <p>{t('receipts.connect.reconnectBody', { name: reconnect.display_name ?? reconnect.provider })}</p>
            <Button onClick={() => start.mutate(reconnect.provider)} disabled={start.isPending}>
              {start.isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />}
              {t('receipts.connect.getLink')}
            </Button>
          </div>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {(providers ?? []).map((p) => (
              <button
                key={p.name}
                type="button"
                disabled={start.isPending}
                onClick={() => start.mutate(p.name)}
                data-testid={`retail-provider-${p.name}`}
                className="rounded-lg border border-border px-4 py-3 text-left hover:bg-muted transition-colors disabled:opacity-50"
              >
                <div className="text-sm font-medium">{p.display_name}</div>
                <div className="text-xs text-muted-foreground">{t(`receipts.connect.provider.${p.name}`, '')}</div>
              </button>
            ))}
          </div>
        )
      ) : (
        <div className="space-y-4 text-sm">
          <ol className="list-decimal pl-5 space-y-2 text-muted-foreground">
            <li>
              {t('receipts.connect.step1')}{' '}
              <a
                href={started.auth_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
                data-testid="retail-auth-link"
              >
                {t('receipts.connect.openLogin')}
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </li>
            <li>{t('receipts.connect.step2')}</li>
            <li>{t('receipts.connect.step3', { prefix: provider?.redirect_prefix ?? 'app://' })}</li>
          </ol>

          <div className="inline-flex rounded-lg border border-border bg-muted p-0.5 text-xs font-medium">
            {(['redirect', 'token'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={cn(
                  'px-3 py-1 rounded-md transition-colors',
                  mode === m ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground',
                )}
              >
                {t(`receipts.connect.mode.${m}`)}
              </button>
            ))}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="retail-auth-value">
              {mode === 'redirect' ? t('receipts.connect.redirectLabel') : t('receipts.connect.tokenLabel')}
            </Label>
            <textarea
              id="retail-auth-value"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              rows={4}
              spellCheck={false}
              placeholder={mode === 'redirect' ? `${provider?.redirect_prefix ?? 'app://'}?code=…` : ''}
              className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-ring"
              data-testid="retail-auth-value"
            />
            <p className="text-xs text-muted-foreground">
              {mode === 'redirect' ? t('receipts.connect.redirectHint') : t('receipts.connect.tokenHint')}
            </p>
          </div>
        </div>
      )}

      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          {t('common.cancel')}
        </Button>
        {started && (
          <Button
            onClick={() => finish.mutate()}
            disabled={!value.trim() || finish.isPending}
            data-testid="retail-auth-submit"
          >
            {finish.isPending && <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />}
            {t('receipts.connect.submit')}
          </Button>
        )}
      </DialogFooter>
    </DialogContent>
  )
}
