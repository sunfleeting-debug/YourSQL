import { describe, expect, it } from 'vitest'
import { encodeManualPayload } from '../../payload-codec'
import { inspectCatalogPayload, inspectFreePagePayload, inspectIndexPayload, inspectStoragePayload, inspectYsplPayload } from './raw-bytes'

describe('存储原始字节解析', () => {
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

  it('解析 MFR1 空闲页的后继指针', () => {
    const nextPage = [0x2a, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    const bytes = [0x4d, 0x46, 0x52, 0x31, ...nextPage]

    const inspection = inspectFreePagePayload(bytes)

    expect(inspection?.title).toBe('MFR1 空闲页链指针')
    expect(inspection?.fields).toContainEqual({ label: '下一空闲页', value: '#42' })
    expect(inspectStoragePayload(bytes, false, 'free')?.kind).toBe('free')
  })

  it('识别 MFR1 链尾页', () => {
    const bytes = [0x4d, 0x46, 0x52, 0x31, ...Array(8).fill(0)]

    const inspection = inspectFreePagePayload(bytes)

    expect(inspection?.summary).toBe('free-list 链尾页')
    expect(inspection?.fields).toContainEqual({ label: '下一空闲页', value: '链尾（NULL）' })
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

  it('解析 MCAT2 目录页链头和完整目录摘要', () => {
    const catalog = { version: 3, tables: [{ name: 'users' }], views: [], indexes: [] }
    const nextPage = [0x2a, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    const bytes = [0x4d, 0x43, 0x41, 0x54, 0x32, ...nextPage, ...Array.from(new TextEncoder().encode(JSON.stringify(catalog)))]

    const inspection = inspectCatalogPayload(bytes)

    expect(inspection?.title).toBe('MCAT2 目录链页')
    expect(inspection?.fields).toContainEqual({ label: '下一页', value: '#42' })
    expect(inspection?.fields).toContainEqual({ label: '编码', value: 'JSON payload' })
    expect(inspection?.fields).toContainEqual({ label: '目录摘要', value: 'v3 · 表 1 · 视图 0 · 索引 0' })
    expect(inspectStoragePayload(bytes)?.kind).toBe('catalog')
  })

  it('解析实际目录页使用的 YSPL 手写编码', () => {
    const catalog = { version: 3, tables: [], views: [{ name: 'active_users' }], indexes: [{ name: 'idx_users_id' }] }
    const bytes = [0x4d, 0x43, 0x41, 0x54, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, ...Array.from(encodeManualPayload(catalog))]

    const inspection = inspectCatalogPayload(bytes)

    expect(inspection?.fields).toContainEqual({ label: '编码', value: 'YSPL 手写 payload' })
    expect(inspection?.fields).toContainEqual({ label: '目录摘要', value: 'v3 · 表 0 · 视图 1 · 索引 1' })
  })

  it('按目录页类型兼容旧版裸 JSON payload', () => {
    const bytes = Array.from(new TextEncoder().encode(JSON.stringify({ version: 2, tables: [], views: [], indexes: [] })))

    const inspection = inspectStoragePayload(bytes, false, 'catalog')

    expect(inspection?.title).toBe('目录页 payload（兼容格式）')
    expect(inspection?.fields).toContainEqual({ label: '目录摘要', value: 'v2 · 表 0 · 视图 0 · 索引 0' })
  })

  it('目录页片段不完整时仍显示链路和编码，不退化成普通 Hex', () => {
    const bytes = [0x4d, 0x43, 0x41, 0x54, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x59, 0x53, 0x50, 0x4c, 0x07]

    const inspection = inspectCatalogPayload(bytes)

    expect(inspection?.summary).toContain('链尾页')
    expect(inspection?.fields).toContainEqual({ label: '编码', value: 'YSPL 手写 payload' })
    expect(inspection?.fields).toContainEqual({ label: '解析状态', value: '目录 payload 跨页分片，当前页片段不足以独立解码' })
  })
})
