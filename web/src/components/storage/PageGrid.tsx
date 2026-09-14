/** 存储页的字节/区域可视化与选中详情。 */

import { memo, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import { Grid3X3 } from 'lucide-react'
import type { JsonObject, JsonValue } from '../../types/common'
import type { RawPayload, StorageLayout, StoragePageDetail, StorageRegion, StorageSlot } from '../../types/storage'
import { pageCellClassName, pageCellSegmentClassName } from '../../view-classes'
import { StorageTooltip, useStorageTooltip } from './StorageTooltip'
import { inspectStoragePayload } from './raw-bytes'

type PageGridValue = JsonValue | undefined

export interface PageGridSelection {
  cellIndex: number
  kind: PageGridCellKind
  heading: string
  range: string
  byteCount: number
  slotText: string
  slotIds: number[]
  slotDetails: StorageSlot[]
  cellCount: number | null
  partialText: string
  explanation: string
  hex: string
  ascii: string
  completeRange: string
  completeByteCount: number
  completeHex: string
  completeAscii: string
  completeKind: PageGridCellKind
  payloadInspection: ReturnType<typeof inspectStoragePayload>
  grouped: boolean
}

interface Props {
  detail: StoragePageDetail
  onCellDetail?: (selection: PageGridSelection | null) => void
  onSelectTable?: (tableName: string) => void
  onOpenIndex?: (indexName: string) => void
}

type PageAssociationInlineProps = Pick<Props, 'detail' | 'onSelectTable' | 'onOpenIndex'>

export type PageGridCellKind = 'header' | 'inner-header' | 'directory' | 'free' | 'record' | 'payload' | 'legacy-slot'
type CellKind = PageGridCellKind

interface CellSegment {
  kind: CellKind
  offset: number
  bytes: number[]
  slotIds: number[]
  start: number
  end: number
  masked: boolean
}

interface PageCell {
  index: number
  offset: number
  bytes: number[]
  kind: CellKind
  slotIds: number[]
  fraction: number
  masked: boolean
  segments: CellSegment[]
}

interface CellRange {
  start: number
  end: number
  kind: CellKind
  slotIds: number[]
  masked: boolean
}

type SelectionMode = 'group' | 'cell'

interface CellGroup {
  key: string
  kind: CellKind
  isRegion: boolean
  cellIndices: number[]
  slotIds: number[]
  offset: number
  end: number
  bytes: number[]
  masked: boolean
}

const GRID_COLUMNS = 32
const CELL_SIZE = 16
const DEFAULT_SLOT_WIDTH = 8

// WHY：选中区域组件会随选区切换重新挂载，原生 details 的 open 状态会因此丢失。
// 将状态提升到模块级订阅源，让页内视图和右侧详情面板共享同一个展开偏好。
let rawByteDetailsOpen = false
const rawByteDetailsListeners = new Set<() => void>()

function subscribeRawByteDetails(listener: () => void): () => void {
  rawByteDetailsListeners.add(listener)
  return () => rawByteDetailsListeners.delete(listener)
}

function getRawByteDetailsOpen(): boolean {
  return rawByteDetailsOpen
}

function setRawByteDetailsOpen(open: boolean): void {
  if (rawByteDetailsOpen === open) return
  rawByteDetailsOpen = open
  rawByteDetailsListeners.forEach(listener => listener())
}

function decodeHex(value: string): number[] {
  const bytes: number[] = []
  for (const part of value.trim().split(/\s+/).filter(Boolean)) {
    if (!/^[0-9a-f]{2}$/i.test(part)) break
    bytes.push(Number.parseInt(part, 16))
  }
  return bytes
}

function decodeBase64(value: string): number[] {
  try {
    return Array.from(atob(value), character => character.charCodeAt(0))
  } catch {
    return []
  }
}

function payloadBytes(payload: RawPayload | undefined): number[] {
  if (!payload) return []
  const decoded = decodeBase64(payload.base64)
  return decoded.length > 0 ? decoded : decodeHex(payload.hex)
}

function pageBytes(detail: StoragePageDetail): { bytes: number[]; masked: boolean } {
  const fullPage = payloadBytes(detail.raw_page)
  if (fullPage.length >= detail.page_size) return { bytes: fullPage.slice(0, detail.page_size), masked: detail.masked === true }
  const headerSize = detail.header_size ?? 26
  const bytes = new Array<number>(detail.page_size).fill(0)
  payloadBytes(detail.raw_payload)
    .slice(0, Math.max(0, detail.page_size - headerSize))
    .forEach((value, index) => {
      bytes[headerSize + index] = value
    })
  return {
    bytes,
    masked: detail.masked === true || (!detail.raw_page && detail.type === 'catalog' && detail.payload_size > 0)
  }
}

function kindLabel(kind: CellKind): string {
  if (kind === 'header') return '页头'
  if (kind === 'inner-header') return '页内头'
  if (kind === 'directory') return '槽目录'
  if (kind === 'record') return '记录'
  if (kind === 'legacy-slot') return '旧槽位'
  if (kind === 'payload') return 'Payload'
  return '空闲区'
}

function cellGroupKey(cell: PageCell): string {
  const segment = cell.segments[0]
  if (segment && (segment.kind === 'record' || segment.kind === 'legacy-slot')) {
    return `cell:${segment.kind}:${segment.slotIds[0] ?? cell.index}`
  }
  return `cell:${segment?.kind ?? cell.kind}`
}

function segmentGroupKey(segment: CellSegment): string {
  if (segment.kind === 'record' || segment.kind === 'legacy-slot') {
    return `cell:${segment.kind}:${segment.slotIds[0] ?? segment.offset}`
  }
  return `cell:${segment.kind}`
}

function boundaryKind(left: CellSegment | undefined, right: CellSegment | undefined): 'region' | 'slot' | undefined {
  if (!left || !right) return undefined
  if (left.kind !== right.kind) return 'region'
  if (!['directory', 'record', 'legacy-slot'].includes(left.kind)) return undefined
  const sameSlot = left.slotIds.length === right.slotIds.length && left.slotIds.every((slotId, index) => slotId === right.slotIds[index])
  return sameSlot ? undefined : 'slot'
}

function regionGroupKey(kind: CellKind): string {
  return `region:${kind}`
}

function rangeText(start: number, end: number): string {
  return `${start.toString(16).padStart(4, '0')}–${end.toString(16).padStart(4, '0')}`
}

function makeCells(detail: StoragePageDetail): { cells: PageCell[]; slotWidth: number } {
  const { bytes, masked } = pageBytes(detail)
  const pageSize = Math.max(0, detail.page_size)
  const layout: StorageLayout | undefined = detail.physical_layout
  const slotWidth = Math.max(1, layout?.slot_entry_size ?? DEFAULT_SLOT_WIDTH)
  const ranges: CellRange[] = []

  const addRange = (offset: number, length: number, kind: CellKind, slotIds: number[] = []) => {
    let cursor = Math.max(0, Math.min(pageSize, offset))
    let remaining = Math.max(0, Math.min(pageSize - cursor, length))
    while (remaining > 0) {
      const size = Math.min(slotWidth, remaining)
      ranges.push({ start: cursor, end: cursor + size, kind, slotIds, masked: masked && kind !== 'header' })
      cursor += size
      remaining -= size
    }
  }

  const addRegion = (region: StorageRegion, kind: CellKind, slotIds: number[] = []) => {
    addRange(region.start, region.size, kind, slotIds)
  }

  const binary = layout?.physical === true && layout.format === 'double_ended_v2'
  const headerSize = Math.min(pageSize, Math.max(0, detail.header_size ?? 26))
  addRange(0, headerSize, 'header')

  if (binary && layout) {
    const payloadOffset = layout.payload_offset ?? headerSize
    addRange(payloadOffset, layout.inner_header_size ?? 0, 'inner-header')
    const layoutSlots = layout.slots ?? []
    for (const slot of layoutSlots) {
      if (typeof slot.directory_offset === 'number') addRange(slot.directory_offset, slot.directory_length ?? slotWidth, 'directory', [slot.slot_id])
    }
    const freeRegions = Array.isArray(layout.free_regions) ? layout.free_regions : layout.free_region ? [layout.free_region] : []
    for (const region of freeRegions) addRegion(region, 'free')
    const records = layoutSlots.filter(slot => !slot.deleted && slot.length > 0).sort((left, right) => left.offset - right.offset)
    let recordCursor = layout.record_region?.start ?? pageSize
    for (const slot of records) {
      if (slot.offset > recordCursor) addRange(recordCursor, slot.offset - recordCursor, 'free')
      addRange(slot.offset, slot.length, 'record', [slot.slot_id])
      recordCursor = Math.max(recordCursor, slot.offset + slot.length)
    }
    const recordEnd = layout.record_region?.end ?? pageSize
    if (recordCursor < recordEnd) addRange(recordCursor, recordEnd - recordCursor, 'free')
  } else {
    let cursor = headerSize
    const payloadEnd = Math.min(pageSize, headerSize + Math.max(0, detail.payload_size))
    const legacySlots = (detail.slots ?? [])
      .filter(slot => typeof slot.page_offset === 'number' && (slot.byte_length ?? 0) > 0)
      .map(slot => ({
        slot,
        start: Math.max(headerSize, slot.page_offset ?? headerSize),
        end: Math.min(payloadEnd, (slot.page_offset ?? headerSize) + Math.max(0, slot.byte_length ?? 0))
      }))
      .filter(item => item.end > item.start)
      .sort((left, right) => left.start - right.start)
    for (const item of legacySlots) {
      if (item.start > cursor) addRange(cursor, item.start - cursor, 'payload')
      addRange(item.start, item.end - item.start, 'legacy-slot', [item.slot.slot_id])
      cursor = Math.max(cursor, item.end)
    }
    if (cursor < payloadEnd) addRange(cursor, payloadEnd - cursor, 'payload')
    if (payloadEnd < pageSize) addRange(payloadEnd, pageSize - payloadEnd, 'free')
  }

  const sortedRanges = ranges.slice().sort((left, right) => left.start - right.start || left.end - right.end)
  const cells = Array.from({ length: Math.ceil(pageSize / slotWidth) }, (_, index): PageCell => {
    const offset = index * slotWidth
    const end = Math.min(pageSize, offset + slotWidth)
    const segments: CellSegment[] = []
    let cursor = offset
    const addSegment = (start: number, segmentEnd: number, kind: CellKind, slotIds: number[], segmentMasked: boolean) => {
      if (segmentEnd <= start) return
      segments.push({
        kind,
        offset: start,
        bytes: bytes.slice(start, segmentEnd),
        slotIds,
        start: ((start - offset) / slotWidth) * 100,
        end: ((segmentEnd - offset) / slotWidth) * 100,
        masked: segmentMasked
      })
      cursor = Math.max(cursor, segmentEnd)
    }
    for (const range of sortedRanges) {
      if (range.end <= offset) continue
      if (range.start >= end) break
      const segmentStart = Math.max(offset, range.start)
      const segmentEnd = Math.min(end, range.end)
      if (segmentStart > cursor) addSegment(cursor, segmentStart, 'free', [], masked)
      addSegment(segmentStart, segmentEnd, range.kind, range.slotIds, range.masked)
    }
    if (cursor < end) addSegment(cursor, end, 'free', [], masked)
    const firstSegment = segments[0]
    const slotIds = Array.from(new Set(segments.flatMap(segment => segment.slotIds)))
    return {
      index,
      offset,
      bytes: bytes.slice(offset, end),
      kind: firstSegment?.kind ?? 'free',
      slotIds,
      fraction: (end - offset) / slotWidth,
      masked: segments.some(segment => segment.masked),
      segments
    }
  })
  return { cells, slotWidth }
}

function makeGroups(cells: PageCell[]): Map<string, CellGroup> {
  const groups = new Map<string, CellGroup>()
  const append = (key: string, cell: PageCell, segment: CellSegment, isRegion: boolean) => {
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        kind: segment.kind,
        isRegion,
        cellIndices: [],
        slotIds: [],
        offset: segment.offset,
        end: segment.offset + segment.bytes.length - 1,
        bytes: [],
        masked: false
      }
      groups.set(key, group)
    }
    if (!group.cellIndices.includes(cell.index)) group.cellIndices.push(cell.index)
    group.offset = Math.min(group.offset, segment.offset)
    group.end = Math.max(group.end, segment.offset + segment.bytes.length - 1)
    group.bytes.push(...segment.bytes)
    group.masked ||= segment.masked
    for (const slotId of segment.slotIds) if (!group.slotIds.includes(slotId)) group.slotIds.push(slotId)
  }
  for (const cell of cells)
    for (const segment of cell.segments) {
      append(segmentGroupKey(segment), cell, segment, false)
      append(regionGroupKey(segment.kind), cell, segment, true)
    }
  return groups
}

