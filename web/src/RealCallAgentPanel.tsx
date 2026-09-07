import { Bot, Check, ChevronRight, CircleAlert, Clipboard, FlaskConical, LoaderCircle } from 'lucide-react'
import { useEffect, useState } from 'react'

import { diagnoseRealCallExperiment } from './realCallDiagnostics'
import type { RealCallExperiment } from './types'

type Props = {
  apiBase: string
  compareRunId: string
  onUseRuns: (primaryRunId: string, compareRunId: string) => void
  primaryRunId: string
}

const BASELINE_COMMAND = './scripts/asterisk-local gemini --collect-rtcp --experiment-condition no-interruption'
const BARGE_IN_COMMAND = './scripts/asterisk-local gemini --collect-rtcp --experiment-condition intentional-barge-in'

export function RealCallAgentPanel({ apiBase, compareRunId, onUseRuns, primaryRunId }: Props) {
  const [primary, setPrimary] = useState(primaryRunId)
  const [compare, setCompare] = useState(compareRunId)
  const [result, setResult] = useState<RealCallExperiment | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  useEffect(() => setPrimary(primaryRunId), [primaryRunId])
  useEffect(() => setCompare(compareRunId), [compareRunId])

  async function investigate() {
    if (!primary.trim()) return
    setPending(true)
    setError(null)
    try {
      const next = await diagnoseRealCallExperiment(apiBase, primary.trim(), compare.trim())
      setResult(next)
      if (compare.trim()) onUseRuns(primary.trim(), compare.trim())
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Investigation failed')
    } finally {
      setPending(false)
    }
  }

  async function copyCommand(label: string, command: string) {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(label)
    } catch {
      setError('Could not copy the command. Check clipboard permissions.')
    }
  }

  const nextAction = result?.next_actions[0]
  return (
    <section className="realCallAgent">
      <div className="agentHeading">
        <span className="agentMark"><Bot size={18} /></span>
        <div>
          <span className="eyebrow">Ask VoxBench</span>
          <h2>Reproduce a real-call issue</h2>
        </div>
      </div>
      <p className="agentIntro">
        Run one quiet baseline and one intentional interruption. VoxBench checks that the
        comparison is controlled before drawing a conclusion.
      </p>

      <div className="agentFlow" aria-label="Experiment progress">
        <FlowStep done={Boolean(primary.trim())} label="Baseline" />
        <ChevronRight size={15} />
        <FlowStep done={Boolean(compare.trim())} label="Barge-in" />
        <ChevronRight size={15} />
        <FlowStep done={result?.status === 'ready'} label="Evidence" />
      </div>

      <div className="agentRunInputs">
        <label>
          Baseline run
          <input onChange={(event) => setPrimary(event.target.value)} placeholder="no-interruption run ID" value={primary} />
        </label>
        <label>
          Barge-in run
          <input onChange={(event) => setCompare(event.target.value)} placeholder="intentional-barge-in run ID" value={compare} />
        </label>
      </div>
      <button className="agentPrimaryAction" disabled={!primary.trim() || pending} onClick={() => void investigate()} type="button">
        {pending ? <LoaderCircle className="spin" size={16} /> : <FlaskConical size={16} />}
        {pending ? 'Checking evidence' : compare.trim() ? 'Compare evidence' : 'Check baseline'}
      </button>

      {error ? <div className="agentNotice error" role="alert"><CircleAlert size={16} />{error}</div> : null}
      {result ? (
        <div className="agentResult" aria-live="polite">
          <div className={`agentVerdict ${result.status}`}>
            {result.status === 'ready' ? <Check size={17} /> : <CircleAlert size={17} />}
            <div><strong>{result.status === 'ready' ? 'Comparison ready' : result.status === 'needs-compare' ? 'One run remaining' : 'More evidence needed'}</strong><span>{result.summary}</span></div>
          </div>
          <div className="agentCriteria">
            {result.criteria.map((item) => (
              <div className={`agentCriterion ${item.status}`} key={item.key}>
                <span>{item.status === 'pass' ? <Check size={14} /> : <CircleAlert size={14} />}</span>
                <div><strong>{item.label}</strong><small>{item.detail}</small></div>
              </div>
            ))}
          </div>
          {result.findings.length ? (
            <div className="agentFindings">
              {result.findings.map((finding) => (
                <article key={`${finding.title}-${finding.run_role}`}>
                  <span>{finding.classification}</span>
                  <strong>{finding.title}</strong>
                  <p>{finding.detail}</p>
                </article>
              ))}
            </div>
          ) : null}
          {nextAction ? <div className="agentNext"><span>Next</span><strong>{nextAction}</strong></div> : null}
        </div>
      ) : null}

      <details className="agentSetup">
        <summary>Call setup and commands</summary>
        <div>
          <p>Start the local stack, then run each bridge command in its own terminal. Telephone and spoken interruption remain operator-confirmed steps.</p>
          <CommandRow command={'PATH="$PWD/.venv/bin:$PATH" ./scripts/dev-demo'} copied={copied} label="Local stack" onCopy={copyCommand} />
          <CommandRow command="./scripts/asterisk-local up" copied={copied} label="Asterisk" onCopy={copyCommand} />
          <CommandRow command={BASELINE_COMMAND} copied={copied} label="Baseline" onCopy={copyCommand} />
          <CommandRow command={BARGE_IN_COMMAND} copied={copied} label="Barge-in" onCopy={copyCommand} />
        </div>
      </details>
    </section>
  )
}

function FlowStep({ done, label }: { done: boolean; label: string }) {
  return <span className={done ? 'done' : ''}>{done ? <Check size={13} /> : <i />}{label}</span>
}

function CommandRow({ command, copied, label, onCopy }: { command: string; copied: string | null; label: string; onCopy: (label: string, command: string) => Promise<void> }) {
  return (
    <div className="agentCommandRow">
      <div><strong>{label}</strong><code>{command}</code></div>
      <button aria-label={`Copy ${label} command`} onClick={() => void onCopy(label, command)} title={`Copy ${label} command`} type="button">
        {copied === label ? <Check size={15} /> : <Clipboard size={15} />}
      </button>
    </div>
  )
}
