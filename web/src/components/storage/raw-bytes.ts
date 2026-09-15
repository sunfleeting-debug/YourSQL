import { decodeManualPayload } from '../../payload-codec'
import type { JsonObject, JsonValue } from '../../types/common'

export interface RawByteField {
  label: string
  value: string
}

export interface PayloadInspection {
  kind: 'yspl' | 'index' | 'catalog' | 'directory' | 'free'
  title: string
  summary: string
  fields: RawByteField[]
}

const YSPL_MAGIC = [0x59, 0x53, 0x50, 0x4c]
const INDEX_MAGIC = [0x4d, 0x42, 0x49, 0x58]
const CATALOG_MAGIC = [0x4d, 0x43, 0x41, 0x54, 0x32]
const FREE_MAGIC = [0x4d, 0x46, 0x52, 0x31]
const DIRECTORY_MAGIC = [0x4d, 0x44, 0x49, 0x52, 0x31]
const CATALOG_CHAIN_HEADER_SIZE = CATALOG_MAGIC.length + 8
const FREE_CHAIN_HEADER_SIZE = FREE_MAGIC.length + 8
const DIRECTORY_CHAIN_HEADER_SIZE = DIRECTORY_MAGIC.length + 8

function formatHex(bytes: number[]): string {
  return bytes.map(value => value.toString(16).padStart(2, '0')).join(' ')
}

function decodeUtf8(bytes: number[]): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes))
  } catch {
    return null
  }
}

function decodeBase64(value: string): number[] {
  try {
    return Array.from(atob(value), character => character.charCodeAt(0))
  } catch {
    return []
  }
}

/** 从接口返回的 raw payload 还原字节；Base64 为空时回退到 Hex 预览。 */
export function decodeRawPayloadBytes(payload: { base64: string; hex: string }): number[] {
  const decoded = decodeBase64(payload.base64)
  if (decoded.length > 0) return decoded
  const bytes: number[] = []
  for (const part of payload.hex.trim().split(/\s+/).filter(Boolean)) {
    if (!/^[0-9a-f]{2}$/i.test(part)) break
    bytes.push(Number.parseInt(part, 16))
  }
  return bytes
}

function littleEndianUint32(bytes: number[]): number {
  return bytes[0] + (bytes[1] << 8) + (bytes[2] << 16) + ((bytes[3] << 24) >>> 0)
}

function littleEndianUint64(bytes: number[]): bigint {
  let value = 0n
  for (let index = 0; index < Math.min(8, bytes.length); index += 1) {
    value |= BigInt(bytes[index]) << BigInt(index * 8)
  }
  return value
}

function isObject(value: JsonValue): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function formatJson(value: JsonValue | undefined): string {
  if (value === undefined) return '—'
  return JSON.stringify(value) ?? '—'
}

function indexField(label: string, value: JsonValue | undefined): RawByteField {
  return { label, value: formatJson(value) }
}

function catalogEncoding(bytes: number[]): string {
  if (YSPL_MAGIC.every((value, index) => bytes[index] === value)) return 'YSPL 手写 payload'
  const text = decodeUtf8(bytes)
  if (text !== null && text.trim().length > 0) return 'JSON payload'
  return '未识别'
}

function catalogChainTarget(bytes: number[]): string {
  const nextPageId = littleEndianUint64(bytes)
  return nextPageId === 0n ? '链尾（0）' : `#${nextPageId.toString()}`
}

function freeChainTarget(bytes: number[]): string {
  const nextPageId = littleEndianUint64(bytes)
  return nextPageId === 0n ? '链尾（NULL）' : `#${nextPageId.toString()}`
}

