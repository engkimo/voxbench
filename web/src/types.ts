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

export type TimelineResponse = {
  run_id: string
  t0: string
  config_hash: string
  lanes: {
    sip_ladder: Record<string, unknown>[]
    rtp_quality: Record<string, unknown>[]
    stages: TimelineStageLane[]
    turns: Record<string, unknown>[]
    host: Record<string, unknown>[]
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
  ended_at: string
  recording_count: number
  violation_count: number
}
