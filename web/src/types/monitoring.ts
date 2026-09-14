/** 性能监控接口的响应模型。 */

import type { JsonValue } from './common'
import type { PlanEstimate, Stage } from './query'

export interface LatencySample {
  at: string
  total_ms: number
  slow: boolean
  status: string
}

export interface StorageEvent {
  at: string
  action: string
  page_id: number
  dirty: boolean
  writeback: boolean
  policy: string
  reason: string
}

export interface MonitorSummary {
  threshold_ms: number
  sampled_queries: number
  successful_queries: number
  failed_queries: number
  slow_queries: number
  avg_ms: number
  p50_ms: number
  p95_ms: number
  max_ms: number
  cache_hit_rate: number
  page_reads: number
  page_writes: number
  cache_hits: number
  cache_misses: number
  cache_evictions: number
  latency_series: LatencySample[]
  storage_events: StorageEvent[]
  storage_policy: string
  retention: string
  log_failures: number
}

export interface MonitorQuery {
  query_id: string
  task_id: string
  statement_index: number
  user: string
  sql: string
  status: string
  started_at: string
  finished_at: string
  total_ms: number
  queue_wait_ms: number
  compile_ms: number
  execute_ms: number
  materialize_ms: number
  rows_examined: number
  rows_returned: number
  affected_rows: number
  page_reads: number
  page_writes: number
  cache_hits: number
  cache_misses: number
  cache_evictions: number
  operator: string | null
  error_code: string | null
  slow: boolean
}

export interface MonitorDetail extends MonitorQuery {
  stages: Stage[]
  plan: JsonValue | null
  plan_estimate: PlanEstimate | null
}

export interface MonitorQueriesResponse {
  items: MonitorQuery[]
  total: number
  slow_only: boolean
  threshold_ms: number
}
