export type TimelineMetricPoint = {
  ts: number
  name: string
  value: number
}

export type TimelineViolation = {
  invariant: string
  passed: boolean
  detail: string
  observed: Record<string, unknown>
  expected: Record<string, unknown>
}

export type TimelineStageLane = {
  stage: string
  metrics: TimelineMetricPoint[]
  violations: TimelineViolation[]
}

export type TimelineRecording = {
  stage: string
  uri: string
  format: Record<string, unknown>
  duration_ms: number
}

export type TimelineCategory =
  | 'conversation'
  | 'signaling'
  | 'transport'
  | 'buffer'
  | 'pipeline'
  | 'provider'
  | 'runtime'
  | 'session'

export type TimelineTypedEvent = {
  event_id: string
  category: TimelineCategory
  name: string
  t_rel_ms: number
  clock_domain: string
  alignment_uncertainty_ms: number | null
  direction: string | null
  stage: string | null
  stream_alias: string | null
  source: string
  correlation_alias: string | null
  attributes: Record<string, unknown>
}

export type TimelineTypedInterval = {
  interval_id: string
  category: TimelineCategory
  name: string
  start_ms: number
  end_ms: number
  clock_domain: string
  alignment_uncertainty_ms: number | null
  direction: string | null
  stage: string | null
  stream_alias: string | null
  source: string
  correlation_alias: string | null
  attributes: Record<string, unknown>
}

export type TimelineSeriesPoint = {
  t_rel_ms: number
  value: number
}

export type TimelineTypedSeries = {
  series_id: string
  category: TimelineCategory
  name: string
  unit: string | null
  clock_domain: string
  alignment_uncertainty_ms: number | null
  direction: string | null
  stage: string | null
  stream_alias: string | null
  source: string
  points: TimelineSeriesPoint[]
}

export type TimelineTypedArtifact = {
  artifact_id: string
  category: TimelineCategory
  name: string
  kind: 'audio' | 'trace' | 'report' | 'capture' | 'config'
  start_ms: number
  duration_ms: number | null
  stage: string | null
  direction: string | null
  artifact_ref: string
  metadata: Record<string, unknown>
}

export type TimelineIncident = {
  incident_id: string
  rule_id: string
  category: TimelineCategory
  severity: 'info' | 'warning' | 'error'
  title: string
  summary: string
  start_ms: number
  end_ms: number
  confidence: 'certain' | 'high' | 'medium' | 'low'
  stage: string | null
  direction: string | null
  observed: Record<string, unknown>
  expected: Record<string, unknown>
  evidence_refs: string[]
}

export type StorageReadiness = {
  mode: 'local' | 'minio' | 'injected'
  state: 'ready' | 'configured' | 'unavailable'
  bucket_alias: string | null
  prefix_alias: string | null
  secure: boolean | null
  reason_alias: string | null
  remote_audio_proxy_enabled: boolean
  web_audio_session_enabled: boolean
  web_audio_cookie_secure: boolean | null
  web_audio_session_ttl_seconds: number | null
}

export type AudioSessionStatus = {
  enabled: boolean
  authenticated: boolean
  expires_in_seconds: number | null
}

export type TimelineSipEvent = {
  ts: number
  call_id: string | null
  method: string
  direction: 'in' | 'out'
  status_code: number | null
  summary_alias: string | null
}

export type TimelineRtpStat = {
  ts: number
  jitter_ms: number | null
  loss_pct: number | null
  mos: number | null
  direction: 'received' | 'sent' | null
  rtt_ms: number | null
}

export type EnvironmentProfile = 'local' | 'dev' | 'demo' | 'integration' | 'staging'

export type RunEnvironmentMetadata = {
  environment_profile: EnvironmentProfile
  server_alias: string | null
  integration_target_alias: string | null
  environment_snapshot_hash: string | null
  started_from: string | null
  operator_note: string | null
  manual_blockers: string[]
  tags: string[]
  related_internal_ref: string | null
  secret_ref_names: string[]
}

