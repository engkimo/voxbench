import { Bot, Send } from 'lucide-react'
import { FormEvent, useEffect, useRef, useState } from 'react'

import {
  acknowledgeUiCommand,
  createDiagnosticSession,
  diagnosticSessionEventsUrl,
} from './diagnosticSessions'
import type {
  DiagnosticSession,
  DiagnosticSessionEvent,
  ResolvedUiCommand,
  UiCommandExecutionResult,
} from './types'

type Props = {
  apiBase: string
  onResolved: (command: ResolvedUiCommand) => UiCommandExecutionResult
  runId: string
}

type StreamState = 'idle' | 'connecting' | 'streaming' | 'completed' | 'error'

const EVENT_TYPES: DiagnosticSessionEvent['type'][] = [
  'status',
  'evidence_found',
  'answer_delta',
  'ui_command_proposed',
  'ui_command',
  'ui_command_result',
  'completed',
]

export function AgentDiagnosticPanel({
  apiBase,
  onResolved,
  runId,
}: Props) {
  const [message, setMessage] = useState('Show me the strongest evidence in this run.')
  const [pending, setPending] = useState(false)
  const [session, setSession] = useState<DiagnosticSession | null>(null)
  const [events, setEvents] = useState<DiagnosticSessionEvent[]>([])
  const [streamState, setStreamState] = useState<StreamState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<UiCommandExecutionResult | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)
  const requestRef = useRef<AbortController | null>(null)

  useEffect(() => () => {
    requestRef.current?.abort()
    eventSourceRef.current?.close()
  }, [])

  function connect(sessionId: string, signal: AbortSignal) {
    eventSourceRef.current?.close()
    setStreamState('connecting')
    const source = new EventSource(
      diagnosticSessionEventsUrl(apiBase, sessionId),
      { withCredentials: true },
    )
    eventSourceRef.current = source
    source.addEventListener('open', () => setStreamState('streaming'))
    const seenOrdinals = new Set<number>()
    const deliveredCommands = new Set<string>()
    for (const eventType of EVENT_TYPES) {
      source.addEventListener(eventType, (rawEvent) => {
        try {
          const event = JSON.parse((rawEvent as MessageEvent<string>).data) as DiagnosticSessionEvent
          if (signal.aborted || event.session_id !== sessionId || seenOrdinals.has(event.ordinal)) return
          seenOrdinals.add(event.ordinal)
          setEvents((current) => [...current, event])
          if (event.type === 'ui_command') {
            const command = event.payload.command as ResolvedUiCommand | undefined
            if (!command || command.status !== 'resolved' || command.target.run_id !== runId) {
              throw new Error('Agent stream returned an invalid UI command')
            }
            if (deliveredCommands.has(command.command_id)) return
            deliveredCommands.add(command.command_id)
            const result = onResolved(command)
            setLastResult(result)
            void acknowledgeUiCommand(apiBase, sessionId, result)
              .then(() => {
                if (signal.aborted) return
                setSession((current) => current && {
                  ...current,
                  acknowledged_command_count: current.acknowledged_command_count + 1,
                })
              })
              .catch((caught) => {
                if (!signal.aborted) {
                  setError(caught instanceof Error ? caught.message : 'Command acknowledgement failed')
                }
              })
          }
          if (event.type === 'completed') {
            setStreamState('completed')
            source.close()
          }
        } catch (caught) {
          setStreamState('error')
          setError(caught instanceof Error ? caught.message : 'Agent event was invalid')
          source.close()
        }
      })
    }
    source.addEventListener('error', () => {
      setStreamState((current) => current === 'completed' ? current : 'error')
      source.close()
    })
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    requestRef.current?.abort()
    eventSourceRef.current?.close()
    const request = new AbortController()
    requestRef.current = request
    setPending(true)
    setSession(null)
    setStreamState('connecting')
    setError(null)
    setEvents([])
    setLastResult(null)
    try {
      const created = await createDiagnosticSession(apiBase, runId, message, request.signal)
      if (request.signal.aborted) return
      setSession(created)
      connect(created.session_id, request.signal)
    } catch (caught) {
      if (request.signal.aborted) return
      setStreamState('error')
      setError(caught instanceof Error ? caught.message : 'Diagnostic session failed')
    } finally {
      if (!request.signal.aborted) setPending(false)
    }
  }

  const answer = events
    .filter((event) => event.type === 'answer_delta')
    .map((event) => event.payload.text)
    .filter((value): value is string => typeof value === 'string')
    .join('')
  const evidence = events.find((event) => event.type === 'evidence_found')
  const evidenceRefs = evidence?.payload.evidence_refs
  const command = events.find((event) => event.type === 'ui_command')
    ?.payload.command as ResolvedUiCommand | undefined

  return (
    <section className="agentDiagnosticPanel">
      <div className="sectionHeader compact">
        <Bot size={17} />
        <h2>Ask VoxBench</h2>
        <span className={`agentStreamState ${streamState}`}>{streamState}</span>
      </div>
      <p>
        Ask the local diagnostic agent to show evidence from this run in the inspector.
      </p>
      <form onSubmit={submit}>
        <label>
          Investigation request
          <textarea
            maxLength={500}
            onChange={(event) => setMessage(event.target.value)}
            value={message}
          />
        </label>
        <button disabled={pending || message.trim().length === 0} type="submit">
          <Send size={14} />
          {pending ? 'Starting' : 'Investigate & guide UI'}
        </button>
      </form>
      {answer ? <div className="agentAnswer">{answer}</div> : null}
      {evidence ? (
        <div className="agentEvidence">
          <strong>{String(evidence.payload.classification ?? 'evidence')}</strong>
          <span>{String(evidence.payload.summary ?? '')}</span>
          {Array.isArray(evidenceRefs) && evidenceRefs.length > 0 && (
            <ul aria-label="Evidence references">
              {evidenceRefs.filter((ref): ref is string => typeof ref === 'string').map((ref) => (
                <li key={ref}><code>{ref}</code></li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      {command ? (
        <div className="agentCommandDelivery">
          <span>Delivered {command.type}</span>
          <small>{command.command_id}</small>
        </div>
      ) : null}
      {lastResult ? (
        <div className={`agentCommandResult ${lastResult.outcome}`}>
          <strong>{lastResult.outcome}</strong>
          <span>{lastResult.detail}</span>
          <small>
            {session && session.acknowledged_command_count > 0
              ? 'Acknowledged by Control Plane'
              : 'Acknowledging result'}
          </small>
        </div>
      ) : null}
      {session ? (
        <small className="agentSessionMeta">
          local session {session.session_id} · {session.acknowledged_command_count}/
          {session.command_count} acknowledged
        </small>
      ) : null}
      {error ? <div className="agentCommandResult rejected">{error}</div> : null}
    </section>
  )
}
