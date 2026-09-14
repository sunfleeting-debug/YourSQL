/** 存储检查页面、页面地图、缓存与索引快照的响应模型。 */

import type { JsonObject, JsonValue } from './common'
import type { IndexMeta } from './catalog'

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
  row: JsonValue[] | null
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
  catalog?: JsonValue
  metadata?: JsonValue
  index_node?: JsonObject
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
  system_tables?: JsonObject[] | 'MASKED'
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

export interface StorageResizeChange {
  snapshot_at: string
  changed: boolean
  previous_capacity: number
  capacity: number
  evicted_pages: number
  buffer_pool: BufferPoolSnapshot
  note: string
}

export interface StorageIndexSnapshot {
  snapshot_at: string
  readonly: boolean
  indexes: IndexMeta[]
}
