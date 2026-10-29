import type { RetailConnectionStatus, ShoppingConsumption } from '@/types'

/** Store chains by provider id. Receipts keep the provider as their source. */
export const RETAIL_SOURCE_LABELS: Record<string, string> = {
  lidl_plus: 'Lidl',
  biedronka: 'Biedronka',
  allegro: 'Allegro',
}

export function sourceLabel(source: string): string {
  return RETAIL_SOURCE_LABELS[source] ?? source
}

/** The message a ReceiptError carries (`{"detail": "..."}`), when there is one. */
export function apiErrorMessage(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message
    return typeof message === 'string' ? message : null
  }
  return null
}

/** Only a sign-in the store refused needs the person; the rest retries itself. */
export function needsSignIn(status: RetailConnectionStatus): boolean {
  return status === 'pending' || status === 'reauth_required'
}

export type StockLevel = 'unknown' | 'out' | 'low' | 'ok'

/**
 * How urgent restocking is, from the consumption forecast. "Low" is the
 * last fifth of a typical purchase interval, with a floor of three days so
 * a daily purchase does not sit permanently in the warning state.
 */
export function stockLevel(consumption: ShoppingConsumption | null | undefined): StockLevel {
  if (!consumption || consumption.days_left === null) return 'unknown'
  if (consumption.days_left <= 0) return 'out'
  const interval = consumption.avg_days_between ?? 0
  if (consumption.days_left <= Math.max(3, interval * 0.2)) return 'low'
  return 'ok'
}

/** Quantities as printed: whole pieces without decimals, weights with up to three. */
export function formatQuantity(value: string | number, locale: string): string {
  const n = Number(value)
  return n.toLocaleString(locale, { maximumFractionDigits: Number.isInteger(n) ? 0 : 3 })
}
