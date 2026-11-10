import { useState } from 'react'
import type { InvoiceJourney, ValidationResult } from '../types'
import { MemoryCard } from './MemoryCard'

// Shows one invoice's journey across the pipeline directories on the S3 Files
// mount, then the validator's memory verdict. Every value here was read from a
// file an agent wrote — and each row can expand to show that exact file, so the
// "trace the handoffs" idea is visible right in the UI.

interface Row {
  key: 'incoming' | 'tasks' | 'classified' | 'extracted' | 'validated' | 'memory'
  agent: string
  done: boolean
  detail: string
  raw: unknown
}

export function InvoiceDetail({ journey }: { journey: InvoiceJourney | null }) {
  const [open, setOpen] = useState<Record<string, boolean>>({})

  if (!journey) {
    return (
      <div className="detail empty">
        <div className="detail-empty-icon">📄</div>
        <p>Select an invoice to follow it through the pipeline, or upload one to start.</p>
      </div>
    )
  }

  const s = journey.stages
  const classified = s.classified as { document_type?: string; vendor_name?: string; category?: string } | null
  const extracted = s.extracted as { vendor?: string; amount?: number; invoice_number?: string } | null
  const validated = s.validated as ValidationResult | null

  const rows: Row[] = [
    { key: 'incoming', agent: 'Uploaded', done: s.incoming, detail: s.incoming ? 'PDF on the mount' : 'waiting', raw: null },
    { key: 'tasks', agent: 'Orchestrator', done: !!s.tasks, detail: s.tasks ? 'work assigned' : 'waiting', raw: s.tasks },
    {
      key: 'classified',
      agent: 'Classifier',
      done: !!classified,
      detail: classified ? `${classified.document_type ?? '?'} · ${classified.vendor_name ?? '?'}` : 'waiting',
      raw: s.classified,
    },
    {
      key: 'extracted',
      agent: 'Extractor',
      done: !!extracted,
      detail: extracted ? `$${Number(extracted.amount ?? 0).toLocaleString()} · ${extracted.invoice_number ?? ''}` : 'waiting',
      raw: s.extracted,
    },
    {
      key: 'validated',
      agent: 'Validator',
      done: !!validated,
      detail: validated ? validated.validation_status : 'waiting',
      raw: s.validated,
    },
    { key: 'memory', agent: 'Embedder', done: !!s.memory, detail: s.memory ? 'stored in memory' : 'waiting', raw: s.memory },
  ]

  return (
    <div className="detail">
      <div className="detail-head">
        <span className="detail-id">{journey.id}</span>
        {validated && <span className={`detail-status status-${validated.validation_status}`}>{validated.validation_status}</span>}
      </div>

      <ol className="journey">
        {rows.map((r) => {
          const isOpen = !!open[r.key]
          return (
            <li key={r.key} className={`journey-row ${r.done ? 'done' : 'pending'}`}>
              <div className="journey-main">
                <span className="journey-dot" />
                <span className="journey-agent">{r.agent}</span>
                <span className="journey-detail">{r.detail}</span>
                {r.raw != null && (
                  <button className="journey-raw-toggle" onClick={() => setOpen((o) => ({ ...o, [r.key]: !o[r.key] }))}>
                    {isOpen ? 'hide' : '{ } raw'}
                  </button>
                )}
              </div>
              {isOpen && r.raw != null && (
                <pre className="journey-raw">{JSON.stringify(r.raw, null, 2)}</pre>
              )}
            </li>
          )
        })}
      </ol>

      {validated && <MemoryCard result={validated} />}
    </div>
  )
}
