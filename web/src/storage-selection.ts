/** 页面地图点块的点击语义：切换必须一次点击生效，已选中的页不得打断当前块选中。 */

export type PageTileAction = 'inspect' | 'noop'

/**
 * 决定点击页面块时该做什么。
 *
 * WHY：早期实现切换页时只清空 detail/selected/cellSelection 而不加载新页，UI 表现为
 * “第一次点击把工作区和抽屉折叠，第二次点击才真正切换”。
 *
 * @param selectedPageId 当前已选中的页号
 * @param pageId 本次点击的页号
 * @param pageLoaded 当前页的详情是否已加载（加载失败时允许重试）
 */
export function pageTileAction(selectedPageId: string | null, pageId: string, pageLoaded: boolean): PageTileAction {
  if (selectedPageId !== pageId) return 'inspect'
  return pageLoaded ? 'noop' : 'inspect'
}
