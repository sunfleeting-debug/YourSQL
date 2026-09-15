/** 存储工作区的状态模型、分页常量和纯展示计算。 */

import { api } from '../../api'
import type { JsonObject } from '../../types/common'
import type { TableMeta } from '../../types/catalog'
import type { PageHeader, RawPayload, StorageSnapshot, StorageSlot } from '../../types/storage'

export interface StoragePanelProps {
  fullMode?: boolean
  onExit?: () => void
  selectedTable?: TableMeta | null
  onSelectTable?: (tableName: string) => void
  active?: boolean
  refreshToken?: number
}

export type StorageTab = 'pages' | 'buffer' | 'indexes'
export type InspectableStorageKind = 'pages' | 'indexes'
export type StorageSelection = { kind: InspectableStorageKind; value: string }
export type PageUsageVisual = 'color' | 'fill'

export const STORAGE_TAB_OPTIONS: Array<readonly [StorageTab, string]> = [
  ['pages', '页面'],
  ['buffer', '缓存'],
  ['indexes', '索引']
]

export const PAGE_TYPE_LABELS: Record<string, string> = {
  superblock: '数据库元数据',
  catalog: '目录与权限',
  heap: '表记录与槽位',
  index: 'B+Tree 索引页',
  directory: '命名页目录',
  free: '可复用空闲页'
}

export const PAGE_TYPE_LEGEND = [
  { type: 'superblock', label: '元数据' },
  { type: 'catalog', label: '目录' },
  { type: 'heap', label: '表记录' },
  { type: 'index', label: '索引' },
  { type: 'directory', label: '命名目录' },
  { type: 'free', label: '空闲' }
]

export interface SnapshotProgress {
  loaded: number
  total: number
  snapshot: StorageSnapshot
}

const STORAGE_PAGE_BATCH = 500

/** 分批补齐页头；fields=map 只取画图所需字段，表/索引标签在选中页按需获取。 */
export async function loadStorageSnapshot(onProgress?: (progress: SnapshotProgress) => void): Promise<StorageSnapshot> {
  const first = await api<StorageSnapshot>(`/api/storage?offset=0&limit=${STORAGE_PAGE_BATCH}&fields=map`)
  const pages = [...first.pages]
  const total = Math.max(first.total, pages.length)
  const publish = () =>
    onProgress?.({
      loaded: pages.length,
      total,
      snapshot: { ...first, pages: [...pages], offset: 0, limit: pages.length }
    })
  publish()
  if (pages.length >= total) return first
  let nextOffset = pages.length
  while (nextOffset < total) {
    const chunk = await api<StorageSnapshot>(`/api/storage?offset=${nextOffset}&limit=${STORAGE_PAGE_BATCH}&fields=map`)
    if (chunk.pages.length === 0) throw new Error(`页面总览只加载了 ${pages.length} / ${first.total} 页，服务端未返回后续页。`)
    pages.push(...chunk.pages)
    nextOffset = pages.length
    publish()
  }
  return { ...first, pages, offset: 0, limit: pages.length }
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function rawValue(value: unknown): RawPayload | null {
  if (!isRecord(value) || typeof value.encoding !== 'string' || typeof value.hex !== 'string' || typeof value.base64 !== 'string') return null
  return value as unknown as RawPayload
}

export function slotRecordLoaded(slot: StorageSlot): boolean {
  return slot.deleted || typeof slot.storage_encoding === 'string'
}

// HOW：页块颜色只表达空间密度，保留 page type 的色相作为第二层语义。
export function pageOccupancy(page: PageHeader): { ratio: number; freeRatio: number } {
  const capacity = Math.max(1, page.page_size)
  const freeSpace = Math.max(0, Math.min(capacity, page.logical_free_space ?? page.free_space))
  const freeRatio = freeSpace / capacity
  return { ratio: 1 - freeRatio, freeRatio }
}
