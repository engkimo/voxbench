import { useMemo, useState } from 'react'
import type { TimelineResponse, TimelineSeriesPoint } from './types'
import './mediaTiming.css'

export type MediaTimingRun = {
  id: string
  label: string
  durationMs: number
  send: TimelineSeriesPoint[]
  loop: TimelineSeriesPoint[]
  sameClock: boolean
  alignmentMs?: number | null
}

type Window = { startMs: number; endMs: number }
type Candidate = { send: Window; loop: Window; overlap: Window }

const RTP_GAP_MS = 100
const LOOP_DELAY_MS = 50
const MIN_OVERLAP_MS = 20
const WIDTH = 760
const LEFT = 53
const RIGHT = 743
const TOP = 12
const BOTTOM = 104

export function mediaTimingRuns(timeline: TimelineResponse, label: string): MediaTimingRun[] {
  const outgoing = timeline.lanes.series.filter(
    (series) => series.name === 'rtp_send_interval_ms' && series.direction === 'sent',
  )
  const loops = timeline.lanes.series.filter(
    (series) => series.category === 'runtime' && series.name === 'loop_lag' && series.unit === 'ms',
  )
  return (outgoing.length ? outgoing : [null]).flatMap((send) => {
    const matching = loops.filter((loop) => loop.clock_domain === send?.clock_domain)
    const loop = matching.length === 1 ? matching[0] : loops.length === 1 ? loops[0] : null
    if (!send && !loop) return []
    const sendPoints = send?.points ?? []
    const loopPoints = loop?.points ?? []
    return [{
      id: `${timeline.run_id}:${send?.series_id ?? 'loop'}`,
      label: outgoing.length > 1 ? `${label} · ${send?.stream_alias}` : label,
      durationMs: Math.max(sendPoints.at(-1)?.t_rel_ms ?? 0, loopPoints.at(-1)?.t_rel_ms ?? 0),
      send: sendPoints,
      loop: loopPoints,
      sameClock: Boolean(send && loop && send.clock_domain === loop.clock_domain),
      alignmentMs: send?.alignment_uncertainty_ms,
    }]
  })
}

function span(point: TimelineSeriesPoint): Window {
  return { startMs: point.t_rel_ms - point.value, endMs: point.t_rel_ms }
}

function summarize(run: MediaTimingRun) {
  const gaps = run.send.filter((point) => point.value >= RTP_GAP_MS)
  const stalls = run.loop.filter((point) => point.value >= LOOP_DELAY_MS).map(span)
  let stallIndex = 0
  let overlappingGaps = 0
  let candidate: Candidate | null = null
  if (run.sameClock) {
    for (const gap of gaps) {
      const send = span(gap)
      while (stallIndex < stalls.length && stalls[stallIndex].endMs <= send.startMs) stallIndex += 1
      let best: Candidate | null = null
      for (let index = stallIndex; index < stalls.length; index += 1) {
        const loop = stalls[index]
        if (loop.startMs >= send.endMs) break
        const overlap = {
          startMs: Math.max(send.startMs, loop.startMs),
          endMs: Math.min(send.endMs, loop.endMs),
        }
        const duration = overlap.endMs - overlap.startMs
        if (duration >= MIN_OVERLAP_MS && (!best || duration > best.overlap.endMs - best.overlap.startMs)) {
          best = { send, loop, overlap }
        }
      }
      if (best) {
        overlappingGaps += 1
        if (!candidate || gap.value > candidate.send.endMs - candidate.send.startMs) candidate = best
      }
    }
  }
  return { longGaps: gaps.length, overlappingGaps, candidate }
}

function seconds(ms: number, decimals = 2) {
  return `${(ms / 1000).toFixed(decimals)} s`
}

function milliseconds(ms: number) {
  return `${ms.toFixed(2)} ms`
}

function windowText(interval: Window) {
  return `${seconds(interval.startMs, 3)}–${seconds(interval.endMs, 3)}`
}

