import { useEffect, useState } from 'react'
import type { AgentActivity, AgentActivityItem } from '../types'

// A CLI-style activity stream for one agent. It polls the agent's recent output
// files and renders each as a terminal line, so you can watch what the agent
// decided. Everything shown was read straight off the S3 Files mount — the same
// files the next agent in the pipeline reads.

const POLL_MS = 1500

// Turn one output file into a CLI line (and an optional second "thinking" line).
function format(stage: string, item: AgentActivityItem): { line: string; think?: string } {
  const d = (item.data ?? {}) as Record<string, unknown>
  const n = (v: unknown) => `$${Number(v ?? 0).toLocaleString()}`
  switch (stage) {
    case 'incoming':
      return { line: `uploaded ${item.id}.pdf` }
    case 'tasks':
      return { line: `${item.id}  → work assigned` }
    case 'classified':
      return {
        line: `${item.id}  → ${d.document_type ?? '?'} · ${d.vendor_name ?? '?'} · ${d.category ?? '?'}`,
        think: d.reasoning ? String(d.reasoning) : undefined,
      }
    case 'extracted':
      return {
        line: `${item.id}  → ${n(d.amount)} · ${d.invoice_number ?? ''}`,
        think: d.reasoning ? String(d.reasoning) : undefined,
      }
    case 'validated': {
      const status = String(d.validation_status ?? '?')
      const anomaly = d.anomaly as { flagged?: boolean; factor?: number } | undefined
      const dup = d.duplicate as { flagged?: boolean; matched_invoice_number?: string } | undefined
      let why = ''
      if (anomaly?.flagged) why = `  (${anomaly.factor}× vendor avg)`
      else if (dup?.flagged) why = `  (duplicate of ${dup.matched_invoice_number})`
      return { line: `${item.id}  → ${status}${why}`, think: d.recommendation ? String(d.recommendation) : undefined }
    }
    case 'memory':
      return { line: `${item.id}  → stored in memory (${d.outcome ?? 'approved'})` }
    default:
      return { line: item.id }
  }
}

const STATUS_CLASS: Record<string, string> = {
  PASS: 'ok',
  FLAGGED: 'warn',
  REJECTED: 'bad',
}

export function AgentConsole({ stageId, onClose }: { stageId: string; onClose: () => void }) {
  const [activity, setActivity] = useState<AgentActivity | null>(null)

  useEffect(() => {
    setActivity(null)
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch(`/api/agent/${encodeURIComponent(stageId)}`, { cache: 'no-store' })
        const data = (await res.json()) as AgentActivity
        if (!cancelled) setActivity(data)
      } catch {
        /* retry next tick */
      }
    }
    poll()
    const t = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [stageId])

  const agent = activity?.agent ?? stageId
  const items = activity?.items ?? []

  return (
    <div className="console">
      <div className="console-bar">
        <span className="console-dots" aria-hidden="true"><i /><i /><i /></span>
        <span className="console-title">{agent.toLowerCase()} — live activity</span>
        <button className="console-close" onClick={onClose} aria-label="Close console">✕</button>
      </div>
      <div className="console-body">
        <div className="console-line muted">$ tail -f /mnt/s3files/{stageId}/*.json</div>
        {items.length === 0 && <div className="console-line muted">…waiting for {agent.toLowerCase()} to write its first file</div>}
        {items.map((it) => {
          const f = format(stageId, it)
          const status =
            stageId === 'validated' ? String(((it.data ?? {}) as Record<string, unknown>).validation_status ?? '') : ''
          return (
            <div className="console-entry" key={it.id}>
              <div className={`console-line ${status ? STATUS_CLASS[status] ?? '' : ''}`}>
                <span className="console-prompt">›</span> {f.line}
              </div>
              {f.think && <div className="console-think">“{f.think}”</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
