/** 后端响应模型：可视化只读取这些真实响应。 */

// —— 会话与错误 ——
export interface DBError {
  code: string
  message: string
  line?: number
  column?: number
  position_accuracy?: string
  position_note?: string
  suggestion?: { kind: 'keyword'; replacement: string }
}
export interface SessionInfo {
  user: string
  roles: string[]
  permissions: string[]
  database: string
  database_path?: string
  session_expires_in: number
  active_task: string | null
}

// —— 元数据与对象（表 / 视图 / 索引 / 数据库文件 / 方言） ——
export interface Column {
  name: string
  type: string
  nullable: boolean
  primary_key: boolean
  unique: boolean
  default: { type: string; value: unknown } | null
}
export interface IndexMeta {
  name: string
  columns: string[]
  unique: boolean
  index_type: string
  root_page_id: number | null
}
export interface IndexNode {
  page_id: number
  node_type: 'leaf' | 'internal'
  level: number
  parent_page_id: number | null
  next_page_id: number | null
  prev_page_id: number | null
  key_count: number
  child_count: number
  children: number[]
  min_key: unknown[] | null
  max_key: unknown[] | null
}
export interface IndexEntry {
  key: unknown[]
  row_ids: [number, number][]
  row_count: number
  rows_truncated: boolean
}
export interface IndexSnapshot {
  metadata: IndexMeta
  entries: IndexEntry[]
  total: number
  offset: number
  limit: number
  representation: string
  format: string
  physical: boolean
  unique: boolean
  root_page_id: number | null
  height: number
  page_count: number
  page_ids: number[]
  all_page_ids?: number[]
  pages_truncated: boolean
  nodes: IndexNode[]
  limitation?: string
}
export interface TableMeta {
  name: string
  columns: Column[]
  indexes: IndexMeta[]
  row_count: number
  page_ids: number[]
  create_sql: string
  system?: boolean
}
export interface ViewMeta {
  name: string
  columns: Column[]
  definition_sql: string
  create_sql: string
  system?: boolean
}
export interface Metadata {
  databases: { name: string; tables: TableMeta[]; views: ViewMeta[]; total: number; total_views?: number }[]
  single_database: boolean
  refreshed_at: string
}
export interface DatabaseFile {
  name: string
  path?: string
  size_bytes: number
  active: boolean
}
export interface DatabaseFiles {
  active: string
  active_path?: string
  files: DatabaseFile[]
  limit: number
  note: string
}
export interface DatabaseSwitch {
  database: string
  path?: string
  previous_database: string
  previous_path?: string
  changed: boolean
  requires_login: boolean
}
export interface Dialect {
  keywords: string[]
  types: string[]
  functions: string[]
  limitations: string[]
}

// —— 查询、执行流水线与历史 ——
export interface Source {
  start: number
  end: number
  line: number
  column: number
}
export interface Stage {
  name: string
  status: string
  data: unknown
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
export interface QueryResult {
  sql: string
  source: Source
  status: string
  columns: { name: string; type: string; type_source: string }[]
  rows: unknown[][]
  affected_rows: number
  total_rows: number
  retained_rows: number
  truncated: boolean
  elapsed_ms: number
  execution_ms?: number
  message?: string
  error?: DBError
  plan?: unknown
  plan_estimate?: PlanEstimate
  stats?: unknown
  stages: Stage[]
  offset: number
  limit: number
}
export interface QueryTask {
  id: string
  status: string
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
  status: string
  elapsed_ms: number
  executed_at: string
  error_summary: string | null
}
export interface History {
  items: HistoryEntry[]
  total: number
  retention: string
}

// —— 存储检查：页面地图、选中页详情、缓存与索引快照 ——
export type ReplacementPolicy = 'lru' | 'fifo'
export interface PageHeader {
  page_id: number
  type: string
  page_size: number
  payload_size: number
  free_space: number
  crc32: string
  masked?: boolean
  table_name?: string
  index_name?: string
  system_table?: boolean
  storage_format?: string
  slot_count?: number
  logical_used_space?: number
  logical_free_space?: number
  index_format?: string
  index_node_type?: string
  index_level?: number
  index_key_count?: number
}
export interface RawPayload {
  encoding: string
  size_bytes: number
  preview_bytes: number
  truncated: boolean
  text: string | null
  hex: string
  base64: string
  note: string
}
export interface StorageSlot {
  slot_id: number
  deleted: boolean
  record_bytes: number
  row: unknown[] | null
  page_offset?: number | null
  byte_length?: number
  storage_encoding?: string
  slot_directory_offset?: number | null
  slot_directory_length?: number
}
export interface StorageRegion {
  start: number
  end: number
  size: number
  direction?: string
}
export interface StorageLayout {
  format: string
  physical: boolean
  note?: string
  payload_offset?: number
  payload_capacity?: number
  inner_header_size?: number
  slot_entry_size?: number
  slot_count?: number
  slot_directory?: StorageRegion
  free_region?: StorageRegion
  free_regions?: StorageRegion[]
  record_region?: StorageRegion
  slots?: Array<{
    slot_id: number
    offset: number
    length: number
    deleted: boolean
    directory_offset?: number
    directory_length?: number
  }>
}
export interface StoragePageDetail extends PageHeader {
  readonly: boolean
  offset: number
  limit: number
  source: string
  mask_reason?: string
  header_size?: number
  magic?: string
  version?: number
  raw_payload?: RawPayload
  raw_page?: RawPayload
  slots?: StorageSlot[]
  total_slots?: number
  layout?: string
  physical_layout?: StorageLayout
  catalog?: unknown
  metadata?: unknown
  index_node?: Record<string, unknown>
  note?: string
}
export interface BufferPoolSnapshot {
  stats: Record<string, number>
  policy: ReplacementPolicy
  frames: {
    page_id: number
    type: string
    dirty: boolean
    pin_count: number
    loaded_order: number
    last_used: number
  }[]
  /** 当前可淘汰页号，数组下标越小表示越快被淘汰。 */
  eviction_order?: number[]
  total: number
  offset: number
  limit: number
  revision?: number
}
export interface StorageSnapshot {
  snapshot_at: string
  readonly: boolean
  total: number
  offset: number
  limit: number
  page_size: number
  map_only?: boolean
  pages: PageHeader[]
  files: { name: string; size_bytes: number }[]
  free_page_count: number
  free_pages: number[]
  system_tables?: Array<Record<string, unknown>> | 'MASKED'
  storage_revision?: number
  buffer_pool: BufferPoolSnapshot
  indexes: IndexMeta[]
  io: Record<string, number>
  limitations: string[]
}
export interface StoragePageChanges {
  snapshot_at: string
  readonly: boolean
  since: number
  revision: number
  changed_page_ids: number[]
  pages: PageHeader[]
  total?: number
  page_size?: number
  free_pages?: number[]
  free_page_count?: number
  truncated: boolean
  note?: string
}
export interface StorageCacheSnapshot {
  snapshot_at: string
  readonly: boolean
  buffer_pool: BufferPoolSnapshot
  io: Record<string, number>
  note: string
}
export interface StoragePolicyChange {
  snapshot_at: string
  changed: boolean
  previous_policy: ReplacementPolicy
  replacement_policy: ReplacementPolicy
  buffer_pool: BufferPoolSnapshot
  note: string
}
export interface StorageIndexSnapshot {
  snapshot_at: string
  readonly: boolean
  indexes: IndexMeta[]
}
