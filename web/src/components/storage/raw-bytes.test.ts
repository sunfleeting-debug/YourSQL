import { describe, expect, it } from 'vitest'
import { encodeManualPayload } from '../../payload-codec'
import { inspectIndexPayload, inspectYsplPayload } from './raw-bytes'

describe('YSPL raw byte inspection', () => {
  it('解码魔数后的小端长度文本帧', () => {
    const bytes = [0x59, 0x53, 0x50, 0x4c, 0x0b, 0x00, 0x00, 0x00, ...Array.from(new TextEncoder().encode('Hello World'))]

    const inspection = inspectYsplPayload(bytes)

    expect(inspection?.title).toBe('YSPL 长度帧')
    expect(inspection?.fields).toContainEqual({ label: '小端长度', value: '0b 00 00 00 · 11 B' })
    expect(inspection?.fields).toContainEqual({ label: '值', value: 'Hello World' })
  })

  it('解码当前 manual payload 的 TLV 内容', () => {
    const inspection = inspectYsplPayload(Array.from(encodeManualPayload([15, 'Hello World'])))

    expect(inspection?.title).toBe('YSPL 手写 TLV')
    expect(inspection?.fields).toContainEqual({ label: '解码值', value: '[15,"Hello World"]' })
  })

  it('解析 MBIX 索引页中的手写 payload', () => {
    const node = {
      version: 1,
      kind: 'leaf',
      level: 0,
      parent: null,
      next: 18,
      prev: null,
      keys: [[15], [21]],
      row_ids: [
        [32, 4],
        [32, 5]
      ],
      payloads: [['alpha'], ['beta']]
    }
    const bytes = [0x4d, 0x42, 0x49, 0x58, ...Array.from(encodeManualPayload(node))]

    const inspection = inspectIndexPayload(bytes)

    expect(inspection?.title).toBe('MBIX 索引页 payload')
    expect(inspection?.fields).toContainEqual({ label: '编码', value: 'YSPL 手写 payload' })
    expect(inspection?.fields).toContainEqual({ label: '节点类型', value: '叶子节点' })
    expect(inspection?.fields).toContainEqual({ label: '覆盖列 payload', value: '[["alpha"],["beta"]]' })
  })
})
