import { useEffect, useMemo, useState } from 'react'
import type { MemoryItem, MemoryState } from '../types'

// What's inside the vendor memory. Polls the embedder's stored markers and
// summarises Amazon S3 Vectors the way the validator sees it: per vendor, how
// many invoices are remembered and what their approved average is — the exact
// baseline an incoming invoice is checked against.

const POLL_MS = 2000

interface VendorRoll {
  vendor: string
  count: number
  approvedAvg: number | null
  items: MemoryItem[]
}

function money(n?: number) {
  if (n === undefined || n === null || Number.isNaN(n)) return '—'
  return `$${Math.round(Number(n)).toLocaleString()}`
}

export function MemoryView({ onClose }: { onClose: () => void }) {
  const [mem, setMem] = useState<MemoryState | null>(null)
  const [open, setOpen] = useState<Record<string, boolean>>({})

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/memory', { cache: 'no-store' })
        const data = (await res.json()) as MemoryState
        if (!cancelled) setMem(data)
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
  }, [])

  const vendors = useMemo<VendorRoll[]>(() => {
    const items = mem?.items ?? []
    const byVendor = new Map<string, MemoryItem[]>()
    for (const it of items) {
      const v = it.vendor ?? 'unknown'
      byVendor.set(v, [...(byVendor.get(v) ?? []), it])
    }
    return [...byVendor.entries()]
      .map(([vendor, list]) => {
        const approved = list.filter((i) => String(i.outcome ?? '').toLowerCase() === 'approved' || String(i.outcome ?? '').toLowerCase() === 'pass')
        const approvedAvg = approved.length
          ? Math.round(approved.reduce((s, i) => s + Number(i.amount ?? 0), 0) / approved.length)
          : null
        return { vendor, count: list.length, approvedAvg, items: list }
      })
      .sort((a, b) => b.count - a.count)
  }, [mem])

  const total = mem?.count ?? 0

  return (
    <div className="memview">
      <div className="memview-bar">
        <span className="memview-eyebrow">Amazon S3 Vectors</span>
        <span className="memview-index">invoice-history</span>
        <span className="memview-spacer" />
        <span className="memview-count">{total} embedded this run</span>
        <button className="memview-close" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <div className="memview-body">
        <div className="memview-facts">
          <span className="memview-fact"><b>1024</b> dimensions</span>
          <span className="memview-fact"><b>cosine</b> similarity</span>
          <span className="memview-fact">Titan Text Embeddings v2</span>
        </div>
        <p className="memview-sub">
          These are the invoices <strong>this pipeline has embedded</strong> into Amazon S3 Vectors so far. The
          validator queries this memory — together with a baseline of historical invoices seeded at setup (not
          shown here) — to compare each new invoice against the vendor's approved average. The averages below
          are computed from the invoices listed here, so they can differ from the validator's once the seeded
          history is included.
        </p>
        {vendors.length === 0 && <div className="memview-empty">No vectors stored yet. Once the embedder runs, vendors and their history appear here.</div>}
        <div className="memview-grid">
          {vendors.map((v) => (
            <div className="memview-vendor" key={v.vendor}>
              <div className="memview-vendor-head">
                <span className="memview-vendor-name">{v.vendor}</span>
                <span className="memview-vendor-count">{v.count} invoice{v.count === 1 ? '' : 's'}</span>
              </div>
              <div className="memview-avg">
                <span className="memview-avg-label">approved average</span>
                <span className="memview-avg-value">{money(v.approvedAvg ?? undefined)}</span>
              </div>
              <div className="memview-items">
                {v.items.map((it) => {
                  const ok = String(it.outcome ?? '').toLowerCase() === 'approved' || String(it.outcome ?? '').toLowerCase() === 'pass'
                  const key = it.invoice_id ?? it.invoice_number ?? ''
                  const isOpen = !!open[key]
                  return (
                    <div className="memview-item-wrap" key={key}>
                      <div className="memview-item" onClick={() => setOpen((o) => ({ ...o, [key]: !o[key] }))}>
                        <span className="memview-item-id">{isOpen ? '▾' : '▸'} {it.invoice_id ?? it.invoice_number}</span>
                        <span className="memview-item-amount">{money(it.amount)}</span>
                        <span className={`memview-item-outcome ${ok ? 'ok' : 'warn'}`}>{it.outcome ?? '—'}</span>
                      </div>
                      {isOpen && (
                        <div className="memview-embed">
                          <div className="memview-embed-row">
                            <span className="memview-embed-label">embedded text</span>
                            <code className="memview-embed-text">"{it.embedded_text ?? `${it.vendor} invoice total ${Math.round(Number(it.amount ?? 0))}`}"</code>
                          </div>
                          <div className="memview-embed-arrow">↓ Titan Text Embeddings v2</div>
                          <div className="memview-embed-row">
                            <span className="memview-embed-label">vector</span>
                            <code className="memview-embed-vec">
                              [{(it.vector_preview ?? []).map((x) => x.toFixed(4)).join(', ')}{(it.vector_preview?.length ?? 0) > 0 ? ', …' : ''}]
                              <span className="memview-embed-dim">float32 · {it.vector_dim ?? 1024} dims</span>
                            </code>
                          </div>
                          <div className="memview-embed-arrow">↓ stored with filterable metadata</div>
                          <div className="memview-embed-meta">
                            <span><b>key</b> {it.invoice_id}</span>
                            <span><b>invoice_number</b> {it.invoice_number ?? '—'}</span>
                            <span><b>category</b> {it.category ?? '—'}</span>
                            <span><b>outcome</b> {it.outcome ?? '—'}</span>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