function TimingChart({
  run, window, yMax, candidate, cursorMs, onSeek,
}: {
  run: MediaTimingRun
  window: Window
  yMax: number
  candidate?: Candidate
  cursorMs?: number
  onSeek?: (ms: number) => void
}) {
  const range = Math.max(1, window.endMs - window.startMs)
  const x = (ms: number) => LEFT + ((ms - window.startMs) / range) * (RIGHT - LEFT)
  const y = (ms: number) => BOTTOM - (Math.min(ms, yMax) / yMax) * (BOTTOM - TOP)
  const ticks = [0, 1, 2, 3, 4].map((part) => window.startMs + (range * part) / 4)
  function strokes(points: TimelineSeriesPoint[]) {
    return points.flatMap((point) => (
      point.t_rel_ms >= window.startMs && point.t_rel_ms <= window.endMs
        ? [`M${x(point.t_rel_ms).toFixed(1)} ${BOTTOM}V${y(point.value).toFixed(1)}`]
        : []
    )).join('')
  }
  return (
    <svg
      aria-label={`${run.label}: outbound RTP intervals and event-loop delay in milliseconds`}
      className="timingChart"
      onClick={onSeek && ((event) => {
        const matrix = event.currentTarget.getScreenCTM()
        if (!matrix) return
        const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
        onSeek(window.startMs + Math.max(0, Math.min(1, (point.x - LEFT) / (RIGHT - LEFT))) * range)
      })}
      role="img"
      viewBox={`0 0 ${WIDTH} 134`}
    >
      {candidate && (
        <g>
          <rect className="timingOverlap" x={x(candidate.overlap.startMs)} y={TOP}
            width={x(candidate.overlap.endMs) - x(candidate.overlap.startMs)} height={BOTTOM - TOP} />
          <line className="timingSendSpan" x1={x(candidate.send.startMs)} x2={x(candidate.send.endMs)} y1={TOP + 4} y2={TOP + 4} />
          <line className="timingLoopSpan" x1={x(candidate.loop.startMs)} x2={x(candidate.loop.endMs)} y1={TOP + 10} y2={TOP + 10} />
        </g>
      )}
      {[0, yMax / 2, yMax].map((value) => (
        <g key={value}>
          <line className="timingGridline" x1={LEFT} x2={RIGHT} y1={y(value)} y2={y(value)} />
          <text x={LEFT - 7} y={y(value) + 4} textAnchor="end">{value}</text>
        </g>
      ))}
      {ticks.map((value, index) => (
        <text key={value} x={x(value)} y={125}
          textAnchor={index === 0 ? 'start' : index === ticks.length - 1 ? 'end' : 'middle'}>
          {seconds(value, candidate ? 2 : 1)}
        </text>
      ))}
      <path className="timingLoopLine" d={strokes(run.loop)} />
      <path className="timingSendLine" d={strokes(run.send)} />
      {candidate && (
        <g>
          <circle className="timingSendDot" cx={x(candidate.send.endMs)} cy={y(candidate.send.endMs - candidate.send.startMs)} r="4" />
          <circle className="timingLoopDot" cx={x(candidate.loop.endMs)} cy={y(candidate.loop.endMs - candidate.loop.startMs)} r="4" />
        </g>
      )}
      {cursorMs !== undefined && cursorMs >= window.startMs && cursorMs <= window.endMs && (
        <line className="timingCursor" x1={x(cursorMs)} x2={x(cursorMs)} y1={TOP} y2={BOTTOM} />
      )}
    </svg>
  )
}

