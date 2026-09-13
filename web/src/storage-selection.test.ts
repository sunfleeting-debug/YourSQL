import { describe, expect, it } from 'vitest'
import { pageTileAction } from './storage-selection'

describe('页面块点击语义', () => {
  it('切换页必须一次点击直接加载，而不是先折叠工作区', () => {
    expect(pageTileAction('12', '13', true)).toBe('inspect')
    expect(pageTileAction(null, '13', false)).toBe('inspect')
  })

  it('点击已选中的页保持块选中与抽屉，不清空当前页面', () => {
    expect(pageTileAction('12', '12', true)).toBe('noop')
  })

  it('已选中但详情未加载（例如上次失败）时允许重试', () => {
    expect(pageTileAction('12', '12', false)).toBe('inspect')
  })
})