/** 解析 FREE 页的 MFR1 + uint64 后继指针，供页面详情直接显示 free-list 链路。 */
export function inspectFreePagePayload(bytes: number[], masked = false): PayloadInspection | null {
  if (masked || bytes.length < FREE_MAGIC.length || !FREE_MAGIC.every((value, index) => bytes[index] === value)) return null

  const fields: RawByteField[] = [{ label: '魔数', value: `${formatHex(FREE_MAGIC)} · MFR1` }]
  if (bytes.length < FREE_CHAIN_HEADER_SIZE) {
    return {
      kind: 'free',
      title: 'MFR1 空闲页链指针',
      summary: '已识别 FREE 页魔数，但后继页号不完整',
      fields: [...fields, { label: '链头', value: `${bytes.length} / ${FREE_CHAIN_HEADER_SIZE} B` }]
    }
  }

  const nextPageBytes = bytes.slice(FREE_MAGIC.length, FREE_CHAIN_HEADER_SIZE)
  fields.push(
    { label: '链头', value: `${FREE_CHAIN_HEADER_SIZE} B · MFR1 + uint64 小端页号` },
    { label: '下一空闲页', value: freeChainTarget(nextPageBytes) },
    { label: '当前 payload', value: `${bytes.length} B` },
    { label: '解析状态', value: freeChainTarget(nextPageBytes) === '链尾（NULL）' ? '链尾' : '指针有效' }
  )

  return {
    kind: 'free',
    title: 'MFR1 空闲页链指针',
    summary: freeChainTarget(nextPageBytes) === '链尾（NULL）' ? 'free-list 链尾页' : 'free-list 链中页 · 指向下一空闲页',
    fields
  }
}

function catalogValueSummary(value: JsonValue): string {
  if (!isObject(value)) return formatJson(value)
  const tableCount = Array.isArray(value.tables) ? value.tables.length : 0
  const viewCount = Array.isArray(value.views) ? value.views.length : 0
  const indexCount = Array.isArray(value.indexes) ? value.indexes.length : 0
  const version = typeof value.version === 'number' ? `v${value.version}` : '未知版本'
  return `${version} · 表 ${tableCount} · 视图 ${viewCount} · 索引 ${indexCount}`
}

function decodeCatalogBody(bytes: number[]): { encoding: string; value?: JsonValue } {
  const encoding = catalogEncoding(bytes)
  if (encoding === 'YSPL 手写 payload') {
    try {
      return { encoding, value: decodeManualPayload(new Uint8Array(bytes)) }
    } catch {
      return { encoding }
    }
  }
  if (encoding !== 'JSON payload') return { encoding }
  try {
    return { encoding, value: JSON.parse(decodeUtf8(bytes) ?? '') as JsonValue }
  } catch {
    return { encoding }
  }
}

/** 解析 MCAT2 目录页链，展示链指针与编码状态，而不是把跨页片段当作普通原始 payload。 */
export function inspectCatalogPayload(bytes: number[], masked = false, allowLegacy = false): PayloadInspection | null {
  if (masked) return null
  const hasCatalogMagic = bytes.length >= CATALOG_MAGIC.length && CATALOG_MAGIC.every((value, index) => bytes[index] === value)
  if (!hasCatalogMagic) {
    if (!allowLegacy) return null
    const decoded = decodeCatalogBody(bytes)
    if (decoded.value === undefined) return null
    return {
      kind: 'catalog',
      title: '目录页 payload（兼容格式）',
      summary: `旧版裸 payload · ${decoded.encoding}`,
      fields: [
        { label: '编码', value: decoded.encoding },
        { label: '当前片段', value: `${bytes.length} B` },
        { label: '目录摘要', value: catalogValueSummary(decoded.value) }
      ]
    }
  }

  const fields: RawByteField[] = [{ label: '魔数', value: `${formatHex(CATALOG_MAGIC)} · MCAT2` }]
  if (bytes.length < CATALOG_CHAIN_HEADER_SIZE) {
    return {
      kind: 'catalog',
      title: 'MCAT2 目录链页',
      summary: '已识别目录页魔数，但链头不完整',
      fields: [...fields, { label: '链头', value: `${bytes.length} / ${CATALOG_CHAIN_HEADER_SIZE} B` }]
    }
  }

  const nextPageBytes = bytes.slice(CATALOG_MAGIC.length, CATALOG_CHAIN_HEADER_SIZE)
  const body = bytes.slice(CATALOG_CHAIN_HEADER_SIZE)
  const decoded = decodeCatalogBody(body)
  fields.push(
    { label: '链头', value: `${CATALOG_CHAIN_HEADER_SIZE} B · MCAT2 + uint64 小端页号` },
    { label: '下一页', value: catalogChainTarget(nextPageBytes) },
    { label: '编码', value: decoded.encoding },
    { label: '当前片段', value: `${body.length} B` }
  )

  if (decoded.value !== undefined) {
    fields.push({ label: '目录摘要', value: catalogValueSummary(decoded.value) })
  } else if (body.length === 0) {
    fields.push({ label: '解析状态', value: '当前页没有目录片段' })
  } else {
    fields.push({ label: '解析状态', value: '目录 payload 跨页分片，当前页片段不足以独立解码' })
  }

  const hasNextPage = catalogChainTarget(nextPageBytes) !== '链尾（0）'
  return {
    kind: 'catalog',
    title: 'MCAT2 目录链页',
    summary: hasNextPage ? '目录元数据跨页保存 · 当前页仍有后续片段' : '目录元数据链尾页 · 当前片段已到末端',
    fields
  }
}

