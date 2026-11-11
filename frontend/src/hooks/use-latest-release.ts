import { useQuery } from '@tanstack/react-query'
import { useAutoUpdateCheck } from '@/hooks/use-auto-update-check'

/**
 * Off in this fork. The check asks GitHub for the *upstream* project's
 * latest release from the visitor's browser: it leaks the visitor's IP to
 * GitHub on every load, and offers an upstream version this fork must not
 * be replaced with. The fork updates by pulling its own images.
 */
export const UPDATE_CHECK_AVAILABLE = false

const LATEST_RELEASE_URL =
  'https://api.github.com/repos/securo-finance/securo/releases/latest'

const SIX_HOURS = 1000 * 60 * 60 * 6

export type LatestRelease = {
  tagName: string
  htmlUrl: string
}

async function fetchLatestRelease(): Promise<LatestRelease | null> {
  const response = await fetch(LATEST_RELEASE_URL, {
    headers: { Accept: 'application/vnd.github+json' },
  })
  if (!response.ok) return null
  const payload = (await response.json()) as {
    tag_name?: string
    html_url?: string
  }
  if (!payload.tag_name || !payload.html_url) return null
  return { tagName: payload.tag_name, htmlUrl: payload.html_url }
}

export function useLatestRelease() {
  const { enabled } = useAutoUpdateCheck()
  return useQuery<LatestRelease | null>({
    queryKey: ['latest-release', 'securo-finance/securo'],
    queryFn: fetchLatestRelease,
    enabled: UPDATE_CHECK_AVAILABLE && enabled,
    staleTime: SIX_HOURS,
    gcTime: SIX_HOURS,
    retry: 1,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
}
