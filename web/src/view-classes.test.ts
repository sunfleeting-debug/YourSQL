import { describe, expect, it } from 'vitest'
import { pageCellClassName, pageCellSegmentClassName, pageMapClassName, pageTileClassName, workbenchClassName } from './view-classes'

/** HOW：参照实现直接抄自重构前的模板字符串，用于逐例校验类名 token 完全一致。 */
const tokens = (value: string) => value.split(/\s+/).filter(Boolean)

const referenceCell = (options: Parameters<typeof pageCellClassName>[0]) =>
  `page-cell page-cell-v2 ${options.kind} ${options.masked ? 'masked' : ''} ${options.active ? 'selected' : ''} ${
    options.grouped ? 'group-selected' : ''
  } ${options.hasSlot ? 'has-slot' : ''} ${options.slotLinked ? 'slot-linked' : ''} ${
    options.partial ? 'partial' : ''
  } ${options.boundary === 'region' ? 'region-boundary' : ''} ${options.boundary === 'slot' ? 'slot-boundary' : ''}`

const referenceSegment = (options: Parameters<typeof pageCellSegmentClassName>[0]) =>
  `page-cell-segment ${options.kind} ${options.masked ? 'masked' : ''} ${options.boundary ? 'cut' : ''} ${
    options.boundary === 'region' ? 'region-cut' : ''
  } ${options.boundary === 'slot' ? 'slot-cut' : ''}`

const referenceTile = (options: Parameters<typeof pageTileClassName>[0]) =>
  `page-tile ${options.type} ${options.active ? 'selected' : ''} ${
    options.linked ? (options.linkedIndex ? 'table-linked-index' : 'table-linked') : ''
  } ${options.cacheFocus ? (options.cached ? 'cache-hit' : 'cache-muted') : ''}`

const referenceMap = (options: Parameters<typeof pageMapClassName>[0]) =>
  `page-map ${options.usageFill ? 'usage-fill' : ''} ${options.cacheFocus ? 'cache-focus' : ''} ${options.tableFocus ? 'table-focus' : ''}`

const referenceWorkbench = (options: Parameters<typeof workbenchClassName>[0]) =>
  `workbench ${options.leftOpen ? '' : 'left-collapsed'} ${options.storage ? 'storage-active' : ''} ${options.pipelineOpen ? 'pipeline-open' : ''}`

describe('视图类名纯函数', () => {
  it('页面单元格在所有开关组合下与模板串等价', () => {
    for (const kind of ['header', 'record', 'free']) {
      for (const masked of [true, false]) {
        for (const boundary of ['region', 'slot', null] as const) {
          const options = {
            kind,
            masked,
            active: masked,
            grouped: !masked,
            hasSlot: kind === 'record',
            slotLinked: kind === 'record' && masked,
            partial: kind === 'free',
            boundary
          }
          expect(tokens(pageCellClassName(options))).toEqual(tokens(referenceCell(options)))
        }
      }
    }
  })

  it('分段块、页面方块、地图容器与应用外壳的类名 token 一致', () => {
    for (const boundary of ['region', 'slot', null] as const) {
      const segment = { kind: 'record', masked: boundary === null, boundary }
      expect(tokens(pageCellSegmentClassName(segment))).toEqual(tokens(referenceSegment(segment)))
    }
    for (const linked of [true, false]) {
      for (const cacheFocus of [true, false]) {
        const tile = { type: 'heap', active: linked, linked, linkedIndex: !linked, cacheFocus, cached: !cacheFocus }
        expect(tokens(pageTileClassName(tile))).toEqual(tokens(referenceTile(tile)))
      }
    }
    for (const usageFill of [true, false]) {
      const map = { usageFill, cacheFocus: !usageFill, tableFocus: usageFill }
      expect(tokens(pageMapClassName(map))).toEqual(tokens(referenceMap(map)))
    }
    for (const leftOpen of [true, false]) {
      const shell = { leftOpen, storage: !leftOpen, pipelineOpen: leftOpen }
      expect(tokens(workbenchClassName(shell))).toEqual(tokens(referenceWorkbench(shell)))
    }
  })
})
