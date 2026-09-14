/** 视图类名拼接：把条件类名交给纯函数，避免一行塞进几百字符的模板字符串。 */

/** 拼接 className，自动跳过 false / null / undefined / 空串。 */
export function classNames(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ')
}

export interface PageCellClassNameOptions {
  kind: string
  masked: boolean
  active: boolean
  grouped: boolean
  hasSlot: boolean
  slotLinked: boolean
  slotReusable?: boolean
  partial: boolean
  boundary?: 'region' | 'slot' | null
}

/** 页面画布单格：基础类 + 类型 + 选中态 + 边界切分。 */
export function pageCellClassName(options: PageCellClassNameOptions): string {
  const { kind, masked, active, grouped, hasSlot, slotLinked, slotReusable, partial, boundary } = options
  return classNames(
    'page-cell',
    'page-cell-v2',
    kind,
    masked && 'masked',
    active && 'selected',
    grouped && 'group-selected',
    hasSlot && 'has-slot',
    slotLinked && 'slot-linked',
    slotReusable && 'slot-reusable',
    partial && 'partial',
    boundary === 'region' && 'region-boundary',
    boundary === 'slot' && 'slot-boundary'
  )
}

/** 单格内部的边界分段块。 */
export function pageCellSegmentClassName(options: {
  kind: string
  masked: boolean
  slotReusable?: boolean
  boundary?: 'region' | 'slot' | null
}): string {
  return classNames(
    'page-cell-segment',
    options.kind,
    options.masked && 'masked',
    options.slotReusable && 'slot-reusable',
    options.boundary && 'cut',
    options.boundary === 'region' && 'region-cut',
    options.boundary === 'slot' && 'slot-cut'
  )
}

export interface PageTileClassNameOptions {
  type: string
  active: boolean
  linked: boolean
  linkedIndex: boolean
  cacheFocus: boolean
  cached: boolean
  cacheQueue?: 'a1in' | 'am' | null
}

/** 页面地图方块：类型 + 选中 + 关联表/索引 + 缓存高亮。 */
export function pageTileClassName(options: PageTileClassNameOptions): string {
  const { type, active, linked, linkedIndex, cacheFocus, cached, cacheQueue } = options
  return classNames(
    'page-tile',
    type,
    active && 'selected',
    linked && (linkedIndex ? 'table-linked-index' : 'table-linked'),
    cacheFocus && (cached ? 'cache-hit' : 'cache-muted'),
    cacheFocus && cacheQueue === 'a1in' && 'cache-cold',
    cacheFocus && cacheQueue === 'am' && 'cache-hot'
  )
}

/** 页面地图容器：使用率编码、缓存高亮与关联表聚焦。 */
export function pageMapClassName(options: { usageFill: boolean; cacheFocus: boolean; tableFocus: boolean }): string {
  return classNames('page-map', options.usageFill && 'usage-fill', options.cacheFocus && 'cache-focus', options.tableFocus && 'table-focus')
}

/** 应用外壳：左栏折叠、存储模式与流水线展开。 */
export function workbenchClassName(options: { leftOpen: boolean; storage: boolean; pipelineOpen: boolean }): string {
  return classNames('workbench', !options.leftOpen && 'left-collapsed', options.storage && 'storage-active', options.pipelineOpen && 'pipeline-open')
}

/** 查询标签：活动标签与正在执行的标签可以同时出现。 */
export function queryTabClassName(options: { active: boolean; running: boolean }): string {
  return classNames('query-tab', options.active && 'active', options.running && 'running')
}

/** 右侧流水线入口按钮。 */
export function pipelineLaunchClassName(open: boolean): string {
  return classNames('pipeline-launch', 'subtle', open && 'active')
}

/** 右侧流水线容器的拖拽状态。 */
export function pipelineWorkspaceClassName(resizing: boolean): string {
  return classNames('pipeline-workspace', resizing && 'is-resizing')
}

/** 可拖拽的数据库侧栏边界。 */
export function sidebarResizeHandleClassName(resizing: boolean): string {
  return classNames('sidebar-resize-handle', resizing && 'is-resizing')
}

/** 存储页右侧详情栏：拖拽时扩大边界提示。 */
export function storageDetailPanelClassName(resizing: boolean): string {
  return classNames('storage-detail-panel', resizing && 'is-resizing')
}

/** 在线/离线指示灯。 */
export function statusDotClassName(online: boolean): string {
  return classNames('status-dot', online ? 'online' : 'offline')
}
