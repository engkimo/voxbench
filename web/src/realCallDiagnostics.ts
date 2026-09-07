import type { RealCallExperiment } from './types'

export async function diagnoseRealCallExperiment(
  apiBase: string,
  primaryRunId: string,
  compareRunId: string,
): Promise<RealCallExperiment> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/diagnostics/real-call-experiment`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      primary_run_id: primaryRunId,
      compare_run_id: compareRunId.trim() || null,
    }),
  })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const payload = await response.json() as { detail?: unknown }
      if (typeof payload.detail === 'string') detail = payload.detail
    } catch {
      // Keep the bounded HTTP fallback.
    }
    throw new Error(detail)
  }
  return response.json()
}
