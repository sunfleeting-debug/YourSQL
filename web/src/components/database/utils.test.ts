import { describe, expect, it } from 'vitest'
import { formatFileSize, isReplacementPolicy, siblingDatabasePath } from './utils'

describe('数据库选择器工具', () => {
  it('保留文件大小和同目录新库路径的展示规则', () => {
    expect(formatFileSize(1024)).toBe('1.0 KiB')
    expect(siblingDatabasePath('data/demo.db')).toBe('data/new_database.db')
    expect(siblingDatabasePath('D:\\data\\demo.db')).toBe('D:\\data\\new_database.db')
  })

  it('只接受内核支持的缓存淘汰策略', () => {
    expect(isReplacementPolicy('lru')).toBe(true)
    expect(isReplacementPolicy('fifo')).toBe(true)
    expect(isReplacementPolicy('random')).toBe(false)
  })
})
