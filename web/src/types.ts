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
  }
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