function hex(bytes: number[]): string {
  return bytes.map(value => value.toString(16).padStart(2, '0')).join(' ')
}
function ascii(bytes: number[]): string {
  return bytes.map(value => (value >= 32 && value < 127 ? String.fromCharCode(value) : '.')).join('')
}

// 将区域和单格的说明集中管理，保持下方 JSX 只负责布局。
function groupDescription(group: CellGroup, binaryLayout: boolean): string {
  if (group.masked) return 'MASKED'
  if (group.kind === 'directory') return ''
  if (group.kind === 'record')
    return group.slotIds.length === 1
      ? `槽位 ${group.slotIds[0]} 的完整记录，共 ${group.bytes.length} B，从页尾向前分配。`
      : `记录区汇总，共 ${group.bytes.length} B。`
  if (group.kind === 'inner-header') return '槽位布局控制头：记录格式、槽位数和边界。'
  if (group.kind === 'free') return binaryLayout ? `空闲区汇总，共 ${group.bytes.length} B。` : '页负载之后的填充字节。'
  if (group.kind === 'header') return '完整固定 Page Header，位于整个数据库页最前方。'
  if (group.kind === 'legacy-slot') return `旧版槽位 ${group.slotIds.join(', ')} 的完整 JSON/Base64 token。`
  return '完整 payload 区域。'
}