function directoryValueSummary(value: JsonValue | undefined): string {
  if (value === undefined) return '无法独立解码'
  if (!isObject(value)) return formatJson(value)
  return `命名项 ${Object.keys(value).length} 个`
}

/** 解析 MDIR1 命名页目录页，展示链指针和当前页的逻辑命名项。 */
export function inspectDirectoryPayload(bytes: number[], masked = false): PayloadInspection | null {
  if (masked || bytes.length < DIRECTORY_MAGIC.length || !DIRECTORY_MAGIC.every((value, index) => bytes[index] === value)) return null

  const fields: RawByteField[] = [{ label: '魔数', value: `${formatHex(DIRECTORY_MAGIC)} · MDIR1` }]
  if (bytes.length < DIRECTORY_CHAIN_HEADER_SIZE) {
    return {
      kind: 'directory',
      title: 'MDIR1 命名页目录链页',
      summary: '已识别目录页魔数，但链头不完整',
      fields: [...fields, { label: '链头', value: `${bytes.length} / ${DIRECTORY_CHAIN_HEADER_SIZE} B` }]
    }
  }

  const nextPageBytes = bytes.slice(DIRECTORY_MAGIC.length, DIRECTORY_CHAIN_HEADER_SIZE)
  const body = bytes.slice(DIRECTORY_CHAIN_HEADER_SIZE)
  const decoded = decodeCatalogBody(body)
  const nextPageId = littleEndianUint64(nextPageBytes)
  fields.push(
    { label: '链头', value: `${DIRECTORY_CHAIN_HEADER_SIZE} B · MDIR1 + uint64 小端页号` },
    { label: '下一页', value: nextPageId === 0n ? '链尾（0）' : `#${nextPageId.toString()}` },
    { label: '编码', value: decoded.encoding },
    { label: '当前片段', value: `${body.length} B` },
    { label: '目录摘要', value: directoryValueSummary(decoded.value) }
  )
  return {
    kind: 'directory',
    title: 'MDIR1 命名页目录链页',
    summary: nextPageId === 0n ? '命名页目录链尾页' : '命名页目录链中页 · 指向后续目录页',
    fields
  }
}

function decodeIndexPayload(bytes: number[]): { value: JsonObject; encoding: string } | null {
  const body = new Uint8Array(bytes.slice(INDEX_MAGIC.length))
  try {
    const value = decodeManualPayload(body)
    return isObject(value) ? { value, encoding: 'YSPL 手写 payload' } : null
  } catch {
    const text = decodeUtf8(Array.from(body))
    if (text === null) return null
    try {
      const value = JSON.parse(text) as JsonValue
      return isObject(value) ? { value, encoding: 'JSON payload' } : null
    } catch {
      return null
    }
  }
}