export type ReadinessStatus = 'pass' | 'fail' | 'unknown'

export type ReadinessChecklistItem = {
  item_id: string
  label: string
  status: ReadinessStatus
  note: string | null
}

export type ReadinessSummary = {
  passed_count: number
  failed_count: number
  unknown_count: number
  manual_blocker_count: number
  incomplete_count: number
}

export type TimelineResponse = {
  run_id: string
  t0: string
  config_hash: string
  environment: RunEnvironmentMetadata
  readiness_checklist: ReadinessChecklistItem[]
  readiness_summary: ReadinessSummary
  lanes: {
    sip_ladder: TimelineSipEvent[]
    rtp_quality: TimelineRtpStat[]
    stages: TimelineStageLane[]
    turns: Record<string, unknown>[]
    host: TimelineMetricPoint[]
    recordings: TimelineRecording[]
    events: TimelineTypedEvent[]
    intervals: TimelineTypedInterval[]
    series: TimelineTypedSeries[]
    artifacts: TimelineTypedArtifact[]
    incidents: TimelineIncident[]
  }
}

export type ExperimentCriterion = {
  key: string
  label: string
  status: 'pass' | 'fail' | 'missing'
  detail: string
}

export type ExperimentFinding = {
  classification: 'observed' | 'derived' | 'unknown' | 'recommended'
  title: string
  detail: string
  run_role: 'primary' | 'compare' | 'both'
  evidence_refs: string[]
}

export type RunExperimentSnapshot = {
  run_id: string
  role: 'no-interruption' | 'intentional-barge-in' | 'unknown'
  observed_call: boolean
  duration_ms: number
  provider: string
  config_hash: string
  rtp_report_count: number
  incident_count: number
  barge_in_count: number
  recording_stages: string[]
}

export type RealCallExperiment = {
  status: 'needs-compare' | 'ready' | 'inconclusive'
  summary: string
  primary: RunExperimentSnapshot
  compare: RunExperimentSnapshot | null
  criteria: ExperimentCriterion[]
  findings: ExperimentFinding[]
  next_actions: string[]
}

export type RunSummary = {
  run_id: string
  config_hash: string
  provider: string
  engine: string
  status: string
  started_at: string
  ended_at: string | null
  recording_count: number
  violation_count: number
  environment_profile: EnvironmentProfile
  server_alias: string | null
  integration_target_alias: string | null
  readiness_failed_count: number
  readiness_unknown_count: number
  manual_blocker_count: number
  tags: string[]
}

export type HostMetricSnapshot = {
  name: string
  value: number
  ts: string
}

export type CrossSessionTrend = {
  metric: string
  environment_profile: EnvironmentProfile
  server_alias: string
  state: 'insufficient' | 'stable' | 'increasing'
  sample_count: number
  first_value: number
  latest_value: number
  total_delta: number
  points: Array<{
    run_id: string
    started_at: string
    value: number
  }>
}

export type ProviderConnectionState =
  | 'not_applicable'
  | 'pending'
  | 'connected'
  | 'exhausted'
  | 'unobserved'

export type ProviderConnectionStatus = {
  state: ProviderConnectionState
  attempts: number
  retries: number
  failures: number
  exhausted: boolean
}

export type RtpCollectorStatus = {
  state: 'inactive' | 'connected' | 'collecting' | 'failed'
  events_collected: number
  failures: number
}

export type LiveRunStatus = {
  run_id: string
  status: string
  failure_alias: string | null
  started_at: string
  ended_at: string | null
  environment_profile: EnvironmentProfile
  server_alias: string | null
  integration_target_alias: string | null
  readiness_summary: ReadinessSummary
  manual_blockers: string[]
  latest_host_metrics: HostMetricSnapshot[]
  provider_connection: ProviderConnectionStatus
  rtp_collector: RtpCollectorStatus
  violation_count: number
  tags: string[]
}