function cellDescription(cell: PageCell, binaryLayout: boolean): string {
  if (cell.masked) return 'MASKED'
  const segmentKinds = Array.from(new Set(cell.segments.map(segment => segment.kind)))
  if (segmentKinds.length > 1) return `边界格连续包含 ${segmentKinds.map(kindLabel).join('、')}，在同一格内按物理字节切分。`
  if (cell.kind === 'directory') return ''
  if (cell.kind === 'record') return binaryLayout ? `槽位 ${cell.slotIds.join(', ')} 的记录字节，从页尾向前分配。` : '旧版槽位 token 的记录字节。'
  if (cell.kind === 'inner-header') return '槽位布局控制头，包含格式、槽位数和边界。'
  if (cell.kind === 'free') return binaryLayout ? '槽目录和记录区相向增长后留下的空闲字节。' : '页负载之后的填充字节。'
  if (cell.kind === 'header') return '固定 Page Header，位于整个数据库页最前方。'
  if (cell.kind === 'legacy-slot') return '旧版 JSON/Base64 页的兼容槽位。'
  return '非 HEAP 页的 payload 区域。'
}

type StructureFocus = 'header' | 'inner-header' | 'superblock'

interface PageFact {
  label: string
  value: string
  code?: boolean
  raw?: PageFactRaw
}

interface PageFactRaw {
  kind: 'exact' | 'derived' | 'payload'
  range: string
  hex: string
  binary: string
}

function recordValue(value: PageGridValue): JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value : {}
}

function textValue(value: PageGridValue, fallback = '—'): string {
  if (typeof value === 'string' && value.length > 0) return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return fallback
}

function byteValue(value: PageGridValue): string {
  return typeof value === 'number' ? `${value.toLocaleString()} B` : '—'
}

function offsetValue(value: PageGridValue): string {
  return typeof value === 'number' ? `0x${value.toString(16)}` : '—'
}

function rawPageBytes(detail: StoragePageDetail): number[] {
  const bytes = payloadBytes(detail.raw_page)
  return bytes.length >= detail.page_size ? bytes.slice(0, detail.page_size) : []
}

function byteRange(start: number, length: number): string {
  if (length <= 0) return '—'
  const end = start + length - 1
  return `0x${start.toString(16).padStart(4, '0')}–0x${end.toString(16).padStart(4, '0')}`
}

function binary(bytes: number[]): string {
  return bytes.map(value => value.toString(2).padStart(8, '0')).join(' ')
}

function exactFact(detail: StoragePageDetail, start: number, length: number): PageFactRaw {
  const bytes = rawPageBytes(detail).slice(start, start + length)
  if (detail.masked) return { kind: 'exact', range: byteRange(start, length), hex: 'MASKED', binary: 'MASKED' }
  if (bytes.length !== length) return { kind: 'exact', range: byteRange(start, length), hex: '不可用', binary: '不可用' }
  return {
    kind: 'exact',
    range: byteRange(start, length),
    hex: bytes.map(value => value.toString(16).padStart(2, '0')).join(' '),
    binary: binary(bytes)
  }
}

function derivedFact(label = '派生值'): PageFactRaw {
  return { kind: 'derived', range: label, hex: '—', binary: '—' }
}

function payloadFact(label = 'Payload JSON'): PageFactRaw {
  return { kind: 'payload', range: label, hex: '字段映射', binary: '字段映射' }
}

function hexTooltip(label: string, raw: PageFactRaw): string {
  return `${label} · ${raw.range}\nHEX ${raw.hex}`
}

interface PageHeaderField {
  label: string
  start: number
  length: number
  className: string
}

interface PageHeaderByteSegment extends PageHeaderField {
  row: number
  column: number
}

function PageHeaderByteMap({ detail }: { detail: StoragePageDetail }) {
  const { tooltip, tooltipProps } = useStorageTooltip()
  const bytes = rawPageBytes(detail)
  const headerSize = detail.header_size ?? 30
  const byteColumns = 16
  const fields: PageHeaderField[] = [
    { label: 'Magic', start: 0, length: 4, className: 'magic' },
    { label: '版本', start: 4, length: 4, className: 'version' },
    { label: '类型', start: 8, length: 1, className: 'type' },
    { label: '保留', start: 9, length: 1, className: 'reserved' },
    { label: '页号', start: 10, length: 8, className: 'page-id' },
    { label: 'Payload', start: 18, length: 4, className: 'payload' },
    { label: 'CRC32', start: 22, length: 4, className: 'crc' },
    { label: '对齐保留', start: 26, length: 4, className: 'reserved' }
  ]
  const segments = fields.flatMap<PageHeaderByteSegment>(field => {
    const result: PageHeaderByteSegment[] = []
    let start = field.start
    let remaining = field.length
    while (remaining > 0) {
      const column = start % byteColumns
      const length = Math.min(remaining, byteColumns - column)
      result.push({ ...field, start, length, row: Math.floor(start / byteColumns), column })
      start += length
      remaining -= length
    }
    return result
  })
  const rowCount = Math.max(1, Math.ceil(headerSize / byteColumns))
  const byteGridStyle = {
    gridTemplateColumns: `repeat(${byteColumns}, minmax(0, 1fr))`,
    gridTemplateRows: `repeat(${rowCount}, 28px)`
  }
  return (
    <section className="page-header-byte-map" aria-label="页头二进制字段布局">
      <div className="page-header-byte-map-heading">
        <strong>页头 HEX</strong>
        <code>{detail.header_size ?? 30} B</code>
      </div>
      <div className="page-header-byte-layout">
        <div className="page-header-byte-grid" style={byteGridStyle}>
          {Array.from({ length: headerSize }, (_, index) => (
            <code
              className="page-header-byte-value"
              key={index}
              style={{ gridColumn: (index % byteColumns) + 1, gridRow: Math.floor(index / byteColumns) + 1 }}
            >
              {bytes[index]?.toString(16).padStart(2, '0') ?? '—'}
            </code>
          ))}
          <div className="page-header-byte-fields">
            {segments.map(segment => {
              const raw = exactFact(detail, segment.start, segment.length)
              return (
                <div
                  className={`page-header-byte-field ${segment.className}`}
                  key={`${segment.start}-${segment.label}`}
                  style={{ gridColumn: `${segment.column + 1} / span ${segment.length}`, gridRow: segment.row + 1 }}
                  {...tooltipProps(hexTooltip(segment.label, raw))}
                />
              )
            })}
          </div>
        </div>
      </div>
      <StorageTooltip tooltip={tooltip} />
    </section>
  )
}

