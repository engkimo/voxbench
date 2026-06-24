import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  Headphones,
  RefreshCw,
  Search,
  Server,
  Signal,
  Waves,
} from 'lucide-react'
import { FormEvent, useMemo, useState } from 'react'

import type {
  TimelineMetricPoint,
  TimelineRecording,
  TimelineResponse,
  TimelineStageLane,
} from './types'

const DEFAULT_API_BASE = '/api'

async function fetchTimeline(apiBase: string, runId: string): Promise<TimelineResponse> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/${encodeURIComponent(runId)}/timeline`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

export function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE)
  const [draftRunId, setDraftRunId] = useState('')
  const [runId, setRunId] = useState('')

  const timelineQuery = useQuery({
    queryKey: ['timeline', apiBase, runId],
    queryFn: () => fetchTimeline(apiBase, runId),
    enabled: runId.trim().length > 0,
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setRunId(draftRunId.trim())
  }

  const timeline = timelineQuery.data
  const failedCount = useMemo(
    () =>
      timeline?.lanes.stages.reduce(
        (count, stage) => count + stage.violations.length,
        0,
      ) ?? 0,
    [timeline],
  )

  return (
    <main className="appShell">
      <header className="topBar">
        <div>
          <h1>VoxBench</h1>
          <p>{timeline ? shortHash(timeline.config_hash) : 'timeline inspector'}</p>
        </div>
        <form className="runForm" onSubmit={submit}>
          <label>
            API
            <input
              value={apiBase}
              onChange={(event) => setApiBase(event.target.value)}
              placeholder="/api"
            />
          </label>
          <label>
            Run
            <input
              value={draftRunId}
              onChange={(event) => setDraftRunId(event.target.value)}
              placeholder="run_id"
            />
          </label>
          <button type="submit" title="Fetch timeline">
            <Search size={17} />
            Fetch
          </button>
          <button
            type="button"
            title="Refresh"
            onClick={() => void timelineQuery.refetch()}
            disabled={!runId}
          >
            <RefreshCw size={17} />
          </button>
        </form>
      </header>

      <section className="summaryGrid">
        <SummaryTile icon={<Database size={18} />} label="Run" value={timeline?.run_id ?? '-'} />
        <SummaryTile icon={<Clock3 size={18} />} label="t0" value={formatDate(timeline?.t0)} />
        <SummaryTile icon={<AlertTriangle size={18} />} label="Violations" value={failedCount} />
        <SummaryTile
          icon={<Headphones size={18} />}
          label="Recordings"
          value={timeline?.lanes.recordings.length ?? 0}
        />
      </section>

      {timelineQuery.isPending && runId ? <StatusPanel state="loading" /> : null}
      {timelineQuery.isError ? <StatusPanel state="error" detail={timelineQuery.error.message} /> : null}
      {!runId ? <StatusPanel state="idle" /> : null}

      {timeline ? (
        <section className="timelineGrid">
          <div className="timelineMain">
            <div className="sectionHeader">
              <Activity size={18} />
              <h2>Stages</h2>
            </div>
            <div className="stageStack">
              {timeline.lanes.stages.map((stage) => (
                <StageLane key={stage.stage} stage={stage} />
              ))}
            </div>
          </div>

          <aside className="sideRail">
            <LaneStatus title="SIP" icon={<Signal size={17} />} count={timeline.lanes.sip_ladder.length} />
            <LaneStatus title="RTP" icon={<Waves size={17} />} count={timeline.lanes.rtp_quality.length} />
            <LaneStatus title="Turns" icon={<Headphones size={17} />} count={timeline.lanes.turns.length} />
            <LaneStatus title="Host" icon={<Server size={17} />} count={timeline.lanes.host.length} />
            <Recordings
              apiBase={apiBase}
              recordings={timeline.lanes.recordings}
              runId={timeline.run_id}
            />
          </aside>
        </section>
      ) : null}
    </main>
  )
}

function SummaryTile({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
}) {
  return (
    <div className="summaryTile">
      <span className="summaryIcon">{icon}</span>
      <span className="summaryLabel">{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function StatusPanel({ state, detail }: { state: 'idle' | 'loading' | 'error'; detail?: string }) {
  const copy = {
    idle: ['Ready', 'Enter a run_id'],
    loading: ['Loading', 'Fetching timeline'],
    error: ['Error', detail ?? 'Request failed'],
  }[state]

  return (
    <section className={`statusPanel ${state}`}>
      <AlertTriangle size={18} />
      <div>
        <strong>{copy[0]}</strong>
        <span>{copy[1]}</span>
      </div>
    </section>
  )
}

function StageLane({ stage }: { stage: TimelineStageLane }) {
  const failed = stage.violations.length > 0
  return (
    <article className={`stageLane ${failed ? 'failed' : 'passed'}`}>
      <div className="stageHeader">
        <div>
          <h3>{stage.stage}</h3>
          <span>{stage.metrics.length} metrics</span>
        </div>
        <span className={`statusBadge ${failed ? 'failed' : 'passed'}`}>
          {failed ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
          {failed ? `${stage.violations.length} fail` : 'pass'}
        </span>
      </div>
      <MetricStrip metrics={stage.metrics} />
      {failed ? (
        <div className="violations">
          {stage.violations.map((violation) => (
            <div key={`${stage.stage}-${violation.invariant}`} className="violation">
              <strong>{violation.invariant}</strong>
              <span>{violation.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  )
}

function MetricStrip({ metrics }: { metrics: TimelineMetricPoint[] }) {
  if (metrics.length === 0) {
    return <div className="emptyInline">No metrics</div>
  }

  return (
    <div className="metricStrip">
      {metrics.map((metric) => (
        <div key={`${metric.name}-${metric.ts}`} className="metricPill">
          <span>{metric.name}</span>
          <strong>{formatNumber(metric.value)}</strong>
          <small>{metric.ts.toFixed(3)}s</small>
        </div>
      ))}
    </div>
  )
}

function LaneStatus({ title, icon, count }: { title: string; icon: React.ReactNode; count: number }) {
  return (
    <div className="laneStatus">
      <span>{icon}</span>
      <strong>{title}</strong>
      <em>{count === 0 ? 'empty' : count}</em>
    </div>
  )
}

function Recordings({
  apiBase,
  recordings,
  runId,
}: {
  apiBase: string
  recordings: TimelineRecording[]
  runId: string
}) {
  const base = apiBase.replace(/\/$/, '')
  return (
    <section className="recordings">
      <div className="sectionHeader compact">
        <Headphones size={17} />
        <h2>Recordings</h2>
      </div>
      <div className="recordingList">
        {recordings.map((recording) => {
          const src = `${base}/runs/${encodeURIComponent(runId)}/recordings/${encodeURIComponent(recording.stage)}/audio`
          return (
            <div key={recording.stage} className="recordingItem">
              <div className="recordingMeta">
                <strong>{recording.stage}</strong>
                <span>{formatNumber(recording.duration_ms)} ms</span>
              </div>
              <audio controls preload="none" src={src} />
            </div>
          )
        })}
      </div>
    </section>
  )
}

function shortHash(hash: string) {
  return hash.slice(0, 12)
}

function formatDate(value?: string) {
  if (!value) return '-'
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value))
}

function formatNumber(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(3)
}
