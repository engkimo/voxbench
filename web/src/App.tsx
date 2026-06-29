import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock3,
  Database,
  Headphones,
  ListChecks,
  Pause,
  Play,
  RefreshCw,
  Search,
  Server,
  Signal,
  Waves,
} from 'lucide-react'
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import WaveSurfer from 'wavesurfer.js'

import type {
  TimelineMetricPoint,
  TimelineRecording,
  TimelineResponse,
  TimelineStageLane,
  TimelineViolation,
  RunSummary,
} from './types'

const DEFAULT_API_BASE = '/api'

type DetailTab = 'metrics' | 'checks' | 'audio'

async function fetchTimeline(apiBase: string, runId: string): Promise<TimelineResponse> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/${encodeURIComponent(runId)}/timeline`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function fetchRuns(apiBase: string): Promise<RunSummary[]> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

export function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE)
  const [draftRunId, setDraftRunId] = useState('')
  const [draftCompareRunId, setDraftCompareRunId] = useState('')
  const [runId, setRunId] = useState('')
  const [compareRunId, setCompareRunId] = useState('')
  const [selectedStageName, setSelectedStageName] = useState<string | null>(null)

  const timelineQuery = useQuery({
    queryKey: ['timeline', apiBase, runId],
    queryFn: () => fetchTimeline(apiBase, runId),
    enabled: runId.trim().length > 0,
  })
  const compareTimelineQuery = useQuery({
    queryKey: ['timeline-compare', apiBase, compareRunId],
    queryFn: () => fetchTimeline(apiBase, compareRunId),
    enabled: compareRunId.trim().length > 0,
  })
  const runsQuery = useQuery({
    queryKey: ['runs', apiBase],
    queryFn: () => fetchRuns(apiBase),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setRunId(draftRunId.trim())
    setCompareRunId(draftCompareRunId.trim())
  }

  const timeline = timelineQuery.data
  const compareTimeline = compareTimelineQuery.data
  const failedCount = useMemo(
    () =>
      timeline?.lanes.stages.reduce(
        (count, stage) => count + stage.violations.length,
        0,
      ) ?? 0,
    [timeline],
  )
  const compareFailedCount = useMemo(
    () =>
      compareTimeline?.lanes.stages.reduce(
        (count, stage) => count + stage.violations.length,
        0,
      ) ?? 0,
    [compareTimeline],
  )
  const selectedStage =
    timeline?.lanes.stages.find((stage) => stage.stage === selectedStageName) ??
    timeline?.lanes.stages[0]
  const selectedRecording = timeline?.lanes.recordings.find(
    (recording) => recording.stage === selectedStage?.stage,
  )
  const selectedCompareStage = compareTimeline?.lanes.stages.find(
    (stage) => stage.stage === selectedStage?.stage,
  )
  const selectedCompareRecording = compareTimeline?.lanes.recordings.find(
    (recording) => recording.stage === selectedStage?.stage,
  )

  return (
    <main className="appShell">
      <header className="topBar">
        <div>
          <h1>VoxBench</h1>
          <p>
            {timeline ? shortHash(timeline.config_hash) : 'timeline inspector'}
            {compareTimeline ? ` vs ${shortHash(compareTimeline.config_hash)}` : ''}
          </p>
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
            Primary
            <input
              value={draftRunId}
              onChange={(event) => setDraftRunId(event.target.value)}
              placeholder="run_id"
            />
          </label>
          <label>
            Compare
            <input
              value={draftCompareRunId}
              onChange={(event) => setDraftCompareRunId(event.target.value)}
              placeholder="optional run_id"
            />
          </label>
          <button type="submit" title="Fetch timeline">
            <Search size={17} />
            Fetch
          </button>
          <button
            type="button"
            title="Refresh"
            onClick={() => {
              void timelineQuery.refetch()
              void runsQuery.refetch()
              if (compareRunId) {
                void compareTimelineQuery.refetch()
              }
            }}
            disabled={!runId}
          >
            <RefreshCw size={17} />
          </button>
        </form>
      </header>

      <section className="summaryGrid">
        <SummaryTile icon={<Database size={18} />} label="Run" value={timeline?.run_id ?? '-'} />
        <SummaryTile icon={<Database size={18} />} label="Compare" value={compareTimeline?.run_id ?? '-'} />
        <SummaryTile icon={<AlertTriangle size={18} />} label="Violations" value={failedCount} />
        <SummaryTile
          icon={<Clock3 size={18} />}
          label="Compare Fails"
          value={compareTimeline ? compareFailedCount : '-'}
        />
      </section>

      {timelineQuery.isPending && runId ? <StatusPanel state="loading" /> : null}
      {timelineQuery.isError ? <StatusPanel state="error" detail={timelineQuery.error.message} /> : null}
      {compareTimelineQuery.isPending && compareRunId ? (
        <StatusPanel state="loading" detail="Fetching comparison" />
      ) : null}
      {compareTimelineQuery.isError ? (
        <StatusPanel state="error" detail={`Compare: ${compareTimelineQuery.error.message}`} />
      ) : null}
      {!runId ? <StatusPanel state="idle" /> : null}

      {!timeline ? (
        <div className="recentRunsWide">
          <RecentRuns
            compareRunId={compareRunId}
            isError={runsQuery.isError}
            isLoading={runsQuery.isPending}
            onUseCompare={(id) => {
              setDraftCompareRunId(id)
              setCompareRunId(id)
            }}
            onUsePrimary={(id) => {
              setDraftRunId(id)
              setRunId(id)
            }}
            primaryRunId={runId}
            runs={runsQuery.data ?? []}
          />
        </div>
      ) : null}

      {timeline ? (
        <section className="timelineGrid">
          <div className="timelineMain">
            <div className="sectionHeader">
              <Activity size={18} />
              <h2>Stages</h2>
            </div>
            {compareTimeline ? (
              <ComparisonTable primary={timeline} compare={compareTimeline} />
            ) : null}
            <div className="stageStack">
              {timeline.lanes.stages.map((stage) => (
                <StageLane
                  key={stage.stage}
                  onSelect={() => setSelectedStageName(stage.stage)}
                  selected={stage.stage === selectedStage?.stage}
                  stage={stage}
                />
              ))}
            </div>
          </div>

          <aside className="sideRail">
            <LaneStatus title="SIP" icon={<Signal size={17} />} count={timeline.lanes.sip_ladder.length} />
            <LaneStatus title="RTP" icon={<Waves size={17} />} count={timeline.lanes.rtp_quality.length} />
            <LaneStatus title="Turns" icon={<Headphones size={17} />} count={timeline.lanes.turns.length} />
            <LaneStatus title="Host" icon={<Server size={17} />} count={timeline.lanes.host.length} />
            <RecentRuns
              compareRunId={compareRunId}
              isError={runsQuery.isError}
              isLoading={runsQuery.isPending}
              onUseCompare={(id) => {
                setDraftCompareRunId(id)
                setCompareRunId(id)
              }}
              onUsePrimary={(id) => {
                setDraftRunId(id)
                setRunId(id)
              }}
              primaryRunId={runId}
              runs={runsQuery.data ?? []}
            />
            {selectedStage ? (
              <StageDetail
                apiBase={apiBase}
                compareRecording={selectedCompareRecording}
                compareRunId={compareTimeline?.run_id}
                compareStage={selectedCompareStage}
                recording={selectedRecording}
                runId={timeline.run_id}
                stage={selectedStage}
              />
            ) : null}
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

function RecentRuns({
  compareRunId,
  isError,
  isLoading,
  onUseCompare,
  onUsePrimary,
  primaryRunId,
  runs,
}: {
  compareRunId: string
  isError: boolean
  isLoading: boolean
  onUseCompare: (id: string) => void
  onUsePrimary: (id: string) => void
  primaryRunId: string
  runs: RunSummary[]
}) {
  return (
    <section className="recentRuns">
      <div className="sectionHeader compact">
        <Database size={17} />
        <h2>Recent runs</h2>
      </div>
      {isLoading ? <div className="emptyInline">Loading runs</div> : null}
      {isError ? <div className="emptyInline">Run list unavailable</div> : null}
      {!isLoading && !isError && runs.length === 0 ? (
        <div className="emptyInline">No runs in this process</div>
      ) : null}
      <div className="recentRunList">
        {runs.map((run) => (
          <article key={run.run_id} className="recentRunItem">
            <div className="recentRunMeta">
              <strong>{shortId(run.run_id)}</strong>
              <span>{shortHash(run.config_hash)}</span>
              <em>
                {run.recording_count} rec / {run.violation_count} fail
              </em>
            </div>
            <div className="recentRunActions">
              <button
                type="button"
                onClick={() => onUsePrimary(run.run_id)}
                disabled={run.run_id === primaryRunId}
              >
                Primary
              </button>
              <button
                type="button"
                onClick={() => onUseCompare(run.run_id)}
                disabled={run.run_id === compareRunId}
              >
                Compare
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
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
    loading: ['Loading', detail ?? 'Fetching timeline'],
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

function ComparisonTable({
  primary,
  compare,
}: {
  primary: TimelineResponse
  compare: TimelineResponse
}) {
  const compareStages = stageMap(compare)
  return (
    <section className="comparisonPanel">
      <div className="comparisonHeader">
        <div>
          <h3>Run comparison</h3>
          <span>
            {shortHash(primary.config_hash)} vs {shortHash(compare.config_hash)}
          </span>
        </div>
        <span>{compare.lanes.recordings.length} compare recordings</span>
      </div>
      <div className="comparisonTable" role="table">
        <div className="comparisonRow header" role="row">
          <span>Stage</span>
          <span>Primary</span>
          <span>Compare</span>
          <span>Metric deltas</span>
        </div>
        {primary.lanes.stages.map((stage) => {
          const compareStage = compareStages.get(stage.stage)
          return (
            <div className="comparisonRow" key={stage.stage} role="row">
              <strong>{stage.stage}</strong>
              <span>{stage.violations.length} fail</span>
              <span>{compareStage ? `${compareStage.violations.length} fail` : 'missing'}</span>
              <span>{compareStage ? metricDeltas(stage, compareStage) : '-'}</span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function StageLane({
  onSelect,
  selected,
  stage,
}: {
  onSelect: () => void
  selected: boolean
  stage: TimelineStageLane
}) {
  const failed = stage.violations.length > 0
  return (
    <article
      className={`stageLane ${failed ? 'failed' : 'passed'} ${selected ? 'selected' : ''}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
      role="button"
      tabIndex={0}
    >
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