function PageInnerHeaderByteMap({ detail }: { detail: StoragePageDetail }) {
  const { tooltip, tooltipProps } = useStorageTooltip()
  const layout = detail.physical_layout
  const baseOffset = layout?.payload_offset ?? detail.header_size ?? 0
  const headerSize = layout?.inner_header_size ?? 12
  const bytes = rawPageBytes(detail).slice(baseOffset, baseOffset + headerSize)
  const byteColumns = Math.min(16, Math.max(1, headerSize))
  const fields: PageHeaderField[] = [
    { label: 'Magic', start: 0, length: 4, className: 'magic' },
    { label: '版本', start: 4, length: 1, className: 'version' },
    { label: '标志', start: 5, length: 1, className: 'reserved' },
    { label: '槽位数', start: 6, length: 2, className: 'slot-count' },
    { label: '槽目录起点', start: 8, length: 2, className: 'payload' },
    { label: '记录起点', start: 10, length: 2, className: 'record-start' }
  ]
  const segments = fields.flatMap<PageHeaderByteSegment>(field => {
    const result: PageHeaderByteSegment[] = []
    let start = field.start
    let remaining = Math.min(field.length, Math.max(0, headerSize - start))
    while (remaining > 0) {
      const column = start % byteColumns
      const length = Math.min(remaining, byteColumns - column)
      result.push({ ...field, start, length, row: Math.floor(start / byteColumns), column })
      start += length
      remaining -= length
    }
    return result
  })
  const rowCount = Math.max(1, Math.ceil(headerSize / byteColumns))
  const byteGridStyle = {
    gridTemplateColumns: `repeat(${byteColumns}, minmax(0, 1fr))`,
    gridTemplateRows: `repeat(${rowCount}, 28px)`
  }
  return (
    <section className="page-header-byte-map page-inner-header-byte-map" aria-label="页内头 HEX 字段布局">
      <div className="page-header-byte-map-heading">
        <strong>页内头 HEX</strong>
        <code>
          {byteRange(baseOffset, headerSize)} · {headerSize} B
        </code>
      </div>
      <div className="page-header-byte-layout">
        <div className="page-header-byte-grid" style={byteGridStyle}>
          {Array.from({ length: headerSize }, (_, index) => (
            <code
              className="page-header-byte-value"
              key={index}
              style={{ gridColumn: (index % byteColumns) + 1, gridRow: Math.floor(index / byteColumns) + 1 }}
            >
              {bytes[index]?.toString(16).padStart(2, '0') ?? '—'}
            </code>
          ))}
          <div className="page-header-byte-fields">
            {segments.map(segment => {
              const raw = exactFact(detail, baseOffset + segment.start, segment.length)
              return (
                <div
                  className={`page-header-byte-field ${segment.className}`}
                  key={`${segment.start}-${segment.label}`}
                  style={{ gridColumn: `${segment.column + 1} / span ${segment.length}`, gridRow: segment.row + 1 }}
                  {...tooltipProps(hexTooltip(segment.label, raw))}
                />
              )
            })}
          </div>
        </div>
      </div>
      <StorageTooltip tooltip={tooltip} />
    </section>
  )
}

function regionValue(region: StorageRegion | undefined): string {
  const start = region?.start ?? null
  const end = region?.end ?? null
  if (start === null || end === null || end <= start) return '—'
  return `${offsetValue(start)}–${offsetValue(end - 1)}`
}

function structureFocus(detail: StoragePageDetail, selection?: PageGridSelection | null): StructureFocus | null {
  if (selection?.kind === 'header') return 'header'
  if (selection?.kind === 'inner-header') return 'inner-header'
  return detail.type === 'superblock' ? 'superblock' : null
}

function structureTitle(focus: StructureFocus): string {
  if (focus === 'header') return '页头'
  if (focus === 'inner-header') return '页内头'
  return '超级块'
}

function pageTypeLabel(type: string): string {
  if (type === 'superblock') return '超级块'
  if (type === 'catalog') return '目录页'
  if (type === 'heap') return '堆表页'
  if (type === 'index') return '索引页'
  if (type === 'free') return '空闲页'
  return type || '—'
}

function structureFacts(detail: StoragePageDetail, focus: StructureFocus): PageFact[] {
  if (focus === 'header') {
    return [
      { label: '页号', value: `#${detail.page_id}`, code: true, raw: exactFact(detail, 10, 8) },
      { label: '类型', value: pageTypeLabel(detail.type), raw: exactFact(detail, 8, 1) },
      { label: 'Magic', value: textValue(detail.magic), code: true, raw: exactFact(detail, 0, 4) },
      {
        label: '版本',
        value: typeof detail.version === 'number' ? `v${detail.version}` : '—',
        code: true,
        raw: exactFact(detail, 4, 4)
      },
      { label: '页大小', value: byteValue(detail.page_size), raw: derivedFact('文件配置') },
      { label: '页头长度', value: byteValue(detail.header_size), raw: derivedFact('格式常量') },
      { label: 'Payload', value: byteValue(detail.payload_size), raw: exactFact(detail, 18, 4) },
      { label: '空闲', value: byteValue(detail.free_space), raw: derivedFact('页大小 − Payload') },
      { label: 'CRC32', value: textValue(detail.crc32), code: true, raw: exactFact(detail, 22, 4) }
    ]
  }
  if (focus === 'superblock') {
    const metadata = recordValue(detail.metadata)
    const freePages = Array.isArray(metadata.free_pages) ? metadata.free_pages : []
    const namedPages = recordValue(metadata.named_pages)
    return [
      { label: '页大小', value: byteValue(metadata.page_size ?? detail.page_size), raw: payloadFact() },
      {
        label: '文件页数',
        value: textValue(metadata.page_count ?? metadata.next_page_id),
        code: true,
        raw: payloadFact()
      },
      { label: '下一页号', value: textValue(metadata.next_page_id), code: true, raw: payloadFact() },
      { label: '空闲页', value: `${freePages.length} 页`, raw: payloadFact() },
      { label: '命名页', value: `${Object.keys(namedPages).length} 个`, raw: payloadFact() },
      { label: 'Payload', value: byteValue(detail.payload_size), raw: exactFact(detail, 18, 4) },
      { label: '页内空闲', value: byteValue(detail.free_space), raw: derivedFact('页大小 − Payload') },
      { label: '校验', value: textValue(detail.crc32), code: true, raw: exactFact(detail, 22, 4) }
    ]
  }
  const layout = detail.physical_layout
  const innerHeaderSize = layout?.inner_header_size ?? 0
  const slotEntrySize = layout?.slot_entry_size ?? 0
  const directory = layout?.slot_directory
  const record = layout?.record_region
  const payloadOffset = layout?.payload_offset ?? detail.header_size ?? 0
  return [
    { label: '格式', value: textValue(layout?.format), code: true, raw: exactFact(detail, payloadOffset, 4) },
    { label: '页内头', value: byteValue(innerHeaderSize), raw: exactFact(detail, payloadOffset, innerHeaderSize) },
    {
      label: '槽位数',
      value: textValue(layout?.slot_count ?? detail.slot_count),
      code: true,
      raw: exactFact(detail, payloadOffset + 6, 2)
    },
    { label: '槽条目大小', value: byteValue(slotEntrySize), raw: derivedFact('格式常量') },
    { label: 'Payload 起点', value: offsetValue(layout?.payload_offset), code: true, raw: derivedFact('布局偏移') },
    { label: '槽目录范围', value: regionValue(directory), code: true, raw: derivedFact('布局范围') },
    { label: '记录区范围', value: regionValue(record), code: true, raw: derivedFact('布局范围') },
    {
      label: '记录方向',
      value: textValue(record?.direction === 'backward' ? '从页尾向前' : record?.direction),
      raw: derivedFact('布局规则')
    }
  ]
}

