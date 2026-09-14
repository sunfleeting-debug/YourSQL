/** 数据库目录、文件和 SQL 方言的响应模型。 */

import type { JsonValue, PayloadCodecName } from './common'

export interface Column {
  name: string
  type: string
  nullable: boolean
  primary_key: boolean
  unique: boolean
  default: { type: string; value: JsonValue } | null
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
  min_key: JsonValue[] | null
  max_key: JsonValue[] | null
}

export interface IndexEntry {
  key: JsonValue[]
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
  payload_codec: PayloadCodecName
  keywords: string[]
  types: string[]
  functions: string[]
  limitations: string[]
}
