import type { PipelineState } from '../types'

// The live pipeline diagram. Six stages, each backed by a directory on the S3
// Files mount. An edge "flows" (animated dot) when an upstream stage holds more
// files than the one after it, i.e. work is in transit. A node pulses when it
// holds unconsumed work OR its file count just changed (a real activity signal
// passed in from the poller). Each node carries small badges showing what the
// agent uses — an LLM, S3 Vectors, embeddings, or plain file I/O — and clicking
// a node opens that agent's activity stream. The Validator and Embedder both
// wire to the Amazon S3 Vectors memory node.

const C = {
  upload: '#8b949e',
  orchestrator: '#0972d3',
  classifier: '#ff9900',
  extractor: '#8c4fff',
  validator: '#037f0c',
  embedder: '#d97706',
  vectors: '#8c4fff',
  line: '#d6dbe1',
  ink: '#0f1b2a',
  soft: '#5f6b7a',
  mute: '#9aa4b2',
  surface: '#ffffff',
}

// Badge palette — what each agent relies on.
const BADGE_COLOR: Record<string, string> = {
  LLM: '#00a4b4', // Bedrock Nova reasoning
  VECTORS: '#8c4fff', // Amazon S3 Vectors memory
  EMBED: '#d97706', // Titan embeddings
  FILE: '#8b949e', // plain file I/O
  EVENT: '#5f6b7a', // EventBridge + SQS trigger
}

interface NodeDef {
  id: string
  agent: string
  label: string
  color: string
  badges: string[]
}

const NODES: NodeDef[] = [
  { id: 'incoming', agent: 'S3 Upload', label: 'incoming/', color: C.upload, badges: ['FILE'] },
  { id: 'tasks', agent: 'Orchestrator', label: 'tasks/', color: C.orchestrator, badges: ['EVENT'] },
  { id: 'classified', agent: 'Classifier', label: 'classified/', color: C.classifier, badges: ['LLM'] },
  { id: 'extracted', agent: 'Extractor', label: 'extracted/', color: C.extractor, badges: ['LLM'] },
  { id: 'validated', agent: 'Validator', label: 'validated/', color: C.validator, badges: ['LLM', 'VECTORS'] },
  { id: 'memory', agent: 'Embedder', label: 'memory/', color: C.embedder, badges: ['EMBED', 'VECTORS'] },
]

const NW = 138
const NH = 80
const GAP = 26
const Y = 26

function nodeX(i: number) {
  return 8 + i * (NW + GAP)
}

function Edge({ x1, x2, y, flowing, color }: { x1: number; x2: number; y: number; flowing: boolean; color: string }) {
  const d = `M${x1},${y} H${x2}`
  return (
    <g>
      <path d={d} stroke={flowing ? color : C.line} strokeWidth={flowing ? 1.6 : 1.2} fill="none" strokeDasharray={flowing ? '0' : '4 4'} />
      {flowing && (
        <circle r="3.5" fill={color}>
          <animateMotion dur="1.1s" repeatCount="indefinite" path={d} />
        </circle>
      )}
    </g>
  )
}

// Small pill badges along the bottom of a node.
function Badges({ x, y, badges }: { x: number; y: number; badges: string[] }) {
  let cursor = x
  return (
    <g>
      {badges.map((b) => {
        const w = b.length * 5.6 + 11
        const el = (
          <g key={b}>
            <rect x={cursor} y={y} width={w} height={13} rx={6.5} fill={`${BADGE_COLOR[b]}1a`} stroke={BADGE_COLOR[b]} strokeWidth="0.8" />
            <text x={cursor + w / 2} y={y + 9.3} textAnchor="middle" fontFamily="'Open Sans',sans-serif" fontSize="7" fontWeight="700" letterSpacing="0.04em" fill={BADGE_COLOR[b]}>
              {b}
            </text>
          </g>
        )
        cursor += w + 5
        return el
      })}
    </g>
  )
}

