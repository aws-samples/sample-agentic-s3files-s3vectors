// InvoiceIQ — types mirroring the pipeline-api JSON (which is just the files the
// agents leave on the S3 Files mount).

export interface StageFile {
  name: string
  id: string
  size: number
  modified: string
}

export interface Stage {
  id: string
  name: string
  agent: string
  kind: 'pdf' | 'json'
  count: number
  files: StageFile[]
}

export interface PipelineState {
  stages: Stage[]
  flagged: { count: number; files: StageFile[] }
  timestamp: string
}

export interface HistoryMatch {
  key: string
  amount: number
  distance: number
}

export interface ValidationResult {
  invoice_id: string
  vendor: string
  amount: number
  category?: string
  validation_status: 'PASS' | 'FLAGGED' | 'REJECTED'
  checks?: Record<string, boolean>
  history?: { average: number | null; count: number; matches: HistoryMatch[] }
  anomaly?: { flagged: boolean; factor?: number | null; average?: number | null; current_amount?: number }
  duplicate?: { flagged: boolean; matched_invoice_number?: string | null; matched_key?: string | null }
  recommendation?: string
  validated_at?: string
}

export interface InvoiceJourney {
  id: string
  stages: {
    incoming: boolean
    tasks: Record<string, unknown> | null
    classified: Record<string, unknown> | null
    extracted: Record<string, unknown> | null
    validated: ValidationResult | null
    memory: Record<string, unknown> | null
    flagged: ValidationResult | null
  }
}

// Pipeline stage order, used by the flow diagram.
export const STAGE_ORDER = ['incoming', 'tasks', 'classified', 'extracted', 'validated', 'memory'] as const

// One agent's recent output, surfaced as a CLI-style activity stream.
export interface AgentActivityItem {
  id: string
  modified: string
  data: Record<string, unknown> | null
}

export interface AgentActivity {
  stage: string
  agent: string
  items: AgentActivityItem[]
}

// What the embedder has stored in S3 Vectors (memory/ markers), surfaced when
// you click the S3 Vectors node.
export interface MemoryItem {
  invoice_id?: string
  invoice_number?: string
  vendor?: string
  amount?: number
  category?: string
  outcome?: string
  remembered_at?: string
  embedded_text?: string
  vector_dim?: number
  vector_preview?: number[]
}

export interface MemoryState {
  count: number
  items: MemoryItem[]
}