function StageDetail({
  apiBase,
  compareRecording,
  compareRunId,
  compareStage,
  recording,
  runId,
  stage,
}: {
  apiBase: string
  compareRecording?: TimelineRecording
  compareRunId?: string
  compareStage?: TimelineStageLane
  recording?: TimelineRecording
  runId: string
  stage: TimelineStageLane
}) {
  const base = apiBase.replace(/\/$/, '')
  const audioSrc = recording
    ? `${base}/runs/${encodeURIComponent(runId)}/recordings/${encodeURIComponent(recording.stage)}/audio`
    : null
  const compareAudioSrc =
    compareRecording && compareRunId
      ? `${base}/runs/${encodeURIComponent(compareRunId)}/recordings/${encodeURIComponent(compareRecording.stage)}/audio`
      : null
  const hasCompare = compareRunId !== undefined
  const [activePlayerId, setActivePlayerId] = useState<string | null>(null)
  const [activeDetailTab, setActiveDetailTab] = useState<DetailTab>(
    defaultDetailTab(stage, compareStage),
  )
  const primaryPlayerId = `${runId}:${stage.stage}:primary`
  const comparePlayerId = compareRunId ? `${compareRunId}:${stage.stage}:compare` : null

  const activatePlayer = useCallback((playerId: string) => {
    setActivePlayerId(playerId)
  }, [])
  const deactivatePlayer = useCallback((playerId: string) => {
    setActivePlayerId((current) => (current === playerId ? null : current))
  }, [])

  useEffect(() => {
    setActivePlayerId(null)
    setActiveDetailTab(defaultDetailTab(stage, compareStage))
  }, [compareRunId, compareStage, runId, stage])

  return (
    <section className="stageDetail">
      <div className="sectionHeader compact">
        <Activity size={17} />
        <h2>Stage detail</h2>
      </div>
      <div className="detailTitle">
        <strong>{stage.stage}</strong>
        <span>
          {stage.violations.length} fail primary
          {compareStage ? ` / ${compareStage.violations.length} fail compare` : ''}
        </span>
      </div>
      <div className="detailTabs" role="tablist" aria-label="Stage detail">
        <button
          className={detailTabClass(activeDetailTab, 'metrics')}
          onClick={() => setActiveDetailTab('metrics')}
          type="button"
        >
          <BarChart3 size={15} />
          Metrics
        </button>
        <button
          className={detailTabClass(activeDetailTab, 'checks')}
          onClick={() => setActiveDetailTab('checks')}
          type="button"
        >
          <ListChecks size={15} />
          Checks
        </button>
        <button
          className={detailTabClass(activeDetailTab, 'audio')}
          onClick={() => setActiveDetailTab('audio')}
          type="button"
        >
          <Headphones size={15} />
          Audio
        </button>
      </div>
      {activeDetailTab === 'metrics' ? (
        <div className={hasCompare ? 'detailCompareGrid' : 'detailCompareGrid single'}>
          <MetricPanel label="Primary metrics" metrics={stage.metrics} />
          {hasCompare ? (
            compareStage ? (
              <MetricPanel label="Compare metrics" metrics={compareStage.metrics} />
            ) : (
              <MissingComparePanel label="Compare metrics" />
            )
          ) : null}
        </div>
      ) : null}
      {activeDetailTab === 'checks' ? (
        <div className={hasCompare ? 'detailCompareGrid' : 'detailCompareGrid single'}>
          <VerificationPanel label="Primary checks" violations={stage.violations} />
          {hasCompare ? (
            compareStage ? (
              <VerificationPanel label="Compare checks" violations={compareStage.violations} />
            ) : (
              <MissingComparePanel label="Compare checks" />
            )
          ) : null}
        </div>
      ) : null}
      {activeDetailTab === 'audio' ? (
        <div className={hasCompare ? 'detailAudio compare' : 'detailAudio'}>
          <strong>Recording</strong>
          <RecordingWaveform
            activePlayerId={activePlayerId}
            durationMs={recording?.duration_ms}
            label="Primary"
            onActivate={activatePlayer}
            onDeactivate={deactivatePlayer}
            playerId={primaryPlayerId}
            src={audioSrc}
          />
          {hasCompare && comparePlayerId ? (
            <RecordingWaveform
              activePlayerId={activePlayerId}
              durationMs={compareRecording?.duration_ms}
              label="Compare"
              onActivate={activatePlayer}
              onDeactivate={deactivatePlayer}
              playerId={comparePlayerId}
              src={compareAudioSrc}
            />
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function defaultDetailTab(stage: TimelineStageLane, compareStage?: TimelineStageLane): DetailTab {
  if (stage.violations.length > 0 || (compareStage?.violations.length ?? 0) > 0) {
    return 'checks'
  }
  return 'metrics'
}

function detailTabClass(activeTab: DetailTab, tab: DetailTab) {
  return activeTab === tab ? 'detailTab active' : 'detailTab'
}

function MetricPanel({
  label,
  metrics,
}: {
  label: string
  metrics: TimelineMetricPoint[]
}) {
  return (
    <div className="detailPanel">
      <div className="detailPanelHeader">
        <strong>{label}</strong>
        <span>{metrics.length}</span>
      </div>
      <div className="detailMetrics">
        {metrics.map((metric) => (
          <div className="detailMetric" key={`${label}-${metric.name}-${metric.ts}`}>
            <span>{metric.name}</span>
            <strong>{formatNumber(metric.value)}</strong>
          </div>
        ))}
        {metrics.length === 0 ? <div className="emptyInline">No metrics</div> : null}
      </div>
    </div>
  )
}

function VerificationPanel({
  label,
  violations,
}: {
  label: string
  violations: TimelineViolation[]
}) {
  return (
    <div className="detailPanel">
      <div className="detailPanelHeader">
        <strong>{label}</strong>
        <span>{violations.length} fail</span>
      </div>
      {violations.length > 0 ? (
        <div className="detailViolations">
          {violations.map((violation) => (
            <article className="detailViolation" key={`${label}-${violation.invariant}`}>
              <strong>{violation.invariant}</strong>
              <span>{violation.detail}</span>
              <JsonBlock label="Observed" value={violation.observed} />
              <JsonBlock label="Expected" value={violation.expected} />
            </article>
          ))}
        </div>
      ) : (
        <div className="emptyInline">No failed verifications</div>
      )}
    </div>
  )
}

function MissingComparePanel({ label }: { label: string }) {
  return (
    <div className="detailPanel">
      <div className="detailPanelHeader">
        <strong>{label}</strong>
        <span>missing</span>
      </div>
      <div className="emptyInline">No matching compare stage</div>
    </div>
  )
}

function RecordingWaveform({
  activePlayerId,
  durationMs,
  label,
  onActivate,
  onDeactivate,
  playerId,
  src,
}: {
  activePlayerId: string | null
  durationMs?: number
  label: string
  onActivate: (playerId: string) => void
  onDeactivate: (playerId: string) => void
  playerId: string
  src: string | null
}) {
  const isActive = activePlayerId === playerId

  return (
    <div className={isActive ? 'recordingWaveform active' : 'recordingWaveform'}>
      <div className="recordingWaveformHeader">
        <strong>{label}</strong>
        <span>{durationMs !== undefined ? `${formatNumber(durationMs)} ms` : 'missing'}</span>
      </div>
      {src ? (
        <WaveformPlayer
          activePlayerId={activePlayerId}
          onActivate={onActivate}
          onDeactivate={onDeactivate}
          playerId={playerId}
          src={src}
        />
      ) : (
        <div className="emptyInline">No recording</div>
      )}
    </div>
  )
}

function WaveformPlayer({
  activePlayerId,
  onActivate,
  onDeactivate,
  playerId,
  src,
}: {
  activePlayerId: string | null
  onActivate: (playerId: string) => void
  onDeactivate: (playerId: string) => void
  playerId: string
  src: string
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const waveRef = useRef<WaveSurfer | null>(null)
  const [isReady, setIsReady] = useState(false)
  const [isPlaying, setIsPlaying] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    if (!containerRef.current) {
      return
    }

    setIsReady(false)
    setIsPlaying(false)
    setLoadError(null)

    const wave = WaveSurfer.create({
      container: containerRef.current,
      cursorColor: '#c84b52',
      height: 76,
      normalize: true,
      progressColor: '#1f6f78',
      url: src,
      waveColor: '#9eb5c1',
      barGap: 1,
      barWidth: 2,
    })

    waveRef.current = wave
    wave.on('ready', () => setIsReady(true))
    wave.on('play', () => {
      setIsPlaying(true)
      onActivate(playerId)
    })
    wave.on('pause', () => {
      setIsPlaying(false)
      onDeactivate(playerId)
    })
    wave.on('finish', () => {
      setIsPlaying(false)
      onDeactivate(playerId)
    })
    wave.on('error', () => {
      setIsReady(false)
      setIsPlaying(false)
      onDeactivate(playerId)
      setLoadError('Waveform unavailable')
    })

    return () => {
      wave.destroy()
      if (waveRef.current === wave) {
        waveRef.current = null
      }
    }
  }, [onActivate, onDeactivate, playerId, src])

  useEffect(() => {
    if (activePlayerId !== playerId && isPlaying) {
      waveRef.current?.pause()
    }
  }, [activePlayerId, isPlaying, playerId])

  return (
    <div className="waveformPlayer">
      <div className="waveformSurface" ref={containerRef} aria-label="Recording waveform" />
      <div className="waveformControls">
        <button
          className="waveformButton"
          disabled={!isReady}
          onClick={() => {
            if (!isPlaying) {
              onActivate(playerId)
            }
            void waveRef.current?.playPause()
          }}
          title={isPlaying ? 'Pause recording' : 'Play recording'}
          type="button"
        >
          {isPlaying ? <Pause size={16} /> : <Play size={16} />}
          {isPlaying ? 'Pause' : 'Play'}
        </button>
        <span>{loadError ?? (isReady ? 'Waveform ready' : 'Loading waveform')}</span>
      </div>
      <audio className="nativeAudio" controls preload="none" src={src} />
    </div>
  )
}

function JsonBlock({ label, value }: { label: string; value: Record<string, unknown> }) {
  return (
    <details className="jsonBlock">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
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

function shortId(id: string) {
  return id.slice(0, 8)
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

function stageMap(timeline: TimelineResponse) {
  return new Map(timeline.lanes.stages.map((stage) => [stage.stage, stage]))
}

function metricDeltas(primary: TimelineStageLane, compare: TimelineStageLane) {
  const compareMetrics = new Map(compare.metrics.map((metric) => [metric.name, metric.value]))
  const deltas = primary.metrics
    .filter((metric) => compareMetrics.has(metric.name))
    .slice(0, 3)
    .map((metric) => {
      const delta = (compareMetrics.get(metric.name) ?? 0) - metric.value
      return `${metric.name} ${formatSigned(delta)}`
    })

  return deltas.length > 0 ? deltas.join(', ') : 'no shared metrics'
}

function formatSigned(value: number) {
  if (value === 0) return '0'
  const formatted = Number.isInteger(value) ? String(Math.abs(value)) : Math.abs(value).toFixed(3)
  return `${value > 0 ? '+' : '-'}${formatted}`
}
