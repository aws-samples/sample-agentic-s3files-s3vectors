import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { PipelineFlow } from './components/PipelineFlow'
import { InvoiceDetail } from './components/InvoiceDetail'
import { AgentConsole } from './components/AgentConsole'
import { MemoryView } from './components/MemoryView'
import { STAGE_ORDER, type InvoiceJourney, type PipelineState } from './types'
import './App.css'

const POLL_MS = 1500

interface InvoiceRow {
  id: string
  stageIndex: number
  flagged: boolean
  done: boolean
}

function usePipeline(): { state: PipelineState | null; activity: Record<string, boolean> } {
  const [state, setState] = useState<PipelineState | null>(null)
  const [activity, setActivity] = useState<Record<string, boolean>>({})
  const prev = useRef<Record<string, number>>({})
  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/pipeline', { cache: 'no-store' })
        const data = (await res.json()) as PipelineState
        if (cancelled) return
        // Mark a stage active when its file count changed since the last poll —
        // a file just arrived or moved on — so even a single fast invoice blinks.
        const changed: Record<string, boolean> = {}
        for (const s of data.stages) {
          if (prev.current[s.id] !== undefined && prev.current[s.id] !== s.count) changed[s.id] = true
          prev.current[s.id] = s.count
        }
        setState(data)
        setActivity(changed)
      } catch {
        /* keep last state, retry next tick */
      }
    }
    poll()
    const t = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])
  return { state, activity }
}

function useJourney(id: string | null): InvoiceJourney | null {
  const [journey, setJourney] = useState<InvoiceJourney | null>(null)
  useEffect(() => {
    setJourney(null)
    if (!id) return
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch(`/api/invoice/${encodeURIComponent(id)}`, { cache: 'no-store' })
        const data = (await res.json()) as InvoiceJourney
        if (!cancelled) setJourney(data)
      } catch {
        /* retry */
      }
    }
    poll()
    const t = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [id])
  return journey
}

function App() {
  const { state: pipeline, activity } = usePipeline()
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const journey = useJourney(selected)
  const fileInput = useRef<HTMLInputElement>(null)

  const invoices = useMemo<InvoiceRow[]>(() => {
    if (!pipeline) return []
    const flaggedIds = new Set(pipeline.flagged.files.map((f) => f.id))
    const byId = new Map<string, InvoiceRow>()
    pipeline.stages.forEach((stage) => {
      const idx = STAGE_ORDER.indexOf(stage.id as (typeof STAGE_ORDER)[number])
      stage.files.forEach((f) => {
        const row = byId.get(f.id) ?? { id: f.id, stageIndex: -1, flagged: false, done: false }
        row.stageIndex = Math.max(row.stageIndex, idx)
        if (stage.id === 'validated' || stage.id === 'memory') row.done = true
        byId.set(f.id, row)
      })
    })
    flaggedIds.forEach((id) => {
      const row = byId.get(id)
      if (row) row.flagged = true
    })
    return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id))
  }, [pipeline])

  // auto-select the most-recently-flagged or first invoice once data arrives
  useEffect(() => {
    if (!selected && invoices.length) {
      const flagged = invoices.find((i) => i.flagged)
      setSelected((flagged ?? invoices[0]).id)
    }
  }, [invoices, selected])

  const onUpload = useCallback(async (file: File) => {
    const id = file.name.replace(/\.pdf$/i, '').replace(/[^A-Za-z0-9._-]/g, '-')
    await fetch(`/api/upload/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/pdf' },
      body: file,
    })
    setSelected(id)
  }, [])

  return (
    <div className="iq-app">
      <header className="iq-header">
        <span className="iq-brand">
          <span className="iq-cube" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M3 7.5L12 3l9 4.5v9L12 21l-9-4.5v-9z" stroke="#FF9900" strokeWidth="1.6" strokeLinejoin="round" />
              <path d="M3 7.5L12 12l9-4.5M12 12v9" stroke="#FF9900" strokeWidth="1.6" strokeLinejoin="round" />
            </svg>
          </span>
          <span className="iq-title">InvoiceIQ <span className="iq-light">· Agentic Invoice Processing on Amazon S3 Files</span></span>
        </span>
        <span className="iq-actions">
          <button className="iq-upload-btn" onClick={() => fileInput.current?.click()}>Upload invoice</button>
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) onUpload(f)
              e.target.value = ''
            }}
          />
        </span>
      </header>

      <section className="iq-pipeline-card">
        <div className="iq-card-title">Live pipeline <span className="iq-card-sub">— files moving through directories on the S3 Files mount · click an agent to watch it work</span></div>
        <PipelineFlow
          pipeline={pipeline}
          activity={activity}
          selectedAgent={selectedAgent}
          onSelectAgent={(id) => setSelectedAgent((cur) => (cur === id ? null : id))}
        />
        <div className="iq-legend">
          <span className="iq-legend-item"><i className="iq-chip" style={{ background: '#00a4b41a', borderColor: '#00a4b4', color: '#00a4b4' }}>LLM</i> Bedrock Nova reasoning</span>
          <span className="iq-legend-item"><i className="iq-chip" style={{ background: '#8c4fff1a', borderColor: '#8c4fff', color: '#8c4fff' }}>VECTORS</i> S3 Vectors memory</span>
          <span className="iq-legend-item"><i className="iq-chip" style={{ background: '#d977061a', borderColor: '#d97706', color: '#d97706' }}>EMBED</i> Titan embeddings</span>
          <span className="iq-legend-item"><i className="iq-chip" style={{ background: '#5f6b7a1a', borderColor: '#5f6b7a', color: '#5f6b7a' }}>EVENT</i> EventBridge + SQS</span>
          <span className="iq-legend-item"><i className="iq-chip" style={{ background: '#8b949e1a', borderColor: '#8b949e', color: '#8b949e' }}>FILE</i> plain file I/O</span>
        </div>
      </section>

      {selectedAgent && (
        <section className="iq-console-card">
          {selectedAgent === 'vectors' ? (
            <MemoryView onClose={() => setSelectedAgent(null)} />
          ) : (
            <AgentConsole stageId={selectedAgent} onClose={() => setSelectedAgent(null)} />
          )}
        </section>
      )}

      <section className="iq-lower">
        <div className="iq-list-card">
          <div className="iq-card-title">Invoices</div>
          {invoices.length === 0 && <div className="iq-list-empty">No invoices yet. Upload one, or drop a PDF in the S3 bucket&apos;s <code>incoming/</code> prefix.</div>}
          <ul className="iq-list">
            {invoices.map((row) => (
              <li
                key={row.id}
                className={`iq-list-row ${selected === row.id ? 'is-selected' : ''}`}
                onClick={() => setSelected(row.id)}
              >
                <span className="iq-list-id">{row.id}</span>
                <span className="iq-list-stage">{STAGE_ORDER[row.stageIndex] ?? 'incoming'}</span>
                {row.flagged ? (
                  <span className="iq-badge flagged">review</span>
                ) : row.done ? (
                  <span className="iq-badge pass">passed</span>
                ) : (
                  <span className="iq-badge working">working</span>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div className="iq-detail-card">
          <InvoiceDetail journey={journey} />
        </div>
      </section>
    </div>
  )
}

export default App
