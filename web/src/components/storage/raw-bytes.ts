import { decodeManualPayload } from '../../payload-codec'
import type { JsonObject, JsonValue } from '../../types/common'

export interface RawByteField {
  label: string
  value: string
}

export interface YsplInspection {
  kind: 'yspl' | 'index'
  title: string
  summary: string
  fields: RawByteField[]
}

const YSPL_MAGIC = [0x59, 0x53, 0x50, 0x4c]
const INDEX_MAGIC = [0x4d, 0x42, 0x49, 0x58]

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
export function inspectIndexPayload(bytes: number[], masked = false): YsplInspection | null {
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
export function inspectYsplPayload(bytes: number[], masked = false): YsplInspection | null {
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

/** 统一识别存储页中的索引 MBIX 与普通 YSPL payload。 */
export function inspectStoragePayload(bytes: number[], masked = false): YsplInspection | null {
  return inspectIndexPayload(bytes, masked) ?? inspectYsplPayload(bytes, masked)
}