export function PipelineFlow({
  pipeline,
  activity,
  selectedAgent,
  onSelectAgent,
}: {
  pipeline: PipelineState | null
  activity?: Record<string, boolean>
  selectedAgent?: string | null
  onSelectAgent?: (stageId: string) => void
}) {
  const counts: Record<string, number> = {}
  for (const s of pipeline?.stages ?? []) counts[s.id] = s.count
  const flagged = pipeline?.flagged.count ?? 0

  const width = nodeX(NODES.length - 1) + NW + 8

  // An edge i->i+1 flows when the upstream stage has more files than the next.
  const edgeFlowing = (i: number) => (counts[NODES[i].id] ?? 0) > (counts[NODES[i + 1].id] ?? 0)
  // A node is "active" when it holds work the next stage has not consumed, OR
  // its own count just changed between polls (a file just arrived or left).
  const nodeActive = (i: number) => {
    const id = NODES[i].id
    const backlog = i < NODES.length - 1 ? (counts[id] ?? 0) > (counts[NODES[i + 1].id] ?? 0) : false
    return backlog || !!activity?.[id]
  }

  const vx = nodeX(4) + NW / 2 // under validator
  const ex = nodeX(5) + NW / 2 // under embedder
  const vyTop = Y + NH
  const vecY = 196
  // The validator queries S3 Vectors while it is validating — i.e. when there is
  // work waiting in extracted/ that it has not turned into a verdict yet, or a
  // verdict was just written. The embedder writes to S3 Vectors when there are
  // verdicts in validated/ it has not stored in memory/ yet. Keeping these tied
  // to each agent's own work stops both links lighting up on the same condition.
  const validatorQuerying = (counts.extracted ?? 0) > (counts.validated ?? 0) || !!activity?.validated
  const embedderStoring = (counts.validated ?? 0) > (counts.memory ?? 0) || !!activity?.memory

  return (
    <svg viewBox={`0 0 ${width} 278`} width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="InvoiceIQ pipeline">
      {/* stage-to-stage edges */}
      {NODES.slice(0, -1).map((_n, i) => (
        <Edge key={`e${i}`} x1={nodeX(i) + NW} x2={nodeX(i + 1)} y={Y + NH / 2} flowing={edgeFlowing(i)} color={NODES[i].color} />
      ))}

      {/* Validator <-> S3 Vectors (query history / matches back) */}
      <path d={`M${vx - 14},${vyTop} V${vecY}`} stroke={validatorQuerying ? C.vectors : C.line} strokeWidth={validatorQuerying ? 1.6 : 1.2} fill="none" strokeDasharray={validatorQuerying ? '0' : '4 4'} />
      {validatorQuerying && (
        <circle r="3.5" fill={C.vectors}><animateMotion dur="1.3s" repeatCount="indefinite" path={`M${vx - 14},${vyTop} V${vecY}`} /></circle>
      )}
      {/* Embedder -> S3 Vectors (store) */}
      <path d={`M${ex},${vyTop} V${vecY}`} stroke={embedderStoring ? C.embedder : C.line} strokeWidth={embedderStoring ? 1.6 : 1.2} fill="none" strokeDasharray={embedderStoring ? '0' : '4 4'} />
      {embedderStoring && (
        <circle r="3.5" fill={C.embedder}><animateMotion dur="1.3s" repeatCount="indefinite" path={`M${ex},${vyTop} V${vecY}`} /></circle>
      )}

      {/* stage nodes */}
      {NODES.map((n, i) => {
        const active = nodeActive(i)
        const count = counts[n.id] ?? 0
        const x = nodeX(i)
        const selected = selectedAgent === n.id
        return (
          <g key={n.id} style={{ cursor: 'pointer' }} onClick={() => onSelectAgent?.(n.id)}>
            {active && (
              <rect x={x - 2} y={Y - 2} width={NW + 4} height={NH + 4} rx={11} fill="none" stroke={n.color} strokeWidth="1" opacity="0.35">
                <animate attributeName="opacity" values="0.15;0.5;0.15" dur="1.5s" repeatCount="indefinite" />
              </rect>
            )}
            <rect
              x={x}
              y={Y}
              width={NW}
              height={NH}
              rx={10}
              fill={count > 0 ? `${n.color}0d` : C.surface}
              stroke={selected ? n.color : count > 0 ? n.color : C.line}
              strokeWidth={selected ? 2.4 : 1.2}
            />
            <text x={x + 12} y={Y + 18} fontFamily="'Open Sans',sans-serif" fontSize="8.5" fontWeight="700" letterSpacing="0.05em" fill={count > 0 ? n.color : C.mute}>
              {n.agent.toUpperCase()}
            </text>
            <text x={x + 12} y={Y + 37} fontFamily="ui-monospace,Menlo,monospace" fontSize="11" fill={C.soft}>{n.label}</text>
            <text x={x + 12} y={Y + 53} fontFamily="'Open Sans',sans-serif" fontSize="9.5" fill={C.mute}>
              {count} {count === 1 ? 'file' : 'files'}
            </text>
            <Badges x={x + 12} y={Y + 61} badges={n.badges} />
          </g>
        )
      })}

      {/* S3 Vectors memory node (right edge capped to the embedder node so it
          stays inside the viewBox). Clickable — opens the memory view. */}
      <g style={{ cursor: 'pointer' }} onClick={() => onSelectAgent?.('vectors')}>
        <rect x={vx - 90} y={vecY} width={nodeX(5) + NW - (vx - 90)} height={56} rx={10} fill={`${C.vectors}0d`} stroke={C.vectors} strokeWidth={selectedAgent === 'vectors' ? 2.4 : 1.2} />
        <text x={vx - 78} y={vecY + 20} fontFamily="'Open Sans',sans-serif" fontSize="8.5" fontWeight="700" letterSpacing="0.05em" fill={C.vectors}>
          AMAZON S3 VECTORS
        </text>
        <text x={vx - 78} y={vecY + 38} fontFamily="ui-monospace,Menlo,monospace" fontSize="11" fill={C.soft}>invoice-history</text>
        <text x={vx - 78} y={vecY + 50} fontFamily="'Open Sans',sans-serif" fontSize="9" fill={C.mute}>vendor memory · queried by validator, written by embedder</text>
      </g>

      {/* flagged tally */}
      {flagged > 0 && (
        <text x={nodeX(4) + NW / 2} y={Y + NH + 16} textAnchor="middle" fontFamily="'Open Sans',sans-serif" fontSize="9.5" fontWeight="700" fill="#d91515">
          {flagged} flagged for review
        </text>
      )}
    </svg>
  )
}
