import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
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
import {
  FormEvent,
  MouseEvent as ReactMouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import WaveSurfer from 'wavesurfer.js'

import type {
  CrossSessionTrend,
  EnvironmentProfile,
  LiveRunStatus,
  ReadinessChecklistItem,
  ReadinessStatus,
  ReadinessSummary,
  RunSummary,
  RunEnvironmentMetadata,
  TimelineMetricPoint,
  TimelineCategory,
  TimelineIncident,
  TimelineRecording,
  TimelineRtpStat,
  TimelineResponse,
  TimelineSipEvent,
  TimelineStageLane,
  TimelineViolation,
  AudioSessionStatus,
  StorageReadiness,
} from './types'

const DEFAULT_API_BASE = '/api'
const DEFAULT_ASYNC_RUN_PAYLOAD = JSON.stringify(
  {
    config_name: 'baseline',
    configs: [],
    manifests: [],
    environment: {
      environment_profile: 'demo',
      server_alias: 'demo-host-a',
      integration_target_alias: 'integration-target-a',
      manual_blockers: [],
      tags: ['phase4'],
      secret_ref_names: [],
    },
    readiness_checklist: [
      {
        item_id: 'ai_phone_setup_complete',
        label: 'AI phone setup complete',
        status: 'unknown',
      },
      {
        item_id: 'connection_route_verified',
        label: 'Connection route verified',
        status: 'unknown',
      },
      {
        item_id: 'host_metrics_enabled',
        label: 'Host metrics enabled',
        status: 'unknown',
      },
    ],
  },
  null,
  2,
)

type DetailTab = 'metrics' | 'checks' | 'audio'
type LiveSocketState = 'connecting' | 'connected' | 'disconnected' | 'error'
type QuickDemoScenario = 'clean' | 'rtp-gap'
type AgcGainParams = {
  target_rms?: number
  max_gain?: number
  noise_floor?: number
}
type AsyncPayloadDraft = {
  environment?: Partial<RunEnvironmentMetadata>
  readiness_checklist?: Array<Partial<ReadinessChecklistItem>>
  [key: string]: unknown
}

type QuickDemoResponse = {
  run_id: string
  status: string
}

const ENVIRONMENT_PROFILES: EnvironmentProfile[] = [
  'local',
  'dev',
  'demo',
  'integration',
  'staging',
]

const INSPECTOR_CATEGORIES: TimelineCategory[] = [
  'conversation',
  'signaling',
  'transport',
  'buffer',
  'pipeline',
  'provider',
  'runtime',
  'session',
]

const READINESS_CONTROLS: Array<Pick<ReadinessChecklistItem, 'item_id' | 'label'>> = [
  { item_id: 'ai_phone_setup_complete', label: 'AI phone setup complete' },
  {
    item_id: 'intermediate_db_environment_registration_complete',
    label: 'Intermediate DB/environment registration complete',
  },
  { item_id: 'connection_route_verified', label: 'Connection route verified' },
  {
    item_id: 'expected_codec_sample_rate_cadence_declared',
    label: 'Expected codec/sample rate/cadence declared',
  },
  { item_id: 'recording_taps_enabled', label: 'Recording taps enabled' },
  { item_id: 'host_metrics_enabled', label: 'Host metrics enabled' },
  { item_id: 'secret_references_present', label: 'Secret references present' },
]

const READINESS_STATUSES: ReadinessStatus[] = ['unknown', 'pass', 'fail']

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

async function fetchLivePreview(apiBase: string): Promise<LiveRunStatus[]> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/live-preview`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function fetchCrossSessionTrends(apiBase: string): Promise<CrossSessionTrend[]> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/cross-session-trends`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function createAsyncRun(apiBase: string, payload: unknown): Promise<LiveRunStatus> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/async`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(`HTTP ${response.status}${detail ? ` ${detail}` : ''}`)
  }
  return response.json()
}

async function createQuickDemo(
  apiBase: string,
  targetRms: number,
  scenario: QuickDemoScenario,
): Promise<QuickDemoResponse> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/live-demo/simulated`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      provider: 'gemini-live',
      scenario,
      dry_run: true,
      duration_ms: 3000,
      input_rms: 1200,
      target_rms: targetRms,
      max_gain: 4,
      noise_floor: 150,
    }),
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function fetchExamplePayload(apiBase: string): Promise<unknown> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/runs/example-payload?environment_profile=demo`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function fetchStorageReadiness(apiBase: string): Promise<StorageReadiness> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/storage/readiness`, { credentials: 'include' })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function fetchAudioSessionStatus(apiBase: string): Promise<AudioSessionStatus> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/auth/remote-audio/session`, {
    credentials: 'include',
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function createAudioSession(
  apiBase: string,
  loginToken: string,
): Promise<AudioSessionStatus> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/auth/remote-audio/session`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ login_token: loginToken }),
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