function SuperblockByteMap({ detail }: { detail: StoragePageDetail }) {
  const { tooltip, tooltipProps } = useStorageTooltip()
  const pageSize = Math.max(1, detail.page_size)
  const headerBytes = Math.min(pageSize, detail.header_size ?? 26)
  const payloadBytesCount = Math.min(Math.max(0, pageSize - headerBytes), Math.max(0, detail.payload_size))
  const freeBytes = Math.max(0, pageSize - headerBytes - payloadBytesCount)
  const segments = [
    { label: '页头', bytes: headerBytes, className: 'header' },
    { label: '元数据', bytes: payloadBytesCount, className: 'payload' },
    { label: '填充', bytes: freeBytes, className: 'free' }
  ]
  const metadata = recordValue(detail.metadata)
  const namedPages = recordValue(metadata.named_pages)
  const namedEntries = Object.entries(namedPages).slice(0, 6)
  return (
    <div className="superblock-visual">
      <div className="superblock-byte-map" aria-label="超级块页内布局">
        <div className="superblock-byte-track">
          {segments
            .filter(segment => segment.bytes > 0)
            .map(segment => (
              <span
                key={segment.className}
                className={`superblock-byte-segment ${segment.className}`}
                style={{ width: `${(segment.bytes / pageSize) * 100}%` }}
                {...tooltipProps(`${segment.label} · ${segment.bytes} B`)}
              />
            ))}
        </div>
        <div className="superblock-byte-legend">
          {segments.map(segment => (
            <span key={segment.className}>
              <i className={`page-key ${segment.className}`} />
              {segment.label} {segment.bytes} B
            </span>
          ))}
        </div>
      </div>
      {namedEntries.length > 0 && (
        <div className="superblock-named-pages">
          <span>命名页</span>
          {namedEntries.map(([name, page]) => (
            <code key={name}>
              {name} · #{textValue(page)}
            </code>
          ))}
        </div>
      )}
      <StorageTooltip tooltip={tooltip} />
    </div>
  )
}

export function PageStructureSummary({ detail, selection = null }: { detail: StoragePageDetail; selection?: PageGridSelection | null }) {
  const { tooltip, tooltipProps } = useStorageTooltip()
  const focus = structureFocus(detail, selection)
  if (!focus) return null
  const facts = structureFacts(detail, focus)
  return (
    <section className={`page-structure-summary ${focus}`} aria-label={`${structureTitle(focus)}结构摘要`}>
      <div className="page-structure-summary-heading">
        <div>
          <strong>{structureTitle(focus)}</strong>
          <span>{focus === 'superblock' ? '数据库级元数据页' : focus === 'inner-header' ? '槽位布局控制头' : '固定二进制页头'}</span>
        </div>
        <code>{selection ? selection.range : `页 #${detail.page_id}`}</code>
      </div>
      <div className="page-structure-facts">
        {facts.map(fact => (
          <div className="page-structure-fact" key={fact.label}>
            <div className="page-structure-fact-main">
              <span>{fact.label}</span>
              {fact.code ? <code>{fact.value}</code> : <strong>{fact.value}</strong>}
            </div>
            {fact.raw?.kind === 'exact' && (
              <div className="page-structure-fact-raw exact" {...tooltipProps(hexTooltip(fact.label, fact.raw))}>
                <span>
                  <b>HEX</b>
                  <code>{fact.raw.hex}</code>
                </span>
              </div>
            )}
          </div>
        ))}
      </div>
      {focus === 'header' && <PageHeaderByteMap detail={detail} />}
      {focus === 'inner-header' && <PageInnerHeaderByteMap detail={detail} />}
      {focus === 'superblock' && <SuperblockByteMap detail={detail} />}
      <StorageTooltip tooltip={tooltip} />
    </section>
  )
}

function slotLocation(value: number | null | undefined): string {
  return typeof value === 'number' ? `0x${value.toString(16)}` : '—'
}

function slotRow(value: StorageSlot): string {
  if (!value.deleted && !value.storage_encoding) return '加载中…'
  return value.row === null ? 'NULL' : (JSON.stringify(value.row) ?? '—')
}

