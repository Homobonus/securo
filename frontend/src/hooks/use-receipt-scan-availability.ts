import { useQuery } from '@tanstack/react-query'

import { receiptScans } from '@/lib/api'

/** Whether this install reads receipt photos (a model is configured on the server). */
export function useReceiptScanAvailability(): boolean {
  const { data } = useQuery({
    queryKey: ['receipt-scans', 'availability'],
    queryFn: receiptScans.availability,
    staleTime: Infinity,
  })
  return data?.enabled ?? false
}
