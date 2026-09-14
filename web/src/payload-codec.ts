/** 与后端 manual payload 对齐的 TypeScript TLV 编解码器。 */

import type { JsonValue } from './types/common'

export const MANUAL_PAYLOAD_CONTENT_TYPE = 'application/x-yoursql; version=1'
const MAGIC = new Uint8Array([0x59, 0x53, 0x50, 0x4c])

class Writer {
  private readonly data = Array.from(MAGIC)

  byte(value: number): void {
    this.data.push(value & 0xff)
  }

  append(value: Uint8Array): void {
    this.data.push(...value)
  }

  u32(value: number): void {
    if (!Number.isSafeInteger(value) || value < 0 || value > 0xffffffff) {
      throw new Error('manual payload 长度超出 uint32')
    }
    this.data.push(value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, (value >>> 24) & 0xff)
  }

  varUint(value: bigint): void {
    if (value < 0n) throw new Error('manual payload 整数不能为负')
    while (value >= 0x80n) {
      this.byte(Number(value & 0x7fn) | 0x80)
      value >>= 7n
    }
    this.byte(Number(value))
  }

  result(): Uint8Array {
    return new Uint8Array(this.data)
  }
}

class Reader {
  private offset = MAGIC.length

  constructor(private readonly data: Uint8Array) {
    if (!MAGIC.every((value, index) => data[index] === value)) {
      throw new Error('manual payload 魔数错误')
    }
  }

  byte(): number {
    if (this.offset >= this.data.length) throw new Error('manual payload 截断')
    return this.data[this.offset++]
  }

  bytes(length: number): Uint8Array {
    if (!Number.isSafeInteger(length) || length < 0 || this.offset + length > this.data.length) {
      throw new Error('manual payload 截断')
    }
    const value = this.data.slice(this.offset, this.offset + length)
    this.offset += length
    return value
  }

  u32(): number {
    const bytes = this.bytes(4)
    return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0, true)
  }

  varUint(): bigint {
    let value = 0n
    let shift = 0n
    for (let count = 0; count < 1024; count += 1) {
      const item = this.byte()
      value |= BigInt(item & 0x7f) << shift
      if ((item & 0x80) === 0) return value
      shift += 7n
    }
    throw new Error('manual payload 整数过长')
  }

  done(): boolean {
    return this.offset === this.data.length
  }
}

const textEncoder = new TextEncoder()
const textDecoder = new TextDecoder('utf-8', { fatal: true })

function writeValue(writer: Writer, value: JsonValue, depth: number): void {
  if (depth > 64) throw new Error('manual payload 嵌套过深')
  if (value === null) return writer.byte(0)
  if (value === false) return writer.byte(1)
  if (value === true) return writer.byte(2)
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('manual payload 不支持非有限数字')
    if (Number.isSafeInteger(value)) {
      const integer = BigInt(value)
      writer.byte(3)
      writer.varUint(integer >= 0n ? integer * 2n : -integer * 2n - 1n)
      return
    }
    const bytes = new Uint8Array(8)
    new DataView(bytes.buffer).setFloat64(0, value, true)
    writer.byte(4)
    writer.append(bytes)
    return
  }
  if (typeof value === 'string') {
    writer.byte(5)
    const encoded = textEncoder.encode(value)
    writer.u32(encoded.length)
    writer.append(encoded)
    return
  }
  if (Array.isArray(value)) {
    writer.byte(6)
    writer.u32(value.length)
    value.forEach(item => writeValue(writer, item, depth + 1))
    return
  }
  writer.byte(7)
  const keys = Object.keys(value).sort()
  writer.u32(keys.length)
  keys.forEach(key => {
    writeValue(writer, key, depth + 1)
    writeValue(writer, value[key], depth + 1)
  })
}

function readValue(reader: Reader, depth: number): JsonValue {
  if (depth > 64) throw new Error('manual payload 嵌套过深')
  switch (reader.byte()) {
    case 0:
      return null
    case 1:
      return false
    case 2:
      return true
    case 3: {
      const encoded = reader.varUint()
      const value = encoded % 2n === 0n ? encoded / 2n : -(encoded / 2n) - 1n
      return Number(value)
    }
    case 4: {
      const bytes = reader.bytes(8)
      const value = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getFloat64(0, true)
      if (!Number.isFinite(value)) throw new Error('manual payload 含非有限数字')
      return value
    }
    case 5:
      return textDecoder.decode(reader.bytes(reader.u32()))
    case 6: {
      const count = reader.u32()
      if (count > 1_000_000) throw new Error('manual payload 数组过长')
      return Array.from({ length: count }, () => readValue(reader, depth + 1))
    }
    case 7: {
      const count = reader.u32()
      if (count > 1_000_000) throw new Error('manual payload 对象过长')
      const result: { [key: string]: JsonValue } = {}
      for (let index = 0; index < count; index += 1) {
        const key = readValue(reader, depth + 1)
        if (typeof key !== 'string' || key in result) throw new Error('manual payload 对象键无效')
        result[key] = readValue(reader, depth + 1)
      }
      return result
    }
    default:
      throw new Error('manual payload 标签未知')
  }
}

export function encodeManualPayload(value: JsonValue): Uint8Array {
  const writer = new Writer()
  writeValue(writer, value, 0)
  return writer.result()
}

export function decodeManualPayload(raw: ArrayBuffer | Uint8Array): JsonValue {
  const reader = new Reader(raw instanceof Uint8Array ? raw : new Uint8Array(raw))
  const value = readValue(reader, 0)
  if (!reader.done()) throw new Error('manual payload 含尾部字节')
  return value
}
