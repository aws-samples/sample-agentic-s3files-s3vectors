import type { ValidationResult } from '../types'

// Shows what the validator's Amazon S3 Vectors lookup found for one invoice:
// the vendor's historical average, the closest prior invoices, and any amount
// anomaly or near-duplicate that fired. This is where vendor memory surfaces.

function money(n?: number | null) {
  if (n === undefined || n === null || Number.isNaN(n)) return '—'
  return `$${Math.round(n).toLocaleString()}`
}

const STATUS_LABEL: Record<string, string> = {
  PASS: 'Passed',
  FLAGGED: 'Flagged',
  REJECTED: 'Rejected',
}

export function MemoryCard({ result }: { result: ValidationResult }) {
  const anomaly = result.anomaly?.flagged
  const duplicate = result.duplicate?.flagged
  const flagged = anomaly || duplicate
  const matches = result.history?.matches ?? []
  const hasHistory = (result.history?.count ?? 0) > 0

  return (
    <div className={`mem-card ${flagged ? 'is-flagged' : 'is-ok'}`}>
      <div className="mem-head">
        <span className="mem-eyebrow">Validator · Vendor memory</span>
        <span className={`mem-pill status-${result.validation_status}`}>{STATUS_LABEL[result.validation_status] ?? result.validation_status}</span>
      </div>
      <p className="mem-sub">
        Before deciding, the validator embeds this invoice and asks <strong>Amazon S3 Vectors</strong> what
        is normal for <strong>{result.vendor}</strong> — comparing it against the vendor's history with a
        single similarity query, no separate database.
      </p>

      {hasHistory && (
        <div className="mem-stats">
          <div className="mem-stat">
            <span className="mem-stat-label">This invoice</span>
            <span className="mem-stat-value">{money(result.amount)}</span>
          </div>
          <div className="mem-stat">
            <span className="mem-stat-label">Vendor average</span>
            <span className="mem-stat-value">{money(result.history?.average)}</span>
          </div>
          <div className="mem-stat">
            <span className="mem-stat-label">Prior invoices</span>
            <span className="mem-stat-value">{result.history?.count ?? 0}</span>
          </div>
        </div>
      )}

      {anomaly && (
        <div className="mem-flag is-anomaly">
          <span className="mem-flag-tag">Amount anomaly</span>
          <span className="mem-flag-text">
            {money(result.amount)} is {result.anomaly?.factor ?? '?'}x {result.vendor}&apos;s historical
            average of {money(result.history?.average)}.
          </span>
        </div>
      )}
      {duplicate && (
        <div className="mem-flag is-duplicate">
          <span className="mem-flag-tag">Duplicate submission</span>
          <span className="mem-flag-text">
            Invoice number {result.duplicate?.matched_invoice_number} from {result.vendor} is already
            in memory{result.duplicate?.matched_key ? ` (${result.duplicate?.matched_key})` : ''}. This looks like a re-submission.
          </span>
        </div>
      )}

      {hasHistory && matches.length > 0 && (
        <div className="mem-matches">
          <div className="mem-matches-head">Closest prior invoices</div>
          {matches.map((m) => (
            <div className="mem-match" key={m.key}>
              <span className="mem-match-key">{m.key}</span>
              <span className="mem-match-amount">{money(m.amount)}</span>
              <span className="mem-match-dist">distance {m.distance}</span>
            </div>
          ))}
        </div>
      )}

      {!hasHistory && (
        <div className="mem-note">
          No prior history for {result.vendor} yet — this is the first invoice seen for this vendor, so there is no
          baseline to compare against. Once the embedder stores it, the next one will have memory to check.
        </div>
      )}

      {result.recommendation && <div className="mem-rec">{result.recommendation}</div>}
    </div>
  )
}