async function deleteAudioSession(apiBase: string): Promise<AudioSessionStatus> {
  const base = apiBase.replace(/\/$/, '')
  const response = await fetch(`${base}/auth/remote-audio/session`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

function liveWebSocketUrl(apiBase: string) {
  const base = apiBase.replace(/\/$/, '')
  const origin = typeof window === 'undefined' ? 'http://127.0.0.1' : window.location.origin
  const url = new URL(`${base}/live`, origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

function parseAsyncPayloadDraft(payload: string): AsyncPayloadDraft | null {
  try {
    const parsed = JSON.parse(payload) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as AsyncPayloadDraft
    }
    return null
  } catch {
    return null
  }
}

function fallbackAsyncPayloadDraft(payload: string): AsyncPayloadDraft {
  return parseAsyncPayloadDraft(payload) ?? parseAsyncPayloadDraft(DEFAULT_ASYNC_RUN_PAYLOAD) ?? {}
}

function formatAsyncPayloadDraft(draft: AsyncPayloadDraft) {
  return JSON.stringify(draft, null, 2)
}

function updatePayloadEnvironment(
  payload: string,
  updates: Partial<RunEnvironmentMetadata>,
) {
  const draft = fallbackAsyncPayloadDraft(payload)
  const environment = {
    ...(draft.environment ?? {}),
    ...updates,
  }
  return formatAsyncPayloadDraft({ ...draft, environment })
}

function updatePayloadReadiness(
  payload: string,
  itemId: string,
  status: ReadinessStatus,
) {
  const draft = fallbackAsyncPayloadDraft(payload)
  const currentItems = Array.isArray(draft.readiness_checklist)
    ? draft.readiness_checklist
    : []
  const existing = new Map(currentItems.map((item) => [item.item_id, item]))
  const control = READINESS_CONTROLS.find((item) => item.item_id === itemId)
  existing.set(itemId, {
    item_id: itemId,
    label: control?.label ?? itemId,
    ...(existing.get(itemId) ?? {}),
    status,
  })
  const readiness_checklist = READINESS_CONTROLS.map((item) => (
    existing.get(item.item_id) ?? {
      item_id: item.item_id,
      label: item.label,
      status: 'unknown' as ReadinessStatus,
    }
  ))
  return formatAsyncPayloadDraft({ ...draft, readiness_checklist })
}

function updatePayloadAgcParams(payload: string, updates: AgcGainParams) {
  const draft = fallbackAsyncPayloadDraft(payload)
  const configs = Array.isArray(draft.configs) ? draft.configs : []
  let updated = false
  const nextConfigs = configs.map((config) => {
    if (!isRecord(config) || !isRecord(config.spec)) {
      return config
    }
    const media = config.spec.media
    if (!isRecord(media) || !Array.isArray(media.pipeline)) {
      return config
    }
    const nextPipeline = media.pipeline.map((stage) => {
      if (!isRecord(stage) || stage.type !== 'agc') {
        return stage
      }
      updated = true
      const params = isRecord(stage.params) ? stage.params : {}
      return {
        ...stage,
        params: {
          ...params,
          ...updates,
        },
      }
    })
    return {
      ...config,
      spec: {
        ...config.spec,
        media: {
          ...media,
          pipeline: nextPipeline,
        },
      },
    }
  })
  return updated ? formatAsyncPayloadDraft({ ...draft, configs: nextConfigs }) : payload
}

function agcGainParamsFromDraft(draft: AsyncPayloadDraft): AgcGainParams {
  const configs = Array.isArray(draft.configs) ? draft.configs : []
  for (const config of configs) {
    if (!isRecord(config) || !isRecord(config.spec)) {
      continue
    }
    const media = config.spec.media
    if (!isRecord(media) || !Array.isArray(media.pipeline)) {
      continue
    }
    for (const stage of media.pipeline) {
      if (!isRecord(stage) || stage.type !== 'agc' || !isRecord(stage.params)) {
        continue
      }
      return {
        target_rms: numberParam(stage.params.target_rms),
        max_gain: numberParam(stage.params.max_gain),
        noise_floor: numberParam(stage.params.noise_floor),
      }
    }
  }
  return {}
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function numberParam(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function numberInput(value: number | undefined) {
  return value === undefined ? '' : String(value)
}

function parseNumberInput(value: string) {
  if (value.trim() === '') {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function commaList(value: string) {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function timelineQueryParameter(name: 'run_id' | 'compare_run_id') {
  return new URLSearchParams(window.location.search).get(name)?.trim() ?? ''
}

export function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE)
  const [draftRunId, setDraftRunId] = useState(() => timelineQueryParameter('run_id'))
  const [draftCompareRunId, setDraftCompareRunId] = useState(() =>
    timelineQueryParameter('compare_run_id'),
  )
  const [runId, setRunId] = useState(() => timelineQueryParameter('run_id'))
  const [compareRunId, setCompareRunId] = useState(() =>
    timelineQueryParameter('compare_run_id'),
  )
  const [selectedStageName, setSelectedStageName] = useState<string | null>(null)
  const [livePreviewSocketRuns, setLivePreviewSocketRuns] = useState<LiveRunStatus[] | null>(null)
  const [liveSocketState, setLiveSocketState] = useState<LiveSocketState>('connecting')
  const [asyncRunPayload, setAsyncRunPayload] = useState(DEFAULT_ASYNC_RUN_PAYLOAD)
  const [asyncRunError, setAsyncRunError] = useState<string | null>(null)
  const [asyncRunPending, setAsyncRunPending] = useState(false)
  const [examplePayloadPending, setExamplePayloadPending] = useState(false)
  const [audioLoginToken, setAudioLoginToken] = useState('')
  const [audioSessionError, setAudioSessionError] = useState<string | null>(null)
  const [audioSessionPending, setAudioSessionPending] = useState(false)
  const [audioSessionRevision, setAudioSessionRevision] = useState(0)
  const [quickDemoPending, setQuickDemoPending] = useState(false)
  const [quickDemoError, setQuickDemoError] = useState<string | null>(null)
  const [quickDemoTargetRms, setQuickDemoTargetRms] = useState(1600)
  const [quickDemoScenario, setQuickDemoScenario] = useState<QuickDemoScenario>('rtp-gap')
  const stageDetailRef = useRef<HTMLDivElement>(null)

  const timelineQuery = useQuery({
    queryKey: ['timeline', apiBase, runId],
    queryFn: () => fetchTimeline(apiBase, runId),
    enabled: runId.trim().length > 0,
    refetchInterval: (query) =>
      query.state.data && query.state.data.lanes.recordings.length === 0 ? 1_000 : false,
  })
  const compareTimelineQuery = useQuery({
    queryKey: ['timeline-compare', apiBase, compareRunId],
    queryFn: () => fetchTimeline(apiBase, compareRunId),
    enabled: compareRunId.trim().length > 0,
  })
  const runsQuery = useQuery({
    queryKey: ['runs', apiBase],
    queryFn: () => fetchRuns(apiBase),
    refetchInterval: 2_000,
  })
  const livePreviewQuery = useQuery({
    queryKey: ['live-preview', apiBase],
    queryFn: () => fetchLivePreview(apiBase),
    refetchInterval: 2_000,
  })
  const crossSessionTrendsQuery = useQuery({
    queryKey: ['cross-session-trends', apiBase],
    queryFn: () => fetchCrossSessionTrends(apiBase),
    refetchInterval: 5_000,
  })
  const storageReadinessQuery = useQuery({
    queryKey: ['storage-readiness', apiBase],
    queryFn: () => fetchStorageReadiness(apiBase),
  })
  const audioSessionStatusQuery = useQuery({
    queryKey: ['remote-audio-session', apiBase],
    queryFn: () => fetchAudioSessionStatus(apiBase),
    enabled: storageReadinessQuery.data?.web_audio_session_enabled === true,
  })

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (runId) {
      params.set('run_id', runId)
    } else {
      params.delete('run_id')
    }
    if (compareRunId) {
      params.set('compare_run_id', compareRunId)
    } else {
      params.delete('compare_run_id')
    }
    const query = params.toString()
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`,
    )
  }, [compareRunId, runId])

  useEffect(() => {
    const url = liveWebSocketUrl(apiBase)
    setLiveSocketState('connecting')
    setLivePreviewSocketRuns(null)
    const socket = new WebSocket(url)

    socket.addEventListener('open', () => {
      setLiveSocketState('connected')
    })
    socket.addEventListener('message', (event) => {
      try {
        setLivePreviewSocketRuns(JSON.parse(event.data) as LiveRunStatus[])
      } catch {
        setLiveSocketState('error')
      }
    })
    socket.addEventListener('error', () => {
      setLiveSocketState('error')
      setLivePreviewSocketRuns(null)
    })
    socket.addEventListener('close', () => {
      setLiveSocketState((current) => (current === 'error' ? 'error' : 'disconnected'))
      setLivePreviewSocketRuns(null)
    })

    return () => {
      socket.close()
    }
  }, [apiBase])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const nextRunId = draftRunId.trim()
    const nextCompareRunId = draftCompareRunId.trim()
    const sameRun = nextRunId === runId
    const sameCompareRun = nextCompareRunId === compareRunId
    setRunId(nextRunId)
    setCompareRunId(nextCompareRunId)
    setLivePreviewSocketRuns(null)
    if (sameRun && nextRunId) {
      void timelineQuery.refetch()
    }
    if (sameCompareRun && nextCompareRunId) {
      void compareTimelineQuery.refetch()
    }
    void runsQuery.refetch()
    void livePreviewQuery.refetch()
  }

  async function submitAsyncRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setAsyncRunPending(true)
    setAsyncRunError(null)
    try {
      const payload = JSON.parse(asyncRunPayload) as unknown
      const accepted = await createAsyncRun(apiBase, payload)
      setDraftRunId(accepted.run_id)
      setRunId(accepted.run_id)
      setSelectedStageName(null)
      setLivePreviewSocketRuns(null)
      void runsQuery.refetch()
      void livePreviewQuery.refetch()
      void crossSessionTrendsQuery.refetch()
    } catch (error) {
      setAsyncRunError(error instanceof Error ? error.message : 'Async run failed')
    } finally {
      setAsyncRunPending(false)
    }
  }

  async function runQuickDemo() {
    setQuickDemoPending(true)
    setQuickDemoError(null)
    try {
      const completed = await createQuickDemo(
        apiBase,
        quickDemoTargetRms,
        quickDemoScenario,
      )
      setDraftRunId(completed.run_id)
      setRunId(completed.run_id)
      setSelectedStageName(null)
      setLivePreviewSocketRuns(null)
      void runsQuery.refetch()
      void livePreviewQuery.refetch()
      void crossSessionTrendsQuery.refetch()
    } catch (error) {
      setQuickDemoError(error instanceof Error ? error.message : 'Audible demo failed')
    } finally {
      setQuickDemoPending(false)
    }
  }

  function selectStage(stageName: string) {
    setSelectedStageName(stageName)
    window.requestAnimationFrame(() => {
      stageDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }

  async function loadExamplePayload() {
    setExamplePayloadPending(true)
    setAsyncRunError(null)
    try {
      const payload = await fetchExamplePayload(apiBase)
      setAsyncRunPayload(JSON.stringify(payload, null, 2))
    } catch (error) {
      setAsyncRunError(error instanceof Error ? error.message : 'Example payload unavailable')
    } finally {
      setExamplePayloadPending(false)
    }
  }

  async function submitAudioSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setAudioSessionPending(true)
    setAudioSessionError(null)
    try {
      await createAudioSession(apiBase, audioLoginToken)
      setAudioLoginToken('')
      await audioSessionStatusQuery.refetch()
      setAudioSessionRevision((current) => current + 1)
    } catch (error) {
      setAudioSessionError(error instanceof Error ? error.message : 'Audio login failed')
    } finally {
      setAudioSessionPending(false)
    }
  }

  async function logoutAudioSession() {
    setAudioSessionPending(true)
    setAudioSessionError(null)
    try {
      await deleteAudioSession(apiBase)
      await audioSessionStatusQuery.refetch()
      setAudioSessionRevision((current) => current + 1)
    } catch (error) {
      setAudioSessionError(error instanceof Error ? error.message : 'Audio logout failed')
    } finally {
      setAudioSessionPending(false)
    }
  }

  function updateAsyncEnvironment(updates: Partial<RunEnvironmentMetadata>) {
    setAsyncRunPayload((current) => updatePayloadEnvironment(current, updates))
  }

  function updateAsyncReadiness(itemId: string, status: ReadinessStatus) {
    setAsyncRunPayload((current) => updatePayloadReadiness(current, itemId, status))
  }

  function updateAsyncAgcParams(updates: AgcGainParams) {
    setAsyncRunPayload((current) => updatePayloadAgcParams(current, updates))
  }

  const timeline = timelineQuery.data
  const compareTimeline = compareTimelineQuery.data
  const livePreviewRuns = livePreviewSocketRuns ?? livePreviewQuery.data ?? []
  const asyncPayloadDraft = useMemo(
    () => fallbackAsyncPayloadDraft(asyncRunPayload),
    [asyncRunPayload],
  )
  const asyncEnvironment = asyncPayloadDraft.environment ?? {}
  const asyncAgcParams = useMemo(
    () => agcGainParamsFromDraft(asyncPayloadDraft),
    [asyncPayloadDraft],
  )
  const failedCount = useMemo(
    () =>
      timeline?.lanes.stages.reduce(
        (count, stage) => count + stage.violations.length,
        0,
      ) ?? 0,
    [timeline],
  )
  const compareStages = useMemo(
    () => (compareTimeline ? stageMap(compareTimeline) : null),
    [compareTimeline],
  )
  const selectedStage =
    timeline?.lanes.stages.find((stage) => stage.stage === selectedStageName) ??
    timeline?.lanes.stages[0]
  const selectedRecording = timeline?.lanes.recordings.find(
    (recording) => recording.stage === selectedStage?.stage,
  )
  const selectedCompareStage = selectedStage ? compareStages?.get(selectedStage.stage) : undefined
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
              setLivePreviewSocketRuns(null)
              void timelineQuery.refetch()
              void runsQuery.refetch()
              void livePreviewQuery.refetch()
              void crossSessionTrendsQuery.refetch()
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

      <QuickDemoPanel
        error={quickDemoError}
        onRun={runQuickDemo}
        onScenarioChange={setQuickDemoScenario}
        onTargetRmsChange={setQuickDemoTargetRms}
        pending={quickDemoPending}
        scenario={quickDemoScenario}
        targetRms={quickDemoTargetRms}
      />

      <section className="summaryGrid">
        <SummaryTile icon={<Database size={18} />} label="Run" value={timeline?.run_id ?? '-'} />
        <SummaryTile icon={<Database size={18} />} label="Compare" value={compareTimeline?.run_id ?? '-'} />
        <SummaryTile
          icon={<Server size={18} />}
          label="Environment"
          value={timeline ? environmentHeadline(timeline.environment) : '-'}
        />
        <SummaryTile icon={<AlertTriangle size={18} />} label="Violations" value={failedCount} />
        <SummaryTile
          icon={<ListChecks size={18} />}
          label="Readiness"
          value={timeline ? readinessProductHeadline(timeline.readiness_summary) : '-'}
        />
      </section>

      {storageReadinessQuery.data?.remote_audio_proxy_enabled ? (
        <AudioSessionPanel
          error={audioSessionError}
          loginToken={audioLoginToken}
          onLoginTokenChange={setAudioLoginToken}
          onLogout={logoutAudioSession}
          onSubmit={submitAudioSession}
          pending={audioSessionPending}
          readiness={storageReadinessQuery.data}
          status={audioSessionStatusQuery.data}
        />
      ) : null}

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
          <details className="collapsiblePanel">
            <summary>Custom run configuration</summary>
            <AsyncRunPanel
              agcParams={asyncAgcParams}
              environment={asyncEnvironment}
              error={asyncRunError}
              examplePending={examplePayloadPending}
              onAgcParamsChange={updateAsyncAgcParams}
              onEnvironmentChange={updateAsyncEnvironment}
              onLoadExample={loadExamplePayload}
              onPayloadChange={setAsyncRunPayload}
              onReadinessChange={updateAsyncReadiness}
              onSubmit={submitAsyncRun}
              payload={asyncRunPayload}
              pending={asyncRunPending}
              readinessChecklist={asyncPayloadDraft.readiness_checklist ?? []}
            />
          </details>
          <details className="collapsiblePanel">
            <summary>Live status and diagnostics</summary>
            <div className="collapsiblePanelBody">
              <LivePreviewPanel
                connectionState={liveSocketState}
                crossSessionError={crossSessionTrendsQuery.isError}
                crossSessionLoading={crossSessionTrendsQuery.isPending}
                crossSessionTrends={crossSessionTrendsQuery.data ?? []}
                isError={livePreviewQuery.isError}
                isLoading={livePreviewSocketRuns === null && livePreviewQuery.isPending}
                runs={livePreviewRuns}
              />
            </div>
          </details>
        </div>
      ) : null}

      {timeline ? (
        <section className="timelineGrid">
          <div className="timelineMain">
            {compareTimeline ? (
              <ComparisonTable primary={timeline} compare={compareTimeline} />
            ) : null}
            <LinkedCallInspector
              apiBase={apiBase}
              audioSessionRevision={audioSessionRevision}
              runId={timeline.run_id}
              timeline={timeline}
            />
            <Recordings
              apiBase={apiBase}
              audioSessionRevision={audioSessionRevision}
              recordings={timeline.lanes.recordings}
              runId={timeline.run_id}
            />
            <div className="sectionHeader">
              <Activity size={18} />
              <h2>Stages</h2>
            </div>
            <div className="stageStack">
              {timeline.lanes.stages.map((stage) => (
                <StageLane
                  compareEnabled={compareTimeline !== undefined}
                  compareStage={compareStages?.get(stage.stage)}
                  key={stage.stage}
                  onSelect={() => selectStage(stage.stage)}
                  selected={stage.stage === selectedStage?.stage}
                  stage={stage}
                />
              ))}
            </div>
            {selectedStage ? (
              <div ref={stageDetailRef}>
                <StageDetail
                  apiBase={apiBase}
                  audioSessionRevision={audioSessionRevision}
                  compareRecording={selectedCompareRecording}
                  compareRunId={compareTimeline?.run_id}
                  compareStage={selectedCompareStage}
                  recording={selectedRecording}
                  runId={timeline.run_id}
                  stage={selectedStage}
                />
              </div>
            ) : null}
          </div>

          <aside className="sideRail">
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
            <details className="collapsiblePanel">
              <summary>Custom run configuration</summary>
              <AsyncRunPanel
                agcParams={asyncAgcParams}
                environment={asyncEnvironment}
                error={asyncRunError}
                examplePending={examplePayloadPending}
                onAgcParamsChange={updateAsyncAgcParams}
                onEnvironmentChange={updateAsyncEnvironment}
                onLoadExample={loadExamplePayload}
                onPayloadChange={setAsyncRunPayload}
                onReadinessChange={updateAsyncReadiness}
                onSubmit={submitAsyncRun}
                payload={asyncRunPayload}
                pending={asyncRunPending}
                readinessChecklist={asyncPayloadDraft.readiness_checklist ?? []}
              />
            </details>
            <details className="collapsiblePanel">
              <summary>Advanced diagnostics</summary>
              <div className="collapsiblePanelBody">
                <LivePreviewPanel
                  connectionState={liveSocketState}
                  crossSessionError={crossSessionTrendsQuery.isError}
                  crossSessionLoading={crossSessionTrendsQuery.isPending}
                  crossSessionTrends={crossSessionTrendsQuery.data ?? []}
                  isError={livePreviewQuery.isError}
                  isLoading={livePreviewSocketRuns === null && livePreviewQuery.isPending}
                  runs={livePreviewRuns}
                />
                <HostMetricsPanel metrics={timeline.lanes.host} />
                <SipLadderPanel events={timeline.lanes.sip_ladder} />
                <RtpQualityPanel stats={timeline.lanes.rtp_quality} />
                <EnvironmentPanel environment={timeline.environment} />
                <ReadinessPanel
                  checklist={timeline.readiness_checklist}
                  environment={timeline.environment}
                  summary={timeline.readiness_summary}
                />
                <LaneStatus
                  title="Turns"
                  icon={<Headphones size={17} />}
                  count={timeline.lanes.turns.length}
                />
              </div>
            </details>
          </aside>
        </section>
      ) : null}
    </main>
  )
}

function BargeInPacketProof({ incident }: { incident: TimelineIncident }) {
  if (incident.rule_id !== 'barge_in_sequence') {
    return null
  }
  const observedNumber = (name: string) => {
    const value = incident.observed[name]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  const signalMs = observedNumber('discarded_signal_bearing_audio_ms')
  if (signalMs === null) {
    return (
      <div className="bargeInPacketProof unobserved">
        <strong>Packet proof not captured</strong>
        <span>Re-run with the instrumented realtime AudioSocket bridge.</span>
      </div>
    )
  }
  const chunks30 = observedNumber('provider_chunks_last_30ms')
  const discardedMs =
    observedNumber('discarded_total_audio_ms') ??
    observedNumber('discarded_audio_ms')
  const leadMs = observedNumber('first_discarded_audio_lead_ms')
  const writtenMs = observedNumber('written_audio_ms_before_control')
  return (
    <div className="bargeInPacketProof">
      <strong>Local packet proof</strong>
      <dl>
        <div>
          <dt>Provider burst</dt>
          <dd>{chunks30 === null ? '-' : `${displayValue(chunks30)} chunks / 30 ms`}</dd>
        </div>
        <div>
          <dt>Discarded signal</dt>
          <dd>
            {displayValue(signalMs)} / {displayValue(discardedMs)} ms
          </dd>
        </div>
        <div>
          <dt>First arrival lead</dt>
          <dd>{leadMs === null ? '-' : `${displayValue(leadMs)} ms`}</dd>
        </div>
        <div>
          <dt>Written before control</dt>
          <dd>{writtenMs === null ? '-' : `${displayValue(writtenMs)} ms`}</dd>
        </div>
      </dl>
      <span>
        Provider chunks and local queue disposal are correlated. Caller-side playout still
        requires a phone recording.
      </span>
    </div>
  )
}

function LinkedCallInspector({
  apiBase,
  audioSessionRevision,
  runId,
  timeline,
}: {
  apiBase: string
  audioSessionRevision: number
  runId: string
  timeline: TimelineResponse
}) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const audioArtifacts = timeline.lanes.artifacts.filter(
    (artifact) => artifact.kind === 'audio' && artifact.stage,
  )
  const [cursorMs, setCursorMs] = useState(0)
  const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(
    timeline.lanes.incidents[0]?.incident_id ?? null,
  )
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(
    audioArtifacts[0]?.artifact_id ?? null,
  )

  const durationMs = inspectorDurationMs(timeline)
  const selectedIncident =
    timeline.lanes.incidents.find(
      (incident) => incident.incident_id === selectedIncidentId,
    ) ?? null
  const selectedEvidenceEvents = selectedIncident
    ? timeline.lanes.events.filter((event) =>
        selectedIncident.evidence_refs.includes(event.event_id),
      )
    : []
  const selectedArtifact =
    audioArtifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ??
    audioArtifacts[0]
  const visibleCategories = INSPECTOR_CATEGORIES.filter(
    (category) => category === 'conversation' || inspectorCategoryCount(timeline, category) > 0,
  )
  const base = apiBase.replace(/\/$/, '')
  const audioSrc = selectedArtifact?.stage
    ? `${base}/runs/${encodeURIComponent(runId)}/recordings/${encodeURIComponent(selectedArtifact.stage)}/audio`
    : null

  useEffect(() => {
    setCursorMs(0)
    setSelectedIncidentId(timeline.lanes.incidents[0]?.incident_id ?? null)
    setSelectedArtifactId(audioArtifacts[0]?.artifact_id ?? null)
  }, [runId])

  function moveCursor(nextMs: number, seekAudio = true) {
    const bounded = Math.max(0, Math.min(durationMs, nextMs))
    setCursorMs(bounded)
    if (seekAudio && audioRef.current) {
      const audioDurationMs = Number.isFinite(audioRef.current.duration)
        ? audioRef.current.duration * 1000
        : durationMs
      audioRef.current.currentTime = Math.min(bounded, audioDurationMs) / 1000
    }
  }

  function moveCursorFromTrack(event: ReactMouseEvent<HTMLDivElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = bounds.width > 0 ? (event.clientX - bounds.left) / bounds.width : 0
    moveCursor(ratio * durationMs)
  }

  function selectIncident(incident: TimelineIncident) {
    setSelectedIncidentId(incident.incident_id)
    moveCursor(incident.start_ms)
  }

  return (
    <section className="linkedInspector">
      <div className="sectionHeader compact">
        <Activity size={17} />
        <h2>Call inspector</h2>
        <span className="inspectorCursorReadout">Cursor {formatTimelineTime(cursorMs)}</span>
      </div>
      <p className="panelLead">
        One cursor links call events, transport, pipeline evidence, provider lifecycle, and host state.
      </p>

      <div className="incidentStrip" aria-label="Detected incidents">
        {timeline.lanes.incidents.length > 0 ? (
          timeline.lanes.incidents.map((incident) => (
            <button
              className={`incidentChip ${incident.severity} ${incident.incident_id === selectedIncidentId ? 'selected' : ''}`}
              key={incident.incident_id}
              onClick={() => selectIncident(incident)}
              type="button"
            >
              <AlertTriangle size={14} />
              <span>{incident.title}</span>
              <em>{formatTimelineTime(incident.start_ms)}</em>
            </button>
          ))
        ) : (
          <div className="inspectorHealthy">
            No detected incidents. Missing lanes remain explicitly marked as not observed.
          </div>
        )}
      </div>

      <div className="inspectorTimeline">
        <div className="inspectorRulerRow">
          <div className="inspectorLaneLabel">Shared time</div>
          <div className="inspectorRuler">
            {[0, 0.25, 0.5, 0.75, 1].map((ratio) => (
              <span key={ratio} style={{ left: `${ratio * 100}%` }}>
                {formatTimelineTime(durationMs * ratio)}
              </span>
            ))}
          </div>
        </div>

        {visibleCategories.map((category) => {
          const events = timeline.lanes.events.filter((event) => event.category === category)
          const intervals = timeline.lanes.intervals.filter(
            (interval) => interval.category === category,
          )
          const series = timeline.lanes.series.filter((item) => item.category === category)
          const artifacts = timeline.lanes.artifacts.filter(
            (artifact) => artifact.category === category,
          )
          const incidents = timeline.lanes.incidents.filter(
            (incident) => incident.category === category,
          )
          const observed = events.length + intervals.length + series.length + artifacts.length > 0
          const seriesPoints = sampleInspectorPoints(series, 100)
          const artifactDuration = Math.max(
            0,
            ...artifacts.map((artifact) => artifact.duration_ms ?? 0),
          )
          return (
            <div className="inspectorLane" key={category}>
              <div className="inspectorLaneLabel">
                <strong>{timelineCategoryLabel(category)}</strong>
                <span>
                  {observed ? `${inspectorCategoryCount(timeline, category)} evidence` : 'not observed'}
                </span>
              </div>
              <div
                className={`inspectorTrack ${observed ? '' : 'unobserved'}`}
                onClick={moveCursorFromTrack}
              >
                {artifactDuration > 0 ? (
                  <span
                    className="inspectorArtifactBar"
                    style={{ width: `${timePositionPercent(artifactDuration, durationMs)}%` }}
                    title={`${artifacts.length} artifacts · ${formatTimelineTime(artifactDuration)}`}
                  />
                ) : null}
                {intervals.map((interval) => (
                  <span
                    className="inspectorIntervalBar"
                    key={interval.interval_id}
                    style={{
                      left: `${timePositionPercent(interval.start_ms, durationMs)}%`,
                      width: `${Math.max(0.4, timePositionPercent(interval.end_ms - interval.start_ms, durationMs))}%`,
                    }}
                    title={`${interval.name} · ${formatTimelineTime(interval.start_ms)}–${formatTimelineTime(interval.end_ms)}`}
                  />
                ))}
                {seriesPoints.map(({ point, series: pointSeries }, index) => (
                  <button
                    aria-label={`${pointSeries.name} ${displayValue(point.value)} ${pointSeries.unit ?? ''} at ${formatTimelineTime(point.t_rel_ms)}`}
                    className="inspectorSeriesPoint"
                    key={`${pointSeries.series_id}-${index}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      moveCursor(point.t_rel_ms)
                    }}
                    style={{ left: `${timePositionPercent(point.t_rel_ms, durationMs)}%` }}
                    title={`${pointSeries.name}: ${displayValue(point.value)} ${pointSeries.unit ?? ''}`}
                    type="button"
                  />
                ))}
                {events.map((event) => (
                  <button
                    aria-label={`${event.name} at ${formatTimelineTime(event.t_rel_ms)}`}
                    className="inspectorEventMarker"
                    key={event.event_id}
                    onClick={(clickEvent) => {
                      clickEvent.stopPropagation()
                      moveCursor(event.t_rel_ms)
                    }}
                    style={{ left: `${timePositionPercent(event.t_rel_ms, durationMs)}%` }}
                    title={`${event.name} · ${event.direction ?? event.stage ?? event.source}`}
                    type="button"
                  />
                ))}
                {incidents.map((incident) => (
                  <button
                    aria-label={`${incident.title} incident`}
                    className={`inspectorIncidentMarker ${incident.severity}`}
                    key={incident.incident_id}
                    onClick={(clickEvent) => {
                      clickEvent.stopPropagation()
                      selectIncident(incident)
                    }}
                    style={{ left: `${timePositionPercent(incident.start_ms, durationMs)}%` }}
                    title={incident.summary}
                    type="button"
                  />
                ))}
                <span
                  className="inspectorCursor"
                  style={{ left: `${timePositionPercent(cursorMs, durationMs)}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>

      <div className="inspectorControls">
        <label>
          Shared cursor
          <input
            aria-label="Shared timeline cursor"
            max={durationMs}
            min={0}
            onChange={(event) => moveCursor(Number(event.target.value))}
            step={10}
            type="range"
            value={cursorMs}
          />
        </label>
        {audioArtifacts.length > 0 ? (
          <div className="inspectorAudioControl">
            <label>
              Listen at cursor
              <select
                onChange={(event) => setSelectedArtifactId(event.target.value)}
                value={selectedArtifact?.artifact_id ?? ''}
              >
                {audioArtifacts.map((artifact) => (
                  <option key={artifact.artifact_id} value={artifact.artifact_id}>
                    {artifact.stage}
                  </option>
                ))}
              </select>
            </label>
            {audioSrc ? (
              <audio
                controls
                crossOrigin="use-credentials"
                key={`${selectedArtifact?.artifact_id}-${audioSessionRevision}`}
                onLoadedMetadata={() => moveCursor(cursorMs)}
                onTimeUpdate={(event) => setCursorMs(event.currentTarget.currentTime * 1000)}
                preload="metadata"
                ref={audioRef}
                src={audioSrc}
              />
            ) : null}
          </div>
        ) : null}
      </div>

      {selectedIncident ? (
        <div className="incidentEvidence">
          <div>
            <span className={`incidentSeverity ${selectedIncident.severity}`}>
              {selectedIncident.severity}
            </span>
            <strong>{selectedIncident.title}</strong>
            <p>{selectedIncident.summary}</p>
            <small>
              {selectedIncident.stage ?? selectedIncident.direction ?? selectedIncident.category}
              {' · '}
              confidence {selectedIncident.confidence}
              {' · '}
              {formatTimelineTime(selectedIncident.start_ms)}–
              {formatTimelineTime(selectedIncident.end_ms)}
            </small>
            <BargeInPacketProof incident={selectedIncident} />
            {selectedEvidenceEvents.length > 0 ? (
              <ol className="incidentEvidenceChain" aria-label="Correlated evidence chain">
                {selectedEvidenceEvents.map((event) => (
                  <li key={event.event_id}>
                    <button onClick={() => moveCursor(event.t_rel_ms)} type="button">
                      <span>{event.name.replaceAll('_', ' ')}</span>
                      <time>{formatTimelineTime(event.t_rel_ms)}</time>
                    </button>
                  </li>
                ))}
              </ol>
            ) : null}
          </div>
          <JsonBlock label="Observed evidence" value={selectedIncident.observed} />
          <JsonBlock label="Expected contract" value={selectedIncident.expected} />
        </div>
      ) : null}
    </section>
  )
}

function QuickDemoPanel({
  error,
  onRun,
  onScenarioChange,
  onTargetRmsChange,
  pending,
  scenario,
  targetRms,
}: {
  error: string | null
  onRun: () => void
  onScenarioChange: (value: QuickDemoScenario) => void
  onTargetRmsChange: (value: number) => void
  pending: boolean
  scenario: QuickDemoScenario
  targetRms: number
}) {
  return (
    <section className="quickDemoPanel">
      <div className="quickDemoCopy">
        <strong>Diagnose a call in 3 seconds</strong>
        <span>
          Generate a safe local call, hear every stage, and inspect correlated evidence.
        </span>
      </div>
      <div className="quickDemoActions">
        <span>3-second synthetic call · no provider key or Asterisk required</span>
        <label>
          Call condition
          <select
            disabled={pending}
            onChange={(event) => onScenarioChange(event.target.value as QuickDemoScenario)}
            value={scenario}
          >
            <option value="rtp-gap">RTP gap + arrival stall</option>
            <option value="clean">Clean RTP cadence</option>
          </select>
        </label>
        <label>
          Target loudness
          <select
            disabled={pending}
            onChange={(event) => onTargetRmsChange(Number(event.target.value))}
            value={targetRms}
          >
            <option value={1200}>Original · 0 dB</option>
            <option value={1600}>Balanced · +2.5 dB</option>
            <option value={2400}>Boosted · +6 dB</option>
          </select>
        </label>
        <button disabled={pending} onClick={onRun} type="button">
          <Play size={16} />
          {pending ? 'Running demo' : 'Run diagnostic demo'}
        </button>
      </div>
      {error ? <div className="quickDemoError">{error}</div> : null}
    </section>
  )
}

function AudioSessionPanel({
  error,
  loginToken,
  onLoginTokenChange,
  onLogout,
  onSubmit,
  pending,
  readiness,
  status,
}: {
  error: string | null
  loginToken: string
  onLoginTokenChange: (value: string) => void
  onLogout: () => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  pending: boolean
  readiness: StorageReadiness
  status?: AudioSessionStatus
}) {
  const authenticated = status?.authenticated === true
  return (
    <section className={authenticated ? 'audioSessionPanel authenticated' : 'audioSessionPanel'}>
      <div className="audioSessionCopy">
        <strong>Remote audio access</strong>
        <span>
          {readiness.web_audio_session_enabled
            ? authenticated
              ? `Session unlocked${status?.expires_in_seconds ? ` · ${status.expires_in_seconds}s remaining` : ''}`
              : 'Enter the operator login token to create a short-lived HttpOnly session.'
            : 'Remote proxy is server-to-server only; Web session login is disabled.'}
        </span>
        {readiness.web_audio_session_enabled && readiness.web_audio_cookie_secure === false ? (
          <small>Development cookie mode is active. Use Secure cookies in production.</small>
        ) : null}
      </div>
      {readiness.web_audio_session_enabled ? (
        authenticated ? (
          <button disabled={pending} onClick={onLogout} type="button">
            Lock audio
          </button>
        ) : (
          <form className="audioSessionForm" onSubmit={onSubmit}>
            <input
              aria-label="Remote audio operator login token"
              autoComplete="off"
              maxLength={256}
              minLength={32}
              onChange={(event) => onLoginTokenChange(event.target.value)}
              placeholder="operator login token"
              required
              type="password"
              value={loginToken}
            />
            <button disabled={pending || loginToken.length < 32} type="submit">
              {pending ? 'Unlocking…' : 'Unlock audio'}
            </button>
          </form>
        )
      ) : null}
      {error ? <em>{error}</em> : null}
    </section>
  )
}

function AsyncRunPanel({
  agcParams,
  environment,
  error,
  examplePending,
  onAgcParamsChange,
  onEnvironmentChange,
  onLoadExample,
  onPayloadChange,
  onReadinessChange,
  onSubmit,
  payload,
  pending,
  readinessChecklist,
}: {
  agcParams: AgcGainParams
  environment: Partial<RunEnvironmentMetadata>
  error: string | null
  examplePending: boolean
  onAgcParamsChange: (updates: AgcGainParams) => void
  onEnvironmentChange: (updates: Partial<RunEnvironmentMetadata>) => void
  onLoadExample: () => void
  onPayloadChange: (payload: string) => void
  onReadinessChange: (itemId: string, status: ReadinessStatus) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  payload: string
  pending: boolean
  readinessChecklist: Array<Partial<ReadinessChecklistItem>>
}) {
  const readinessById = new Map(readinessChecklist.map((item) => [item.item_id, item]))
  return (
    <section className="asyncRunPanel">
      <div className="sectionHeader compact">
        <Play size={17} />
        <h2>Async run</h2>
      </div>
      <form className="asyncRunForm" onSubmit={onSubmit}>
        <div className="asyncRunControls">
          <label>
            Profile
            <select
              value={environment.environment_profile ?? 'demo'}
              onChange={(event) =>
                onEnvironmentChange({
                  environment_profile: event.target.value as EnvironmentProfile,
                })
              }
            >
              {ENVIRONMENT_PROFILES.map((profile) => (
                <option key={profile} value={profile}>
                  {profile}
                </option>
              ))}
            </select>
          </label>
          <label>
            Server alias
            <input
              value={environment.server_alias ?? ''}
              onChange={(event) => onEnvironmentChange({ server_alias: event.target.value })}
              placeholder="demo-host-a"
            />
          </label>
          <label>
            Target alias
            <input
              value={environment.integration_target_alias ?? ''}
              onChange={(event) =>
                onEnvironmentChange({ integration_target_alias: event.target.value })
              }
              placeholder="integration-target-a"
            />
          </label>
          <label>
            Tags
            <input
              value={(environment.tags ?? []).join(', ')}
              onChange={(event) =>
                onEnvironmentChange({
                  tags: commaList(event.target.value),
                })
              }
              placeholder="phase4, demo"
            />
          </label>
          <label>
            Manual blockers
            <input
              value={(environment.manual_blockers ?? []).join(', ')}
              onChange={(event) =>
                onEnvironmentChange({
                  manual_blockers: commaList(event.target.value),
                })
              }
              placeholder="route-confirmation"
            />
          </label>
          <label>
            Secret refs
            <input
              value={(environment.secret_ref_names ?? []).join(', ')}
              onChange={(event) =>
                onEnvironmentChange({
                  secret_ref_names: commaList(event.target.value),
                })
              }
              placeholder="provider-api-key-ref"
            />
          </label>
        </div>
        <div className="asyncGainControls">
          <label>
            Target RMS
            <input
              min="0"
              step="1"
              type="number"
              value={numberInput(agcParams.target_rms)}
              onChange={(event) =>
                onAgcParamsChange({ target_rms: parseNumberInput(event.target.value) })
              }
              placeholder="3000"
            />
          </label>
          <label>
            Max gain
            <input
              min="0"
              step="0.1"
              type="number"
              value={numberInput(agcParams.max_gain)}
              onChange={(event) =>
                onAgcParamsChange({ max_gain: parseNumberInput(event.target.value) })
              }
              placeholder="8.0"
            />
          </label>
          <label>
            Noise floor
            <input
              min="0"
              step="1"
              type="number"
              value={numberInput(agcParams.noise_floor)}
              onChange={(event) =>
                onAgcParamsChange({ noise_floor: parseNumberInput(event.target.value) })
              }
              placeholder="200"
            />
          </label>
        </div>
        <div className="asyncReadinessControls">
          {READINESS_CONTROLS.map((item) => {
            const status = readinessById.get(item.item_id)?.status ?? 'unknown'
            return (
              <label key={item.item_id}>
                {item.label}
                <select
                  value={status}
                  onChange={(event) =>
                    onReadinessChange(item.item_id, event.target.value as ReadinessStatus)
                  }
                >
                  {READINESS_STATUSES.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            )
          })}
        </div>
        <label>
          Payload JSON
          <textarea
            spellCheck={false}
            value={payload}
            onChange={(event) => onPayloadChange(event.target.value)}
          />
        </label>
        <button disabled={pending} type="submit">
          <Play size={16} />
          {pending ? 'Starting' : 'Start'}
        </button>
        <button
          className="secondaryButton"
          disabled={examplePending || pending}
          onClick={onLoadExample}
          type="button"
        >
          <Database size={16} />
          {examplePending ? 'Loading' : 'Load example'}
        </button>
        {error ? <div className="asyncRunError">{error}</div> : null}
      </form>
    </section>
  )
}

function LivePreviewPanel({
  connectionState,
  crossSessionError,
  crossSessionLoading,
  crossSessionTrends,
  isError,
  isLoading,
  runs,
}: {
  connectionState: LiveSocketState
  crossSessionError: boolean
  crossSessionLoading: boolean
  crossSessionTrends: CrossSessionTrend[]
  isError: boolean
  isLoading: boolean
  runs: LiveRunStatus[]
}) {
  const increasingTrendCount = crossSessionTrends.filter(
    (trend) => trend.state === 'increasing',
  ).length
  return (
    <section className="livePreviewPanel">
      <div className="sectionHeader compact">
        <Activity size={17} />
        <h2>Live preview</h2>
        <span
          className={`liveSocketBadge ${connectionState === 'error' || connectionState === 'disconnected' ? 'fallback' : connectionState}`}
        >
          {liveConnectionLabel(connectionState)}
        </span>
      </div>
      {isLoading ? <div className="emptyInline">Loading live preview</div> : null}
      {isError ? <div className="emptyInline">Live preview unavailable</div> : null}
      {!isLoading && !isError && runs.length === 0 ? (
        <div className="emptyInline">No recent run status</div>
      ) : null}
      <div className="crossSessionTrendList">
        {crossSessionTrends.length > 0 ? (
          <div className="crossSessionTrendHeader">
            <strong>Cross-session trends</strong>
            <span>{increasingTrendCount} increasing</span>
          </div>
        ) : null}
        {crossSessionLoading ? <div className="emptyInline">Loading cross-session trends</div> : null}
        {crossSessionError ? <div className="emptyInline">Cross-session trends unavailable</div> : null}
        {crossSessionTrends.map((trend) => (
          <article
            className={`crossSessionTrend ${trend.state}`}
            key={`${trend.environment_profile}-${trend.server_alias}-${trend.metric}`}
          >
            <div>
              <strong>{trend.metric}</strong>
              <em>{trend.state}</em>
            </div>
            <span>{trend.server_alias} / {trend.sample_count} runs</span>
            <small>
              {formatCrossSessionTrendValue(trend.metric, trend.first_value)} →{' '}
              {formatCrossSessionTrendValue(trend.metric, trend.latest_value)}{' '}
              (Δ {formatCrossSessionTrendValue(trend.metric, trend.total_delta)})
            </small>
          </article>
        ))}
      </div>
      <div className="liveRunList">
        {runs.map((run) => {
          const connection = run.provider_connection
          const rtpCollector = run.rtp_collector
          const blocked =
            run.readiness_summary.failed_count > 0 ||
            run.readiness_summary.manual_blocker_count > 0 ||
            connection.exhausted ||
            rtpCollector.state === 'failed' ||
            run.status === 'failed'
          const hostMetrics = run.latest_host_metrics.filter(
            (metric) =>
              !metric.name.startsWith('provider_connect_') &&
              !metric.name.startsWith('asterisk_ami_rtcp_'),
          )
          return (
            <article className={blocked ? 'liveRunItem blocked' : 'liveRunItem ready'} key={run.run_id}>
              <div className="liveRunHeader">
                <div>
                  <strong>{shortId(run.run_id)}</strong>
                  <span>{environmentStatusLabel(run)}</span>
                </div>
                <em>{run.status}</em>
              </div>
              {run.failure_alias ? (
                <div className="liveRunBlockers">
                  <strong>Failure</strong>
                  <span>{run.failure_alias}</span>
                </div>
              ) : null}
              <div className="liveRunReadiness">
                <strong>{readinessHeadline(run.readiness_summary)}</strong>
                <span>
                  {run.readiness_summary.failed_count} fail / {run.readiness_summary.unknown_count} unknown
                </span>
              </div>
              {run.manual_blockers.length > 0 ? (
                <div className="liveRunBlockers">
                  <strong>Blockers</strong>
                  <span>{joinList(run.manual_blockers)}</span>
                </div>
              ) : null}
              {connection.state !== 'not_applicable' ? (
                <div className={`providerConnectionSummary ${connection.state}`}>
                  <div className="providerConnectionHeader">
                    <strong>Provider connection</strong>
                    <em>{providerConnectionLabel(connection.state)}</em>
                  </div>
                  <div className="providerConnectionMetrics">
                    <span>
                      <strong>attempts</strong>
                      {connection.attempts}
                    </span>
                    <span>
                      <strong>retries</strong>
                      {connection.retries}
                    </span>
                    <span>
                      <strong>failures</strong>
                      {connection.failures}
                    </span>
                  </div>
                </div>
              ) : null}
              {rtpCollector.state !== 'inactive' ? (
                <div className={`providerConnectionSummary ${rtpCollector.state}`}>
                  <div className="providerConnectionHeader">
                    <strong>RTP collector</strong>
                    <em>{rtpCollector.state}</em>
                  </div>
                  <div className="providerConnectionMetrics">
                    <span>
                      <strong>events</strong>
                      {rtpCollector.events_collected}
                    </span>
                    <span>
                      <strong>failures</strong>
                      {rtpCollector.failures}
                    </span>
                  </div>
                </div>
              ) : null}
              <div className="liveRunHostMetrics">
                {hostMetrics.map((metric) => (
                  <span key={metric.name}>
                    <strong>{metric.name}</strong>
                    {formatLiveHostMetric(metric.name, metric.value)}
                  </span>
                ))}
                {hostMetrics.length === 0 ? (
                  <span>
                    <strong>host</strong>
                    missing
                  </span>
                ) : null}
              </div>
            </article>
          )
        })}
      </div>
    </section>
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
            <div className="recentRunEnv">
              <span>{run.environment_profile}</span>
              <span>{run.server_alias ?? 'no server alias'}</span>
              <span>
                {run.readiness_failed_count} fail / {run.readiness_unknown_count} unknown
              </span>
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
        <EnvironmentComparison primary={primary} compare={compare} />
        <BargeInComparison primary={primary} compare={compare} />
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

function BargeInComparison({
  primary,
  compare,
}: {
  primary: TimelineResponse
  compare: TimelineResponse
}) {
  const summarize = (timeline: TimelineResponse) => {
    const incidents = timeline.lanes.incidents.filter(
      (incident) => incident.rule_id === 'barge_in_sequence',
    )
    const number = (incident: TimelineIncident, name: string) => {
      const value = incident.observed[name]
      return typeof value === 'number' && Number.isFinite(value) ? value : null
    }
    const captured = incidents.filter(
      (incident) => number(incident, 'discarded_signal_bearing_audio_ms') !== null,
    )
    const signalMs = captured.reduce(
      (total, incident) =>
        total + (number(incident, 'discarded_signal_bearing_audio_ms') ?? 0),
      0,
    )
    const discardedMs = captured.reduce(
      (total, incident) =>
        total +
        (number(incident, 'discarded_total_audio_ms') ??
          number(incident, 'discarded_audio_ms') ??
          0),
      0,
    )
    const chunks30 = captured
      .map((incident) => number(incident, 'provider_chunks_last_30ms'))
      .filter((value): value is number => value !== null)
    const lead = captured
      .map((incident) => number(incident, 'first_discarded_audio_lead_ms'))
      .filter((value): value is number => value !== null)
    return {
      events: incidents.length,
      captured: captured.length,
      signalMs,
      discardedMs,
      chunks30Average:
        chunks30.length > 0
          ? chunks30.reduce((total, value) => total + value, 0) / chunks30.length
          : null,
      leadMax: lead.length > 0 ? Math.max(...lead) : null,
    }
  }
  const primaryStats = summarize(primary)
  const compareStats = summarize(compare)
  const rows = [
    {
      label: 'Barge-in events',
      primary: primaryStats.events,
      compare: compareStats.events,
    },
    {
      label: 'Packet proof captured',
      primary: `${primaryStats.captured}/${primaryStats.events}`,
      compare: `${compareStats.captured}/${compareStats.events}`,
    },
    {
      label: 'Signal / discarded',
      primary: `${displayValue(roundDisplay(primaryStats.signalMs))} / ${displayValue(roundDisplay(primaryStats.discardedMs))} ms`,
      compare: `${displayValue(roundDisplay(compareStats.signalMs))} / ${displayValue(roundDisplay(compareStats.discardedMs))} ms`,
    },
    {
      label: 'Provider burst avg',
      primary:
        primaryStats.chunks30Average === null
          ? null
          : `${roundDisplay(primaryStats.chunks30Average)} chunks / 30 ms`,
      compare:
        compareStats.chunks30Average === null
          ? null
          : `${roundDisplay(compareStats.chunks30Average)} chunks / 30 ms`,
    },
    {
      label: 'Max discarded lead',
      primary:
        primaryStats.leadMax === null ? null : `${roundDisplay(primaryStats.leadMax)} ms`,
      compare:
        compareStats.leadMax === null ? null : `${roundDisplay(compareStats.leadMax)} ms`,
    },
  ]
  return (
    <div className="environmentCompare bargeInCompare">
      <div className="environmentCompareHeader">Barge-in packet evidence</div>
      {rows.map((row) => {
        const primaryValue = displayValue(row.primary)
        const compareValue = displayValue(row.compare)
        const changed = primaryValue !== compareValue
        return (
          <div
            className={changed ? 'environmentCompareRow changed' : 'environmentCompareRow'}
            key={row.label}
          >
            <strong>{row.label}</strong>
            <span>{primaryValue}</span>
            <span>{compareValue}</span>
            <em>{changed ? 'different' : 'same'}</em>
          </div>
        )
      })}
      <p>
        Local provider chunks and AudioSocket queue disposal only. Use matched repeated calls
        and a caller-side recording before making an audible-quality claim.
      </p>
    </div>
  )
}

function EnvironmentComparison({
  primary,
  compare,
}: {
  primary: TimelineResponse
  compare: TimelineResponse
}) {
  const rows = [
    {
      label: 'Environment',
      primary: primary.environment.environment_profile,
      compare: compare.environment.environment_profile,
    },
    {
      label: 'Server',
      primary: primary.environment.server_alias,
      compare: compare.environment.server_alias,
    },
    {
      label: 'Target',
      primary: primary.environment.integration_target_alias,
      compare: compare.environment.integration_target_alias,
    },
    {
      label: 'Snapshot',
      primary: primary.environment.environment_snapshot_hash,
      compare: compare.environment.environment_snapshot_hash,
    },
    {
      label: 'Readiness',
      primary: readinessHeadline(primary.readiness_summary),
      compare: readinessHeadline(compare.readiness_summary),
    },
    {
      label: 'Manual blockers',
      primary: joinList(primary.environment.manual_blockers),
      compare: joinList(compare.environment.manual_blockers),
    },
    {
      label: 'Tags',
      primary: joinList(primary.environment.tags),
      compare: joinList(compare.environment.tags),
    },
    {
      label: 'SIP events',
      primary: primary.lanes.sip_ladder.length,
      compare: compare.lanes.sip_ladder.length,
    },
    {
      label: 'RTP points',
      primary: primary.lanes.rtp_quality.length,
      compare: compare.lanes.rtp_quality.length,
    },
    {
      label: 'Latest RTP',
      primary: rtpSummary(latestRtpStat(primary.lanes.rtp_quality)),
      compare: rtpSummary(latestRtpStat(compare.lanes.rtp_quality)),
    },
  ]

  return (
    <div className="environmentCompare">
      <div className="environmentCompareHeader">Environment deltas</div>
      {rows.map((row) => {
        const primaryValue = displayValue(row.primary)
        const compareValue = displayValue(row.compare)
        const changed = primaryValue !== compareValue
        return (
          <div className={changed ? 'environmentCompareRow changed' : 'environmentCompareRow'} key={row.label}>
            <strong>{row.label}</strong>
            <span>{primaryValue}</span>
            <span>{compareValue}</span>
            <em>{changed ? 'different' : 'same'}</em>
          </div>
        )
      })}
    </div>
  )
}

function StageLane({
  compareEnabled,
  compareStage,
  onSelect,
  selected,
  stage,
}: {
  compareEnabled: boolean
  compareStage?: TimelineStageLane
  onSelect: () => void
  selected: boolean
  stage: TimelineStageLane
}) {
  const failed = stage.violations.length > 0
  const measured = stage.metrics.length > 0
  const compareBadge = compareStage
    ? compareStageBadge(stage, compareStage)
    : compareEnabled
      ? { label: 'missing stage', tone: 'missing' }
      : null
  const deltaChips = compareStage ? metricDeltaChips(stage, compareStage) : []
  return (
    <article
      className={`stageLane ${failed ? 'failed' : measured ? 'passed' : 'unmeasured'} ${selected ? 'selected' : ''}`}
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
        <span
          className={`statusBadge ${failed ? 'failed' : measured ? 'passed' : 'unmeasured'}`}
        >
          {failed ? <AlertTriangle size={15} /> : measured ? <CheckCircle2 size={15} /> : null}
          {failed ? `${stage.violations.length} fail` : measured ? 'pass' : 'not measured'}
        </span>
      </div>
      {compareBadge ? (
        <div className={`compareBadge ${compareBadge.tone}`}>
          <strong>Compare</strong>
          <span>{compareBadge.label}</span>
        </div>
      ) : null}
      {deltaChips.length > 0 ? (
        <div className="metricDeltaStrip">
          {deltaChips.map((delta) => (
            <span className={delta.tone} key={delta.name}>
              <strong>{delta.name}</strong>
              {formatSigned(delta.value)}
            </span>
          ))}
        </div>
      ) : null}
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

function EnvironmentPanel({ environment }: { environment: RunEnvironmentMetadata }) {
  return (
    <section className="environmentPanel">
      <div className="sectionHeader compact">
        <Server size={17} />
        <h2>Environment</h2>
      </div>
      <div className="environmentFacts">
        <Fact label="Profile" value={environment.environment_profile} />
        <Fact label="Server" value={environment.server_alias} />
        <Fact label="Target" value={environment.integration_target_alias} />
        <Fact label="Snapshot" value={environment.environment_snapshot_hash} />
        <Fact label="Started from" value={environment.started_from} />
        <Fact label="Internal ref" value={environment.related_internal_ref} />
        <Fact label="Secret refs" value={`${environment.secret_ref_names.length} refs`} />
      </div>
      {environment.tags.length > 0 ? <ChipList label="Tags" values={environment.tags} /> : null}
      {environment.operator_note ? (
        <div className="operatorNote">
          <strong>Note</strong>
          <span>{environment.operator_note}</span>
        </div>
      ) : null}
    </section>
  )
}

function ReadinessPanel({
  checklist,
  environment,
  summary,
}: {
  checklist: ReadinessChecklistItem[]
  environment: RunEnvironmentMetadata
  summary: ReadinessSummary
}) {
  return (
    <section className="readinessPanel">
      <div className="sectionHeader compact">
        <ListChecks size={17} />
        <h2>Readiness</h2>
      </div>
      <div
        className={`readinessSummary ${summary.failed_count > 0 || summary.manual_blocker_count > 0 ? 'blocked' : 'ready'}`}
      >
        <strong>{readinessHeadline(summary)}</strong>
        <span>
          {summary.passed_count} pass / {summary.failed_count} fail / {summary.unknown_count} unknown
        </span>
      </div>
      {environment.manual_blockers.length > 0 ? (
        <ChipList label="Manual blockers" values={environment.manual_blockers} tone="danger" />
      ) : null}
      <div className="readinessList">
        {checklist.map((item) => (
          <article className={`readinessItem ${item.status}`} key={item.item_id}>
            <div>
              <strong>{item.label}</strong>
              {item.note ? <span>{item.note}</span> : null}
            </div>
            <em>{item.status}</em>
          </article>
        ))}
      </div>
    </section>
  )
}

function Fact({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="fact">
      <span>{label}</span>
      <strong>{displayValue(value)}</strong>
    </div>
  )
}

function ChipList({
  label,
  tone,
  values,
}: {
  label: string
  tone?: 'danger'
  values: string[]
}) {
  return (
    <div className={tone === 'danger' ? 'chipList danger' : 'chipList'}>
      <strong>{label}</strong>
      <div>
        {values.map((value) => (
          <span key={value}>{value}</span>
        ))}
      </div>
    </div>
  )
}

function StageDetail({
  apiBase,
  audioSessionRevision,
  compareRecording,
  compareRunId,
  compareStage,
  recording,
  runId,
  stage,
}: {
  apiBase: string
  audioSessionRevision: number
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
            audioSessionRevision={audioSessionRevision}
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
              audioSessionRevision={audioSessionRevision}
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
  audioSessionRevision,
  durationMs,
  label,
  onActivate,
  onDeactivate,
  playerId,
  src,
}: {
  activePlayerId: string | null
  audioSessionRevision: number
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
          audioSessionRevision={audioSessionRevision}
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
  audioSessionRevision,
  onActivate,
  onDeactivate,
  playerId,
  src,
}: {
  activePlayerId: string | null
  audioSessionRevision: number
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
      fetchParams: { credentials: 'include' },
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
  }, [audioSessionRevision, onActivate, onDeactivate, playerId, src])

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
      <audio
        className="nativeAudio"
        controls
        crossOrigin="use-credentials"
        key={audioSessionRevision}
        preload="none"
        src={src}
      />
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

function HostMetricsPanel({ metrics }: { metrics: TimelineMetricPoint[] }) {
  const latest = latestMetrics(metrics)
  return (
    <section className="hostMetricsPanel">
      <div className="sectionHeader compact">
        <Server size={17} />
        <h2>Host metrics</h2>
      </div>
      {latest.length > 0 ? (
        <div className="hostMetricGrid">
          {latest.map((metric) => (
            <div className="hostMetric" key={metric.name}>
              <span>{metric.name}</span>
              <strong>{formatHostMetric(metric)}</strong>
              <small>{metric.ts.toFixed(3)}s</small>
            </div>
          ))}
        </div>
      ) : (
        <div className="emptyInline">No host metrics</div>
      )}
    </section>
  )
}

function SipLadderPanel({ events }: { events: TimelineSipEvent[] }) {
  return (
    <section className="sipLadderPanel">
      <div className="sectionHeader compact">
        <Signal size={17} />
        <h2>SIP ladder</h2>
        <span className="panelCount">{events.length}</span>
      </div>
      {events.length > 0 ? (
        <div className="sipEventList">
          {events.map((event, index) => (
            <article className="sipEvent" key={`${event.ts}-${event.method}-${index}`}>
              <div className="sipEventHeader">
                <strong>{event.method}</strong>
                <span>{event.direction}</span>
              </div>
              <div className="sipEventFacts">
                <Fact label="Status" value={event.status_code?.toString() ?? null} />
                <Fact label="Summary" value={event.summary_alias} />
                <Fact label="Time" value={`${event.ts.toFixed(3)}s`} />
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="emptyInline">No SIP events</div>
      )}
    </section>
  )
}

function RtpQualityPanel({ stats }: { stats: TimelineRtpStat[] }) {
  const latest = latestRtpStat(stats)
  return (
    <section className="rtpQualityPanel">
      <div className="sectionHeader compact">
        <Waves size={17} />
        <h2>RTP quality</h2>
        <span className="panelCount">{stats.length}</span>
      </div>
      {latest ? (
        <div className="rtpLatest">
          <strong>Latest</strong>
          <span>{rtpSummary(latest)}</span>
          <small>{latest.ts.toFixed(3)}s</small>
        </div>
      ) : null}
      {stats.length > 0 ? (
        <div className="rtpStatList">
          {stats.map((stat, index) => (
            <article className="rtpStat" key={`${stat.ts}-${index}`}>
              <Fact label="Jitter" value={formatNullableMetric(stat.jitter_ms, ' ms')} />
              <Fact label="Loss" value={formatNullableMetric(stat.loss_pct, '%')} />
              <Fact label="RTT" value={formatNullableMetric(stat.rtt_ms, ' ms')} />
              <Fact label="MOS" value={formatNullableMetric(stat.mos)} />
              <Fact label="Direction" value={stat.direction ?? '-'} />
              <Fact label="Time" value={`${stat.ts.toFixed(3)}s`} />
            </article>
          ))}
        </div>
      ) : (
        <div className="emptyInline">No RTP quality points</div>
      )}
    </section>
  )
}

function Recordings({
  apiBase,
  audioSessionRevision,
  recordings,
  runId,
}: {
  apiBase: string
  audioSessionRevision: number
  recordings: TimelineRecording[]
  runId: string
}) {
  const base = apiBase.replace(/\/$/, '')
  return (
    <section className="recordings">
      <div className="sectionHeader compact">
        <Headphones size={17} />
        <h2>Listen to every stage</h2>
      </div>
      <p className="panelLead">
        Start with resampler as the reference, then compare AGC, limiter, and serializer.
      </p>
      <div className="recordingList">
        {recordings.map((recording) => {
          const src = `${base}/runs/${encodeURIComponent(runId)}/recordings/${encodeURIComponent(recording.stage)}/audio`
          return (
            <div key={recording.stage} className="recordingItem">
              <div className="recordingMeta">
                <strong>{recording.stage}</strong>
                <span>{formatNumber(recording.duration_ms)} ms</span>
              </div>
              <audio
                controls
                crossOrigin="use-credentials"
                key={`${recording.stage}-${audioSessionRevision}`}
                preload="metadata"
                src={src}
              />
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

function formatHostMetric(metric: TimelineMetricPoint) {
  if (metric.name === 'cpu') {
    return `${formatNumber(metric.value)}%`
  }
  if (metric.name === 'loop_lag') {
    return `${formatNumber(metric.value)} ms`
  }
  return formatNumber(metric.value)
}

function formatLiveHostMetric(name: string, value: number) {
  if (name === 'cpu') {
    return `${formatNumber(value)}%`
  }
  if (name === 'loop_lag') {
    return `${formatNumber(value)} ms`
  }
  return formatNumber(value)
}

function providerConnectionLabel(state: LiveRunStatus['provider_connection']['state']) {
  if (state === 'not_applicable') {
    return 'n/a'
  }
  return state
}

function formatCrossSessionTrendValue(metric: string, value: number) {
  if (metric === 'memory_rss_bytes') {
    return `${formatNumber(value / (1024 * 1024))} MiB`
  }
  return formatNumber(value)
}

function latestMetrics(metrics: TimelineMetricPoint[]) {
  const byName = new Map<string, TimelineMetricPoint>()
  for (const metric of metrics) {
    const current = byName.get(metric.name)
    if (!current || metric.ts >= current.ts) {
      byName.set(metric.name, metric)
    }
  }
  return Array.from(byName.values()).sort((left, right) => left.name.localeCompare(right.name))
}

function latestRtpStat(stats: TimelineRtpStat[]) {
  return stats.reduce<TimelineRtpStat | null>(
    (latest, stat) => (latest === null || stat.ts >= latest.ts ? stat : latest),
    null,
  )
}

function rtpSummary(stat: TimelineRtpStat | null) {
  if (!stat) {
    return null
  }
  return [
    `jitter ${formatNullableMetric(stat.jitter_ms, ' ms')}`,
    `loss ${formatNullableMetric(stat.loss_pct, '%')}`,
    `rtt ${formatNullableMetric(stat.rtt_ms, ' ms')}`,
    `mos ${formatNullableMetric(stat.mos)}`,
  ].join(' / ')
}

function formatNullableMetric(value: number | null, suffix = '') {
  return value === null ? '-' : `${formatNumber(value)}${suffix}`
}

function environmentStatusLabel(run: LiveRunStatus) {
  const alias = run.server_alias ?? run.integration_target_alias
  return alias ? `${run.environment_profile} / ${alias}` : run.environment_profile
}

function environmentHeadline(environment: RunEnvironmentMetadata) {
  const server = environment.server_alias ?? environment.integration_target_alias
  return server ? `${environment.environment_profile} / ${server}` : environment.environment_profile
}

function readinessHeadline(summary: ReadinessSummary) {
  if (summary.incomplete_count === 0) {
    return 'ready'
  }
  return `${summary.incomplete_count} incomplete`
}

function inspectorDurationMs(timeline: TimelineResponse) {
  const values = [1_000]
  for (const event of timeline.lanes.events) {
    values.push(event.t_rel_ms)
  }
  for (const interval of timeline.lanes.intervals) {
    values.push(interval.end_ms)
  }
  for (const series of timeline.lanes.series) {
    for (const point of series.points) {
      values.push(point.t_rel_ms)
    }
  }
  for (const artifact of timeline.lanes.artifacts) {
    values.push(artifact.start_ms + (artifact.duration_ms ?? 0))
  }
  for (const incident of timeline.lanes.incidents) {
    values.push(incident.end_ms)
  }
  return Math.max(...values)
}

function inspectorCategoryCount(timeline: TimelineResponse, category: TimelineCategory) {
  return (
    timeline.lanes.events.filter((event) => event.category === category).length +
    timeline.lanes.intervals.filter((interval) => interval.category === category).length +
    timeline.lanes.series.filter((series) => series.category === category).length +
    timeline.lanes.artifacts.filter((artifact) => artifact.category === category).length +
    timeline.lanes.incidents.filter((incident) => incident.category === category).length
  )
}

function sampleInspectorPoints(
  series: TimelineResponse['lanes']['series'],
  maximum: number,
): Array<{
  series: TimelineResponse['lanes']['series'][number]
  point: TimelineResponse['lanes']['series'][number]['points'][number]
}> {
  const all = series.flatMap((item) =>
    item.points.map((point) => ({ series: item, point })),
  )
  if (all.length <= maximum) {
    return all
  }
  const stride = all.length / maximum
  return Array.from({ length: maximum }, (_, index) => all[Math.floor(index * stride)])
}

function timePositionPercent(valueMs: number, durationMs: number) {
  return Math.max(0, Math.min(100, (valueMs / Math.max(durationMs, 1)) * 100))
}

function formatTimelineTime(valueMs: number) {
  const seconds = Math.max(0, valueMs) / 1000
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60)
    return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, '0')}`
  }
  return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`
}

function timelineCategoryLabel(category: TimelineCategory) {
  const labels: Record<TimelineCategory, string> = {
    conversation: 'Conversation',
    signaling: 'Signaling',
    transport: 'Transport',
    buffer: 'Buffers',
    pipeline: 'Pipeline',
    provider: 'Provider',
    runtime: 'Host',
    session: 'Session',
  }
  return labels[category]
}

function readinessProductHeadline(summary: ReadinessSummary) {
  if (summary.failed_count > 0) {
    return `${summary.failed_count} failed`
  }
  if (summary.manual_blocker_count > 0) {
    return `${summary.manual_blocker_count} blocked`
  }
  if (summary.unknown_count > 0) {
    return `${summary.unknown_count} not checked`
  }
  return 'ready'
}

function liveConnectionLabel(state: LiveSocketState) {
  if (state === 'error' || state === 'disconnected') {
    return 'REST fallback'
  }
  return state
}

function displayValue(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  return String(value)
}

function roundDisplay(value: number) {
  return Math.round(value * 1000) / 1000
}

function joinList(values: string[]) {
  return values.length > 0 ? values.join(', ') : null
}

function stageMap(timeline: TimelineResponse) {
  return new Map(timeline.lanes.stages.map((stage) => [stage.stage, stage]))
}

function compareStageBadge(primary: TimelineStageLane, compare: TimelineStageLane) {
  const delta = compare.violations.length - primary.violations.length
  if (delta < 0) {
    return { label: `${Math.abs(delta)} fewer ${failWord(delta)}`, tone: 'improved' }
  }
  if (delta > 0) {
    return { label: `${delta} more ${failWord(delta)}`, tone: 'regressed' }
  }
  return { label: 'same failure count', tone: 'even' }
}

function failWord(value: number) {
  return Math.abs(value) === 1 ? 'fail' : 'fails'
}

function metricDeltas(primary: TimelineStageLane, compare: TimelineStageLane) {
  const deltas = metricDeltaChips(primary, compare)
    .slice(0, 3)
    .map((metric) => `${metric.name} ${formatSigned(metric.value)}`)

  return deltas.length > 0 ? deltas.join(', ') : 'no shared metrics'
}

function metricDeltaChips(primary: TimelineStageLane, compare: TimelineStageLane) {
  const compareMetrics = new Map(compare.metrics.map((metric) => [metric.name, metric.value]))
  return primary.metrics
    .filter((metric) => compareMetrics.has(metric.name))
    .map((metric) => {
      const value = (compareMetrics.get(metric.name) ?? 0) - metric.value
      return {
        name: metric.name,
        tone: value === 0 ? 'even' : value > 0 ? 'up' : 'down',
        value,
      }
    })
    .sort((left, right) => Math.abs(right.value) - Math.abs(left.value))
    .slice(0, 3)
}

function formatSigned(value: number) {
  if (value === 0) return '0'
  const formatted = Number.isInteger(value) ? String(Math.abs(value)) : Math.abs(value).toFixed(3)
  return `${value > 0 ? '+' : '-'}${formatted}`
}
