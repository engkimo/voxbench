import type {
  DiagnosticSession,
  UiCommandExecutionResult,
} from './types'

async function responseDetail(response: Response) {
  let detail = `HTTP ${response.status}`
  try {
    const payload = (await response.json()) as { detail?: unknown }
    if (typeof payload.detail === 'string') {
      detail = payload.detail
    }
  } catch {
    // Keep the bounded HTTP fallback.
  }
  return detail
}

export async function createDiagnosticSession(
  apiBase: string,
  runId: string,
  message: string,
  signal: AbortSignal,
): Promise<DiagnosticSession> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(
    `${base}/runs/${encodeURIComponent(runId)}/diagnostic-sessions`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message }),
      signal,
    },
  )
  if (!response.ok) {
    throw new Error(await responseDetail(response))
  }
  return response.json()
}

export function diagnosticSessionEventsUrl(apiBase: string, sessionId: string) {
  const base = apiBase.replace(/\/$/, '')
  return `${base}/diagnostic-sessions/${encodeURIComponent(sessionId)}/events`
}

export async function acknowledgeUiCommand(
  apiBase: string,
  sessionId: string,
  result: UiCommandExecutionResult,
): Promise<void> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(
    `${base}/diagnostic-sessions/${encodeURIComponent(sessionId)}/ui-command-results`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(result),
    },
  )
  if (!response.ok) {
    throw new Error(await responseDetail(response))
  }
}
