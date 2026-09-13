import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const css = readFileSync(fileURLToPath(new URL('./workspace-overrides.css', import.meta.url)), 'utf8')

describe('存储页常驻右栏', () => {
  it('两栏列宽只由 has-rail 决定，不随块选中开关抽屉改变地图列数', () => {
    expect(css).toContain('.storage-mode-layout.has-rail')
    // WHY：旧实现用 has-detail（依赖块选中态）切换列宽，一点方块地图就横向重排。
    expect(css).not.toContain('.storage-mode-layout.has-detail')
  })
})