function SlotSelectionInfo({ slots }: { slots: StorageSlot[] }) {
  if (!slots.length) return null
  const visibleSlots = slots.slice(0, 12)
  return (
    <section className="selected-slot-info" aria-label="选中块关联的槽位信息">
      {slots.length === 1 ? (
        <div className="selected-slot-facts">
          <div>
            <span>状态</span>
            <b className={slots[0].deleted ? 'slot-deleted' : 'slot-live'}>{slots[0].deleted ? '可复用' : '有效'}</b>
          </div>
          <div>
            <span>记录位置</span>
            <code>{slotLocation(slots[0].page_offset)}</code>
          </div>
          <div>
            <span>记录长度</span>
            <strong>{slots[0].record_bytes} B</strong>
          </div>
          <div className="selected-slot-row">
            <span>记录值</span>
            <code>{slotRow(slots[0])}</code>
          </div>
        </div>
      ) : (
        <div className="selected-slot-table-wrap">
          <table className="selected-slot-table">
            <thead>
              <tr>
                <th>槽位</th>
                <th>状态</th>
                <th>记录位置</th>
                <th>字节</th>
                <th>记录值</th>
              </tr>
            </thead>
            <tbody>
              {visibleSlots.map(slot => (
                <tr key={slot.slot_id}>
                  <td>{slot.slot_id}</td>
                  <td>
                    <span className={slot.deleted ? 'slot-deleted' : 'slot-live'}>{slot.deleted ? '可复用' : '有效'}</span>
                  </td>
                  <td>
                    <code>{slotLocation(slot.page_offset)}</code>
                  </td>
                  <td>{slot.record_bytes}</td>
                  <td>
                    <code>{slotRow(slot)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {slots.length > visibleSlots.length && (
            <small className="selected-slot-overflow">其余 {slots.length - visibleSlots.length} 个槽位仍在下方槽位记录中。</small>
          )}
        </div>
      )}
    </section>
  )
}

function PayloadSelectionInfo({ inspection }: { inspection: NonNullable<PageGridSelection['payloadInspection']> }) {
  return (
    <section className="selected-payload-special" aria-label="Payload 特殊解析">
      <div className="selected-payload-special-heading">
        <strong>{inspection.title}</strong>
        <span>{inspection.summary}</span>
      </div>
      <dl>
        {inspection.fields.map(field => (
          <div key={field.label}>
            <dt>{field.label}</dt>
            <dd>
              <code>{field.value}</code>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

export function PageGridSelectionDetail({ selection, detail }: { selection: PageGridSelection; detail?: StoragePageDetail }) {
  const rawDetailsOpen = useSyncExternalStore(subscribeRawByteDetails, getRawByteDetailsOpen, getRawByteDetailsOpen)
  const showRawDetails = selection.kind !== 'header' && selection.kind !== 'inner-header'
  const scopeLabel = selection.grouped ? '选中区域' : '选中格'
  const rawLabel = selection.grouped ? '选中区域 Hex / ASCII' : '选中格 Hex / ASCII'
  const completeDiffers = selection.completeRange !== selection.range || selection.completeHex !== selection.hex
  const showCompleteDetails = completeDiffers || selection.kind === 'header' || selection.kind === 'inner-header'
  return (
    <div className={`page-cell-detail ${selection.grouped ? 'group-detail' : ''}`} data-selection-scope={selection.grouped ? 'region' : 'cell'}>
      {detail && <PageStructureSummary detail={detail} selection={selection} />}
      <div className="page-cell-detail-heading">
        <strong>{selection.heading}</strong>
        <em>{scopeLabel}</em>
        <span>
          {selection.range} B · {selection.byteCount} B{selection.slotText ? ` · ${selection.slotText}` : ''}
          {selection.grouped ? ` · ${selection.cellCount} 格` : selection.partialText}
        </span>
      </div>
      <SlotSelectionInfo slots={selection.slotDetails} />
      {selection.explanation && <p>{selection.explanation}</p>}
      {showCompleteDetails && (
        <details
          className="raw-byte-details complete-byte-details"
          open={selection.completeKind === 'record' || selection.completeKind === 'legacy-slot'}
        >
          <summary>
            <span>完整块 Hex / ASCII</span>
            <small>
              {selection.completeRange} B · {selection.completeByteCount} B · {kindLabel(selection.completeKind)}
            </small>
          </summary>
          <div className="page-group-readout">
            <dl>
              <dt>Hex</dt>
              <dd>
                <pre>{selection.completeHex}</pre>
              </dd>
            </dl>
            <dl>
              <dt>ASCII</dt>
              <dd>
                <pre>{selection.completeAscii}</pre>
              </dd>
            </dl>
          </div>
        </details>
      )}
      {selection.payloadInspection && <PayloadSelectionInfo inspection={selection.payloadInspection} />}
      {showRawDetails && (
        <details className="raw-byte-details" open={rawDetailsOpen} onToggle={event => setRawByteDetailsOpen(event.currentTarget.open)}>
          <summary>{rawLabel}</summary>
          <div className="page-group-readout">
            <dl>
              <dt>Hex</dt>
              <dd>
                <pre>{selection.hex}</pre>
              </dd>
            </dl>
            <dl>
              <dt>ASCII</dt>
              <dd>
                <pre>{selection.ascii}</pre>
              </dd>
            </dl>
          </div>
        </details>
      )}
    </div>
  )
}

function PageAssociationInline({ detail, onSelectTable, onOpenIndex }: PageAssociationInlineProps) {
  const tableName = detail.table_name?.trim() || '未知'
  const indexName = detail.index_name?.trim() || '未知'
  const tableLinkable = tableName !== '未知' && tableName !== 'MASKED' && !!onSelectTable
  const indexLinkable = indexName !== '未知' && indexName !== 'MASKED' && !!onOpenIndex
  const hasTable = tableName !== '未知'
  const hasIndex = indexName !== '未知'
  const associationText = [hasTable ? `表 ${tableName}` : '', hasIndex ? `索引 ${indexName}` : ''].filter(Boolean).join(' · ')

  if (!hasTable && !hasIndex) return null

  return (
    <span className="page-visual-association" aria-label={`关联对象：${associationText}`}>
      <span className="page-visual-association-prefix">（关联对象：</span>
      {hasTable &&
        (tableLinkable ? (
          <button type="button" title={`定位表 ${tableName}`} onClick={() => onSelectTable(tableName)}>
            表 {tableName}
          </button>
        ) : (
          <span className="page-visual-association-value">表 {tableName}</span>
        ))}
      {hasTable && hasIndex && <span className="page-visual-association-separator">·</span>}
      {hasIndex &&
        (indexLinkable ? (
          <button type="button" title={`打开索引 ${indexName}`} onClick={() => onOpenIndex(indexName)}>
            索引 {indexName}
          </button>
        ) : (
          <span className="page-visual-association-value">索引 {indexName}</span>
        ))}
      <span className="page-visual-association-suffix">）</span>
    </span>
  )
}

function PageGrid({ detail, onCellDetail, onSelectTable, onOpenIndex }: Props) {
  const { cells, slotWidth } = useMemo(() => makeCells(detail), [detail])
  const groups = useMemo(() => makeGroups(cells), [cells])
  const slots = detail.slots ?? []
  const layout = detail.physical_layout
  const binaryLayout = layout?.physical === true && layout.format === 'double_ended_v2'
  const viewport = useRef<HTMLDivElement>(null)
  const pan = useRef({ active: false, x: 0, y: 0, left: 0, top: 0 })
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [selectedSlotId, setSelectedSlotId] = useState<number | null>(null)
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null)
  const [selectionMode, setSelectionMode] = useState<SelectionMode>('cell')
  const { tooltip, tooltipProps } = useStorageTooltip()
  // WHY：回调放进 ref，重置/上报副作用就不能依赖函数标识，否则父组件每次新建回调
  // （切换页、刷新页详情、SQL 自动刷新）都会触发 onCellDetail(null)，把右侧抽屉无声关闭。
  const onCellDetailRef = useRef(onCellDetail)
  useEffect(() => {
    onCellDetailRef.current = onCellDetail
  }, [onCellDetail])

  useEffect(() => {
    setSelectedIndex(0)
    setSelectedSlotId(null)
    setSelectedGroupKey(null)
    setSelectionMode('cell')
    onCellDetailRef.current?.(null)
  }, [detail.page_id])

  const selected = cells[selectedIndex] ?? cells[0]
  if (!selected) return null

  const scrollToCell = (index: number) => {
    window.requestAnimationFrame(() =>
      viewport.current?.querySelector<HTMLElement>(`[data-cell-index="${index}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    )
  }
  const setCellSelection = (index: number, slotId: number | null = null) => {
    const safeIndex = Math.max(0, Math.min(index, cells.length - 1))
    const cell = cells[safeIndex]
    setSelectedIndex(safeIndex)
    setSelectedSlotId(slotId ?? (cell.slotIds.length === 1 ? cell.slotIds[0] : null))
    setSelectedGroupKey(cellGroupKey(cell))
    setSelectionMode('cell')
    scrollToCell(safeIndex)
  }
  const setGroupSelection = (group: CellGroup, preferredIndex = group.cellIndices[0]) => {
    const safeIndex = Math.max(0, Math.min(preferredIndex, cells.length - 1))
    setSelectedIndex(safeIndex)
    setSelectedSlotId(group.slotIds.length === 1 ? group.slotIds[0] : null)
    setSelectedGroupKey(group.key)
    setSelectionMode('group')
    scrollToCell(safeIndex)
  }
  const activateGroup = (group: CellGroup | undefined, preferredIndex?: number) => {
    if (!group) return
    const targetIndex = preferredIndex ?? (selectedGroupKey === group.key ? selectedIndex : group.cellIndices[0])
    if (group.cellIndices.length <= 1) {
      setCellSelection(targetIndex)
      return
    }
    if (selectedGroupKey === group.key && selectionMode === 'group') {
      setCellSelection(targetIndex)
      return
    }
    setGroupSelection(group, targetIndex)
  }
  const activateCell = (cell: PageCell) => {
    const group = groups.get(cellGroupKey(cell))
    // HOW：槽目录和记录格各自对应一个槽位，首次点击直接给出槽位上下文，避免先进入区域汇总。
    if (cell.slotIds.length === 1) {
      setCellSelection(cell.index, cell.slotIds[0])
      return
    }
    if (!group || group.cellIndices.length <= 1) {
      setCellSelection(cell.index)
      return
    }
    if (selectedGroupKey === group.key && selectionMode === 'group') {
      if (selectedIndex === cell.index) setCellSelection(cell.index)
      else setGroupSelection(group, cell.index)
      return
    }
    if (selectedGroupKey === group.key && selectionMode === 'cell') {
      if (selectedIndex === cell.index) setGroupSelection(group, cell.index)
      else setCellSelection(cell.index)
      return
    }
    setGroupSelection(group, cell.index)
  }
  const focusSlot = (slot: StorageSlot) => {
    const recordIndex = cells.findIndex(cell =>
      cell.segments.some(segment => (segment.kind === 'record' || segment.kind === 'legacy-slot') && segment.slotIds.includes(slot.slot_id))
    )
    const anyIndex = cells.findIndex(cell => cell.slotIds.includes(slot.slot_id))
    const recordCell = recordIndex >= 0 ? cells[recordIndex] : undefined
    const recordSegment = recordCell?.segments.find(
      segment => (segment.kind === 'record' || segment.kind === 'legacy-slot') && segment.slotIds.includes(slot.slot_id)
    )
    if (recordCell && recordSegment) activateGroup(groups.get(segmentGroupKey(recordSegment)), recordIndex)
    else if (anyIndex >= 0) activateCell(cells[anyIndex])
  }
  const beginPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button,select')) return
    const target = event.currentTarget
    pan.current = { active: true, x: event.clientX, y: event.clientY, left: target.scrollLeft, top: target.scrollTop }
    target.setPointerCapture(event.pointerId)
    target.classList.add('panning')
  }
  const movePan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pan.current.active) return
    event.currentTarget.scrollLeft = pan.current.left - (event.clientX - pan.current.x)
    event.currentTarget.scrollTop = pan.current.top - (event.clientY - pan.current.y)
  }
  const endPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pan.current.active) return
    pan.current.active = false
    event.currentTarget.releasePointerCapture(event.pointerId)
    event.currentTarget.classList.remove('panning')
  }

  const regionDefinitions = binaryLayout
    ? [
        { key: 'header', label: '页头', kind: 'header' as CellKind },
        { key: 'inner-header', label: '页内头', kind: 'inner-header' as CellKind },
        { key: 'directory', label: '槽目录', kind: 'directory' as CellKind },
        { key: 'free', label: '空闲区', kind: 'free' as CellKind },
        { key: 'record', label: '记录区', kind: 'record' as CellKind }
      ]
    : [
        { key: 'header', label: '页头', kind: 'header' as CellKind },
        { key: 'legacy-slot', label: '旧槽位', kind: 'legacy-slot' as CellKind },
        { key: 'payload', label: 'Payload', kind: 'payload' as CellKind },
        { key: 'free', label: '尾部填充', kind: 'free' as CellKind }
      ]
  const regions = regionDefinitions.map(region => ({ ...region, group: groups.get(regionGroupKey(region.kind)) })).filter(region => region.group)
  const columnCount = Math.min(GRID_COLUMNS, Math.max(1, cells.length))
  const rowCount = Math.ceil(cells.length / columnCount)
  // HOW：刻度和方格共用同一组父级 Grid 轨道，避免嵌套 Grid 各自计算宽高后错位。
  const pageGridStyle = {
    gridTemplateColumns: `42px repeat(${columnCount}, ${CELL_SIZE}px)`,
    gridTemplateRows: `${CELL_SIZE}px repeat(${rowCount}, ${CELL_SIZE}px)`
  }
  const activeGroup = selectedGroupKey ? groups.get(selectedGroupKey) : undefined
  const showingGroup = selectionMode === 'group' && activeGroup !== undefined
  const displayGroup = showingGroup ? activeGroup : undefined
  const displayBytes = displayGroup?.bytes ?? selected.bytes
  const displayMasked = displayGroup?.masked ?? selected.masked
  const displayHex = displayMasked ? 'MASKED' : hex(displayBytes)
  const displayAscii = displayMasked ? 'MASKED' : ascii(displayBytes)
  const displayRange = displayGroup
    ? rangeText(displayGroup.offset, displayGroup.end)
    : rangeText(selected.offset, selected.offset + selected.bytes.length - 1)
  const displaySlotText = displayGroup
    ? displayGroup.slotIds.length === 1
      ? `槽 ${displayGroup.slotIds[0]}`
      : displayGroup.slotIds.length > 1
        ? `${displayGroup.slotIds.length} 个槽位`
        : ''
    : selected.slotIds.length > 0
      ? `槽 ${selected.slotIds.join(', ')}`
      : ''
  const selectedExplanation = displayGroup ? groupDescription(displayGroup, binaryLayout) : cellDescription(selected, binaryLayout)
  const selectedSlotIds = displayGroup?.slotIds ?? selected.slotIds
  const completeGroup = displayGroup ?? groups.get(cellGroupKey(selected))
  const completeBytes = completeGroup?.bytes ?? selected.bytes
  const completeMasked = completeGroup?.masked ?? selected.masked
  const completeKind = completeGroup?.kind ?? selected.kind
  const completeRange = completeGroup
    ? rangeText(completeGroup.offset, completeGroup.end)
    : rangeText(selected.offset, selected.offset + selected.bytes.length - 1)
  const completeHex = completeMasked ? 'MASKED' : hex(completeBytes)
  const completeAscii = completeMasked ? 'MASKED' : ascii(completeBytes)
  const payloadInspection = inspectStoragePayload(completeBytes, completeMasked)
  const layoutSlotDetails = new Map(
    (layout?.slots ?? []).map(slot => [
      slot.slot_id,
      {
        slot_id: slot.slot_id,
        deleted: slot.deleted,
        record_bytes: slot.length,
        row: null,
        page_offset: slot.deleted ? null : slot.offset,
        byte_length: slot.length,
        slot_directory_offset: slot.directory_offset ?? null,
        slot_directory_length: slot.directory_length
      } satisfies StorageSlot
    ])
  )
  const slotDetailsById = new Map<number, StorageSlot>([...layoutSlotDetails.entries(), ...slots.map(slot => [slot.slot_id, slot] as const)])
  const slotDetails = selectedSlotIds.map(slotId => slotDetailsById.get(slotId)).filter((slot): slot is StorageSlot => slot !== undefined)
  const slotDetailsKey = slotDetails.map(slot => `${slot.slot_id}:${slot.deleted}:${slot.page_offset ?? ''}:${slot.record_bytes}`).join('|')
  const selection: PageGridSelection = {
    cellIndex: selected.index,
    kind: displayGroup?.kind ?? selected.kind,
    heading: displayGroup ? kindLabel(displayGroup.kind) : kindLabel(selected.kind),
    range: displayRange,
    byteCount: displayGroup?.bytes.length ?? selected.bytes.length,
    slotText: displaySlotText,
    slotIds: selectedSlotIds,
    slotDetails,
    cellCount: displayGroup?.cellIndices.length ?? null,
    partialText: selected.fraction < 1 ? ` · ${selected.bytes.length}/${slotWidth} B` : '',
    explanation: selectedExplanation,
    hex: displayHex,
    ascii: displayAscii,
    completeRange,
    completeByteCount: completeBytes.length,
    completeHex,
    completeAscii,
    completeKind,
    payloadInspection,
    grouped: displayGroup !== undefined
  }
  useEffect(() => {
    onCellDetailRef.current?.(selection)
  }, [
    selection.ascii,
    selection.completeAscii,
    selection.completeByteCount,
    selection.completeHex,
    selection.completeKind,
    selection.completeRange,
    selection.byteCount,
    selection.cellCount,
    selection.cellIndex,
    selection.explanation,
    selection.grouped,
    selection.heading,
    selection.hex,
    selection.kind,
    selection.partialText,
    selection.range,
    slotDetailsKey,
    selection.slotIds.join(','),
    selection.slotText,
    selection.payloadInspection?.title,
    selection.payloadInspection?.summary,
    selection.payloadInspection?.fields.map(field => `${field.label}:${field.value}`).join('|')
  ])

  return (
    <section className="page-visual" aria-label={`页面 ${detail.page_id} ${binaryLayout ? '双向槽式布局' : '兼容页布局'}`}>
      <div className="page-visual-heading">
        <div>
          <strong>
            <Grid3X3 size={13} />
            <span className="page-visual-page-label">页 #{detail.page_id}</span>
            <PageAssociationInline detail={detail} onSelectTable={onSelectTable} onOpenIndex={onOpenIndex} />
          </strong>
          <span>
            {detail.page_size.toLocaleString()} B · {cells.length} 格 · {slotWidth} B/格
          </span>
        </div>
        <div className="page-visual-controls">
          {regions.map(region => {
            const group = region.group
            if (!group) return null
            const active = selectedGroupKey === group.key
            const tooltipText = `${region.label} ${rangeText(group.offset, group.end)} · ${group.bytes.length} B`
            return (
              <button
                type="button"
                key={region.key}
                aria-label={tooltipText}
                className={`region-link ${region.key} ${active ? 'active' : ''}`}
                onClick={() => activateGroup(group)}
                {...tooltipProps(tooltipText)}
              >
                <i className={`page-key ${region.key}`} />
                <span>{region.label}</span>
              </button>
            )
          })}
          {slots.length > 0 && (
            <label className="slot-filter">
              <span>槽</span>
              <select
                aria-label="定位槽位"
                value={selectedSlotId ?? ''}
                onChange={event => {
                  const value = event.target.value
                  if (!value) {
                    setSelectedSlotId(null)
                    return
                  }
                  const slot = slots.find(item => item.slot_id === Number(value))
                  if (slot) focusSlot(slot)
                }}
              >
                <option value="">全部</option>
                {slots.map(slot => (
                  <option key={slot.slot_id} value={slot.slot_id}>
                    #{slot.slot_id} · {typeof slot.page_offset === 'number' ? `0x${slot.page_offset.toString(16)}` : '未知'}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      </div>
      <div className="page-grid-legend">
        <span>
          <i className="page-key header" />
          页头
        </span>
        {binaryLayout ? (
          <>
            <span>
              <i className="page-key inner-header" />
              页内头
            </span>
            <span>
              <i className="page-key directory" />
              槽目录
            </span>
            <span>
              <i className="page-key record" />
              记录区
            </span>
            <span>
              <i className="page-key free" />
              空闲区
            </span>
          </>
        ) : (
          <>
            <span>
              <i className="page-key legacy-slot" />
              旧槽位
            </span>
            <span>
              <i className="page-key payload" />
              Payload
            </span>
            <span>
              <i className="page-key free" />
              尾部填充
            </span>
          </>
        )}
        <span className="page-grid-note">
          <i className="boundary-key region" />
          区域边界 <i className="boundary-key slot" />
          槽边界 <i className="boundary-key fragment" />
          无数据片段
        </span>
      </div>
      <div
        className="page-grid-viewport"
        ref={viewport}
        tabIndex={0}
        role="grid"
        aria-label="双向槽式页字节格"
        onPointerDown={beginPan}
        onPointerMove={movePan}
        onPointerUp={endPan}
        onPointerCancel={endPan}
      >
        <div className="page-grid-frame page-grid-frame-v2" style={pageGridStyle}>
          <span className="page-grid-axis-corner" aria-hidden="true" style={{ gridColumn: '1', gridRow: '1' }} />
          <div className="page-grid-axis-top" aria-hidden="true">
            {Array.from({ length: columnCount }, (_, column) => (
              <span key={column} style={{ gridColumn: `${column + 2}`, gridRow: '1' }}>
                #{column}
              </span>
            ))}
          </div>
          <div className="page-grid-axis-left" aria-hidden="true">
            {Array.from({ length: rowCount }, (_, row) => {
              const cell = cells[row * columnCount]
              return (
                <span key={row} style={{ gridColumn: '1', gridRow: `${row + 2}` }}>
                  0x{cell.offset.toString(16).padStart(4, '0')}
                </span>
              )
            })}
          </div>
          <div className="page-grid page-grid-v2">
            {cells.map(cell => {
              const partialText = cell.fraction < 1 ? ` · 页尾 ${cell.bytes.length}/${slotWidth} B` : ''
              const segmentKinds = Array.from(new Set(cell.segments.map(segment => segment.kind)))
              const mixedText = segmentKinds.length > 1 ? ` · ${segmentKinds.length} 类边界切分` : ''
              const previousCell = cells[cell.index - 1]
              const firstSegment = cell.segments[0]
              const previousSegment = previousCell?.segments[previousCell.segments.length - 1]
              const cellBoundary = boundaryKind(previousSegment, firstSegment)
              const groupSelected = activeGroup?.cellIndices.includes(cell.index) ?? false
              const isActiveCell = selectedGroupKey === cellGroupKey(cell) && selectedIndex === cell.index

              // HOW：tooltip 拼接抽成小函数，否则这一行会超过 200 字符。
              const cellTooltip = () => {
                const range = rangeText(cell.offset, cell.offset + cell.bytes.length - 1)
                const slots = cell.slotIds.length ? ` · 槽位 ${cell.slotIds.join(', ')}` : ''
                return `${range} · ${kindLabel(cell.kind)}${slots}${partialText}${mixedText}`
              }
              const tooltipText = cellTooltip()
              return (
                <button
                  type="button"
                  role="gridcell"
                  data-cell-index={cell.index}
                  key={cell.index}
                  aria-label={tooltipText}
                  className={pageCellClassName({
                    kind: cell.kind,
                    masked: cell.masked,
                    active: isActiveCell,
                    grouped: groupSelected,
                    hasSlot: cell.slotIds.length > 0,
                    slotLinked: cell.slotIds.some(slotId => slotId === selectedSlotId),
                    partial: cell.fraction < 1,
                    boundary: cellBoundary
                  })}
                  style={{
                    gridColumn: `${(cell.index % columnCount) + 2}`,
                    gridRow: `${Math.floor(cell.index / columnCount) + 2}`
                  }}
                  onClick={() => activateCell(cell)}
                  {...tooltipProps(tooltipText)}
                >
                  {cell.segments.map((segment, segmentIndex) => {
                    const nextSegment = cell.segments[segmentIndex + 1]
                    const boundary = boundaryKind(segment, nextSegment)
                    return (
                      <span
                        aria-hidden="true"
                        className={pageCellSegmentClassName({ kind: segment.kind, masked: segment.masked, boundary })}
                        key={`${segment.kind}-${segment.offset}-${segmentIndex}`}
                        style={{ left: `${segment.start}%`, width: `${segment.end - segment.start}%` }}
                      />
                    )
                  })}
                </button>
              )
            })}
          </div>
        </div>
      </div>
      {!onCellDetail && <PageGridSelectionDetail selection={selection} detail={detail} />}
      <StorageTooltip tooltip={tooltip} />
    </section>
  )
}

export default memo(PageGrid)