/** 解析 MBIX 索引页载荷，展示节点结构而不是只把索引页当作二进制块。 */
export function inspectIndexPayload(bytes: number[], masked = false): PayloadInspection | null {
  if (masked || bytes.length < INDEX_MAGIC.length || !INDEX_MAGIC.every((value, index) => bytes[index] === value)) return null

  const decoded = decodeIndexPayload(bytes)
  if (!decoded) {
    return {
      kind: 'index',
      title: 'MBIX 索引页原始帧',
      summary: '已识别索引页魔数，但后续节点 payload 不完整或无法解码',
      fields: [
        { label: '魔数', value: `${formatHex(INDEX_MAGIC)} · MBIX` },
        { label: '后续字节', value: `${bytes.length - INDEX_MAGIC.length} B` }
      ]
    }
  }

  const node = decoded.value
  const kind = node.kind === 'leaf' ? '叶子节点' : node.kind === 'internal' ? '内部节点' : String(node.kind ?? '未知节点')
  const fields: RawByteField[] = [
    { label: '魔数', value: `${formatHex(INDEX_MAGIC)} · MBIX` },
    { label: '编码', value: decoded.encoding },
    indexField('版本', node.version),
    { label: '节点类型', value: kind },
    indexField('层级', node.level),
    indexField('父页', node.parent),
    indexField('下一页', node.next),
    indexField('上一页', node.prev),
    indexField('键数量', Array.isArray(node.keys) ? node.keys.length : node.keys),
    indexField('keys', node.keys)
  ]

  if (node.kind === 'leaf') {
    fields.push(indexField('RowId', node.row_ids))
    fields.push({
      label: '覆盖列 payload',
      value: Array.isArray(node.payloads) && node.payloads.length > 0 ? formatJson(node.payloads) : '无（纯键索引）'
    })
  } else {
    fields.push(indexField('子页 children', node.children))
  }

  return {
    kind: 'index',
    title: 'MBIX 索引页 payload',
    summary: `MBIX 节点结构 · ${decoded.encoding}`,
    fields
  }
}

/** 识别 YSPL 的长度帧与当前手写 TLV，避免把魔数后的内容笼统显示为乱码。 */
export function inspectYsplPayload(bytes: number[], masked = false): PayloadInspection | null {
  if (masked || bytes.length < YSPL_MAGIC.length || !YSPL_MAGIC.every((value, index) => bytes[index] === value)) return null

  const rest = bytes.slice(YSPL_MAGIC.length)
  const fields: RawByteField[] = [{ label: '魔数', value: `${formatHex(YSPL_MAGIC)} · YSPL` }]
  if (rest.length >= 4) {
    const declaredLength = littleEndianUint32(rest.slice(0, 4))
    const framedBytes = rest.slice(4)
    if (declaredLength === framedBytes.length) {
      const text = decodeUtf8(framedBytes)
      if (text !== null) {
        return {
          kind: 'yspl',
          title: 'YSPL 长度帧',
          summary: '魔数 + uint32 小端长度 + UTF-8/ASCII 内容',
          fields: [...fields, { label: '小端长度', value: `${formatHex(rest.slice(0, 4))} · ${declaredLength} B` }, { label: '值', value: text }]
        }
      }
    }
  }

  try {
    const decoded = decodeManualPayload(new Uint8Array(bytes))
    return {
      kind: 'yspl',
      title: 'YSPL 手写 TLV',
      summary: '当前 YourSQL manual payload 编码',
      fields: [...fields, { label: '解码值', value: JSON.stringify(decoded) ?? '—' }]
    }
  } catch {
    return {
      kind: 'yspl',
      title: 'YSPL 原始帧',
      summary: '已识别 YSPL 魔数，但内容不是完整可解码帧',
      fields: [...fields, { label: '后续字节', value: `${rest.length} B` }]
    }
  }
}

/** 统一识别存储页中的 FREE 链、MCAT2 目录链、MBIX 索引与普通 YSPL payload。 */
export function inspectStoragePayload(bytes: number[], masked = false, pageType?: string): PayloadInspection | null {
  return (
    (pageType === 'free' ? inspectFreePagePayload(bytes, masked) : null) ??
    (pageType === 'directory' ? inspectDirectoryPayload(bytes, masked) : null) ??
    inspectCatalogPayload(bytes, masked, pageType === 'catalog') ??
    inspectIndexPayload(bytes, masked) ??
    inspectYsplPayload(bytes, masked)
  )
}
