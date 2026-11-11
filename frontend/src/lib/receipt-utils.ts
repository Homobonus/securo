// The instance the app configures in @/lib/i18n; imported bare so this stays usable outside a browser.
import i18n from 'i18next'
import type { ReceiptScanLine, RetailConnectionStatus, ShoppingConsumption } from '@/types'

/** Store chains by provider id. Receipts keep the provider as their source. */
export const RETAIL_SOURCE_LABELS: Record<string, string> = {
  lidl_plus: 'Lidl',
  biedronka: 'Biedronka',
  allegro: 'Allegro',
}

/** Receipts read off a photo; any shop. */
export const SCAN_SOURCE = 'scan'

export function sourceLabel(source: string): string {
  if (source === SCAN_SOURCE) return i18n.t('receipts.scan.source')
  return RETAIL_SOURCE_LABELS[source] ?? source
}

/** A stable error code from the scan API, in words; unknown codes read as a generic failure. */
export function scanErrorText(code: string | null): string {
  if (!code) return ''
  return i18n.t(`receipts.scan.errors.${code}`, { defaultValue: i18n.t('receipts.scan.errors.provider_error') })
}

/** What the lines of a draft add up to, after their discounts. */
export function scanLinesSum(lines: ReceiptScanLine[]): number {
  const cents = lines.reduce(
    (sum, line) => sum + Math.round((Number(line.total) || 0) * 100) - Math.round((Number(line.discount) || 0) * 100),
    0,
  )
  return cents / 100
}

/** Longest side of a photo sent for reading; matches the server's. */
const MAX_PHOTO_SIDE = 2400

/**
 * Shrink a phone photo before it is uploaded: a 5 MB camera JPEG becomes a
 * few hundred kilobytes, uploads quickly on mobile data, and stays under
 * the proxy's body limit. Orientation from EXIF is applied. When the
 * browser cannot decode the file, it is sent as it is and the server
 * decides.
 */
export async function shrinkPhoto(file: File): Promise<Blob> {
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') return file
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
    const scale = Math.min(1, MAX_PHOTO_SIDE / Math.max(bitmap.width, bitmap.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(bitmap.width * scale)
    canvas.height = Math.round(bitmap.height * scale)
    const context = canvas.getContext('2d')
    if (!context) return file
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    bitmap.close()
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85))
    return blob ?? file
  } catch {
    return file
  }
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
