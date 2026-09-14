import { decodeManualPayload } from '../../payload-codec'

export interface RawByteField {
  label: string
  value: string
}

export interface YsplInspection {
  title: string
  summary: string
  fields: RawByteField[]
}

const YSPL_MAGIC = [0x59, 0x53, 0x50, 0x4c]

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

function littleEndianUint32(bytes: number[]): number {
  return bytes[0] + (bytes[1] << 8) + (bytes[2] << 16) + ((bytes[3] << 24) >>> 0)
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
      title: 'YSPL 手写 TLV',
      summary: '当前 YourSQL manual payload 编码',
      fields: [...fields, { label: '解码值', value: JSON.stringify(decoded) ?? '—' }]
    }
  } catch {
    return {
      title: 'YSPL 原始帧',
      summary: '已识别 YSPL 魔数，但内容不是完整可解码帧',
      fields: [...fields, { label: '后续字节', value: `${rest.length} B` }]
    }
  }
}
