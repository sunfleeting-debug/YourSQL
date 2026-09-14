/** SQL 查询、执行流水线与历史记录的响应模型。 */

import type { DBError, JsonValue } from './common'

export type QueryTaskStatus = 'queued' | 'running' | 'success' | 'error' | 'cancelled' | 'timeout'
export type QueryResultStatus = 'success' | 'error'
export type StageStatus = 'success' | 'error' | 'unsupported' | 'partial'
export type ColumnTypeSource = 'schema' | 'runtime'
export type ResultViewMode = 'table' | 'messages' | 'statistics' | 'history' | 'json'

export interface Source {
  start: number
  end: number
  line: number
  column: number
}

export interface Stage {
  name: string
  status: StageStatus
  data: JsonValue
  text: string
  duration_ms: number | null
  reason: string | null
  error: DBError | null
  source: Source
  truncated?: boolean
}

export interface PlanEstimate {
  startup_cost: number
  total_cost: number
  rows: number
  unit: string
  model: string
}

export interface QueryColumn {
  name: string
  type: string
  type_source: ColumnTypeSource
}

export interface QueryResult {
  sql: string
  source: Source
  status: QueryResultStatus
  columns: QueryColumn[]
  rows: JsonValue[][]
  affected_rows: number
  total_rows: number
  retained_rows: number
  truncated: boolean
  elapsed_ms: number
  execution_ms?: number
  message?: string
  error?: DBError
  plan?: JsonValue
  plan_estimate?: PlanEstimate
  stats?: JsonValue
  stages: Stage[]
  offset: number
  limit: number
}

export interface QueryTask {
  id: string
  status: QueryTaskStatus
  results: QueryResult[]
  error: DBError | null
  elapsed_ms: number
  submitted_at: string
  cancel_requested: boolean
}

export interface HistoryEntry {
  id: string
  task_id: string
  sql: string
  status: QueryResultStatus
  elapsed_ms: number
  executed_at: string
  error_summary: string | null
}

export interface History {
  items: HistoryEntry[]
  total: number
  retention: string
}