export function MediaTimingPanel({
  runs, cursorMs, cursorRunId, onSeek, window, timeBasis = 'Time from run start',
}: {
  runs: MediaTimingRun[]
  cursorMs?: number
  cursorRunId?: string
  onSeek?: (ms: number) => void
  window?: Window
  timeBasis?: string
}) {
  const summary = useMemo(() => runs.map((run) => ({ run, ...summarize(run) })), [runs])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = summary.find(({ run }) => run.id === selectedId)
  const candidate = selected?.candidate ?? null
  const durationMs = Math.max(1, ...runs.map((run) => run.durationMs))
  const view = candidate ? {
    startMs: Math.max(0, candidate.send.startMs - 500),
    endMs: Math.max(candidate.send.endMs, candidate.loop.endMs) + 500,
  } : window ?? { startMs: 0, endMs: durationMs }
  const peak = runs.reduce((max, run) => (
    [...run.send, ...run.loop].reduce((value, point) => Math.max(value, point.value), max)
  ), 100)
  const yMax = Math.ceil(peak / 100) * 100
  const rows = candidate && selected ? [selected] : summary

  if (rows.length === 0) return null

  return (
    <section className="mediaTiming" aria-label="RTP send interval and event-loop delay">
      <div className="timingHeader">
        <div>
          <h2>RTP send interval and event-loop delay</h2>
          <p>{timeBasis} · Vertical scale in milliseconds</p>
        </div>
        {candidate && (
          <button className="timingButton" onClick={() => setSelectedId(null)} type="button">
            Back to overview
          </button>
        )}
      </div>
      <div className="timingLegend">
        <span><i className="timingSendKey" />Outbound RTP packet interval</span>
        <span><i className="timingLoopKey" />Event-loop delay</span>
        {candidate && <span><i className="timingOverlapKey" />Overlap</span>}
      </div>
      {rows.map(({ run, longGaps, overlappingGaps, candidate: rowCandidate }) => (
        <div className="timingRow" key={run.id}>
          <div className="timingRowHeader">
            <h3>{run.label}</h3>
            {run.send.length > 0 && (
              <span>
                {longGaps} gaps ≥ {RTP_GAP_MS} ms
                {run.sameClock ? ` · ${overlappingGaps} overlap loop delays` : ''}
              </span>
            )}
            {!candidate && rowCandidate && (
              <button className="timingButton" type="button" onClick={() => {
                setSelectedId(run.id)
                if (run.id === cursorRunId) onSeek?.(rowCandidate.send.startMs)
              }}>
                Inspect longest overlapping gap
              </button>
            )}
          </div>
          <TimingChart run={run} window={view} yMax={yMax} candidate={candidate ?? undefined}
            cursorMs={run.id === cursorRunId ? cursorMs : undefined}
            onSeek={run.id === cursorRunId ? onSeek : undefined} />
          {(!run.send.length || !run.loop.length) && (
            <p>{run.send.length ? 'Event-loop delay not observed' : 'Outbound RTP interval not observed'}</p>
          )}
          {run.send.length > 0 && run.loop.length > 0 && !run.sameClock && (
            <p>Clock domains differ. The chart does not calculate overlap for these series.</p>
          )}
        </div>
      ))}
      {candidate && (
        <dl className="timingDetail">
          <div><dt>RTP send gap</dt><dd>{milliseconds(candidate.send.endMs - candidate.send.startMs)}</dd>
            <span>{windowText(candidate.send)}</span></div>
          <div><dt>Event-loop delay</dt><dd>{milliseconds(candidate.loop.endMs - candidate.loop.startMs)}</dd>
            <span>{windowText(candidate.loop)}</span></div>
          <div><dt>Overlap</dt><dd>{(candidate.overlap.endMs - candidate.overlap.startMs).toFixed(1)} ms</dd>
            <span>{windowText(candidate.overlap)}</span></div>
        </dl>
      )}
      <p className="timingFootnote">
        Overlap counts require a send gap ≥ {RTP_GAP_MS} ms, a loop delay ≥ {LOOP_DELAY_MS} ms,
        and ≥ {MIN_OVERLAP_MS} ms in common. Both signals are observed on the server side.
        {candidate && selected?.run.alignmentMs != null ? ` Clock alignment uncertainty estimate: ±${selected.run.alignmentMs} ms.` : ''}
      </p>
    </section>
  )
}
