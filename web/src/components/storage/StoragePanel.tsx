/** 存储检查工作区：页面地图、缓存、索引和页详情。 */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, MouseEvent, UIEvent, WheelEvent } from 'react'
import {
  ArrowLeft,
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  HardDrive,
  List,
  ListTree,
  LockKeyhole,
  Network,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Table2,
  X
} from 'lucide-react'
import { api, ApiError, errorMessage } from '../../api'
import { pageTileAction } from '../../storage-selection'
import { useResizableWidth } from '../../use-resizable-width'
import { pageMapClassName, pageTileClassName, storageDetailPanelClassName } from '../../view-classes'
import type { JsonObject, JsonValue } from '../../types/common'
import type { IndexNode, IndexSnapshot } from '../../types/catalog'
import type {
  PageHeader,
  RawPayload,
  ReplacementPolicy,
  StorageCacheSnapshot,
  StorageBufferPoolDemo,
  DirectoryPageInfo,
  StorageIndexSnapshot,
  StoragePageChanges,
  StoragePageDetail,
  StorageProtectionChange,
  StoragePolicyChange,
  StorageResizeChange,
  StorageSlot,
  StorageSnapshot
} from '../../types/storage'
import { PAGE_TYPE_LABELS, PAGE_TYPE_LEGEND, STORAGE_TAB_OPTIONS, loadStorageSnapshot, pageOccupancy, rawValue, slotRecordLoaded } from './model'
import type { InspectableStorageKind, PageUsageVisual, StoragePanelProps, StorageSelection, StorageTab } from './model'
import JsonTree from '../query/JsonTree'
import PageGrid, { PageGridSelectionDetail } from './PageGrid'
import type { PageGridSelection } from './PageGrid'
import { decodeRawPayloadBytes, inspectStoragePayload } from './raw-bytes'
import { StorageTooltip, useStorageTooltip } from './StorageTooltip'
import type { StorageTooltipContainerProps } from './StorageTooltip'

type StorageDetail = StoragePageDetail | IndexSnapshot

const PAGE_TILE_SIZE = 16
const PAGE_TILE_GAP = 2
const PAGE_ROW_HEIGHT = PAGE_TILE_SIZE + PAGE_TILE_GAP
const PAGE_WINDOW_OVERSCAN_ROWS = 4

// WHY：页详情和索引详情共享同一块展示状态，但字段集合不同；联合类型让分支必须先说明当前详情种类。
function isPageDetail(value: StorageDetail): value is StoragePageDetail {
  return 'page_id' in value
}

function isIndexDetail(value: StorageDetail): value is IndexSnapshot {
  return 'entries' in value
}

interface PageMapTilesProps {
  pages: PageHeader[]
  pageStart: number
  columnCount: number
  selectedPageId: string | null
  linkedPageIds: ReadonlySet<number>
  linkedIndexPageIds: ReadonlySet<number>
  linkedIndexRootIds: ReadonlySet<number>
  tableFocus: boolean
  cachedPageIds: ReadonlySet<number>
  cachePolicy: ReplacementPolicy
  cacheFrameByPageId: ReadonlyMap<number, { pin_count: number; queue?: string }>
  evictionRankByPageId: ReadonlyMap<number, number>
}

/** 页面地图块本体；缓存开关只改变父级状态时，保持 5k+ 个块节点免于重建。 */
const PageMapTiles = memo(function PageMapTiles({
  pages,
  pageStart,
  columnCount,
  selectedPageId,
  linkedPageIds,
  linkedIndexPageIds,
  linkedIndexRootIds,
  tableFocus,
  cachedPageIds,
  cachePolicy,
  cacheFrameByPageId,
  evictionRankByPageId
}: PageMapTilesProps) {
  return (
    <>
      {pages.map((page, index) => {
        const pageIndex = pageStart + index
        const pageId = String(page.page_id)
        const cached = cachedPageIds.has(page.page_id)
        const active = selectedPageId === pageId
        const used = page.logical_used_space ?? page.payload_size
        const occupancy = pageOccupancy(page)
        const linked = linkedPageIds.has(page.page_id)
        const linkedIndexPage = linkedIndexPageIds.has(page.page_id)
        const indexRoot = linkedIndexRootIds.has(page.page_id)
        const frame = cacheFrameByPageId.get(page.page_id)
        const evictionRank = evictionRankByPageId.get(page.page_id)
        const cacheQueue: 'a1in' | 'am' | null =
          cachePolicy === '2q' && frame?.queue === 'a1in' ? 'a1in' : cachePolicy === '2q' && frame?.queue === 'am' ? 'am' : null
        const cacheOrderText = evictionRank ? ` · 淘汰序 #${evictionRank}` : frame?.pin_count ? ' · Pin，不参与淘汰' : ''
        const cacheText = cached ? ` · 缓存${cacheOrderText}` : ''
        const linkText = tableFocus && linked ? ` · ${linkedIndexPage ? (indexRoot ? '索引根页' : '索引页') : '关联表'}` : ''
        const freeLinkText =
          page.type === 'free'
            ? typeof page.free_page_next_id === 'number'
              ? ` · 下一空闲页 #${page.free_page_next_id}`
              : page.free_page_is_tail
                ? ' · 空闲链尾'
                : ''
            : ''
        const directoryLinkText =
          page.type === 'directory' && typeof page.directory_entry_count === 'number'
            ? ` · ${page.directory_entry_count} 项${typeof page.directory_next_page_id === 'number' ? ` · 后继 #${page.directory_next_page_id}` : ''}`
            : ''
        const pageText = `#${page.page_id} · ${PAGE_TYPE_LABELS[page.type] ?? page.type}`
        const usageText = `${used} B 已用 · ${page.free_space} B 空闲`
        const tooltipText = `${pageText} · ${usageText}${cacheText}${linkText}${freeLinkText}${directoryLinkText}`

        return (
          <button
            key={page.page_id}
            type="button"
            data-page-id={pageId}
            data-storage-tooltip={tooltipText}
            aria-pressed={active}
            aria-label={tooltipText}
            className={pageTileClassName({
              type: page.type,
              active,
              linked,
              linkedIndex: linkedIndexPage,
              // HOW：缓存类别常驻在节点上，开关只切换父级 .cache-focus，避免 5k+ 个节点同步改 class。
              cacheFocus: true,
              cached,
              cacheQueue
            })}
            style={
              {
                '--page-occupancy': occupancy.ratio,
                '--eviction-rank': evictionRank ? JSON.stringify(String(evictionRank)) : 'none',
                left: `${(pageIndex % columnCount) * PAGE_ROW_HEIGHT}px`,
                top: `${Math.floor(pageIndex / columnCount) * PAGE_ROW_HEIGHT}px`
              } as CSSProperties
            }
          />
        )
      })}
    </>
  )
})

interface PageMapProps extends Omit<PageMapTilesProps, 'pageStart' | 'columnCount'> {
  cacheFocus: boolean
  usageFill: boolean
  tableFocus: boolean
  ariaLabel: string
  scrollTargetKey: string
  scrollTargetPageId: number | null
  tooltipContainerProps: StorageTooltipContainerProps
  onSelectPage: (pageId: string) => void
}

/** 页面地图容器；点击与悬浮采用事件委托，避免为每个页块创建多组闭包。 */
const PageMap = memo(function PageMap({
  cacheFocus,
  usageFill,
  tableFocus,
  ariaLabel,
  scrollTargetKey,
  scrollTargetPageId,
  tooltipContainerProps,
  onSelectPage,
  ...tileProps
}: PageMapProps) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 })
  const [scrollTop, setScrollTop] = useState(0)
  const viewportHeight = viewportSize.height || 230
  const contentWidth = Math.max(1, viewportSize.width - 4)
  const columnCount = Math.max(1, Math.floor((contentWidth + PAGE_TILE_GAP) / PAGE_ROW_HEIGHT))
  const rowCount = Math.ceil(tileProps.pages.length / columnCount)
  const totalHeight = Math.max(PAGE_TILE_SIZE, rowCount * PAGE_ROW_HEIGHT - PAGE_TILE_GAP)
  const firstVisibleRow = Math.max(0, Math.floor(scrollTop / PAGE_ROW_HEIGHT) - PAGE_WINDOW_OVERSCAN_ROWS)
  const lastVisibleRow = Math.min(rowCount, Math.ceil((scrollTop + viewportHeight) / PAGE_ROW_HEIGHT) + PAGE_WINDOW_OVERSCAN_ROWS)
  const pageStart = firstVisibleRow * columnCount
  const pageEnd = Math.min(tileProps.pages.length, lastVisibleRow * columnCount)
  const visiblePages = useMemo(() => tileProps.pages.slice(pageStart, pageEnd), [pageEnd, pageStart, tileProps.pages])
  const handleScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const nextScrollTop = event.currentTarget.scrollTop
    setScrollTop(current => (Math.floor(current / PAGE_ROW_HEIGHT) === Math.floor(nextScrollTop / PAGE_ROW_HEIGHT) ? current : nextScrollTop))
  }, [])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const updateSize = () => {
      const next = { width: viewport.clientWidth, height: viewport.clientHeight }
      setViewportSize(current => (current.width === next.width && current.height === next.height ? current : next))
    }
    updateSize()
    const observer = new ResizeObserver(updateSize)
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (scrollTargetPageId === null || columnCount < 1) return
    const targetIndex = tileProps.pages.findIndex(page => page.page_id === scrollTargetPageId)
    const viewport = viewportRef.current
    if (targetIndex < 0 || !viewport) return
    const targetTop = Math.floor(targetIndex / columnCount) * PAGE_ROW_HEIGHT
    const targetBottom = targetTop + PAGE_TILE_SIZE
    if (targetTop < viewport.scrollTop) viewport.scrollTop = targetTop
    else if (targetBottom > viewport.scrollTop + viewport.clientHeight) viewport.scrollTop = targetBottom - viewport.clientHeight
  }, [columnCount, scrollTargetKey, scrollTargetPageId, tileProps.pages])

  const handleClick = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      const target = event.target instanceof HTMLElement ? event.target.closest<HTMLButtonElement>('[data-page-id]') : null
      if (!target || !event.currentTarget.contains(target)) return
      const pageId = target.dataset.pageId
      if (pageId) onSelectPage(pageId)
    },
    [onSelectPage]
  )

  return (
    <div ref={viewportRef} className="page-map-viewport" onScroll={handleScroll}>
      <div
        className={`${pageMapClassName({ usageFill, cacheFocus, tableFocus })} virtual-window`}
        style={{ height: `${totalHeight}px` }}
        aria-label={ariaLabel}
        onClick={handleClick}
        {...tooltipContainerProps}
      >
        <PageMapTiles {...tileProps} pages={visiblePages} pageStart={pageStart} columnCount={columnCount} tableFocus={tableFocus} />
      </div>
    </div>
  )
})

function jsonDetailValue(detail: StorageDetail, omitPagePayload: boolean): JsonObject {
  const entries = Object.entries(detail).filter(([key]) => !omitPagePayload || !['raw_payload', 'raw_page', 'slots'].includes(key))
  return Object.fromEntries(entries) as JsonObject
}

function PageData({ detail }: { detail: StoragePageDetail }) {
  const slots = detail.slots ?? []
  return (
    <>
      {slots.length > 0 && (
        <div className="slot-table-wrap">
          <table className="slot-table">
            <thead>
              <tr>
                <th>槽位</th>
                <th>状态</th>
                <th>记录位置</th>
                <th>槽项位置</th>
                <th>字节</th>
                <th>记录值</th>
              </tr>
            </thead>
            <tbody>
              {slots.map(slot => (
                <tr key={slot.slot_id}>
                  <td>{slot.slot_id}</td>
                  <td>
                    <span className={slot.deleted ? 'slot-deleted' : 'slot-live'}>{slot.deleted ? '可复用' : '有效'}</span>
                  </td>
                  <td>
                    <code>{typeof slot.page_offset === 'number' ? `0x${slot.page_offset.toString(16)}` : '—'}</code>
                  </td>
                  <td>
                    <code>{typeof slot.slot_directory_offset === 'number' ? `0x${slot.slot_directory_offset.toString(16)}` : '—'}</code>
                  </td>
                  <td>{slot.record_bytes}</td>
                  <td>
                    <code>{slot.row === null ? 'NULL' : (JSON.stringify(slot.row) ?? '—')}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {slots.length === 0 && detail.type === 'heap' && <p className="stage-note">此页当前没有可见记录槽位。</p>}
    </>
  )
}

/** 页面级信息放在中央工作台，避免和选中块的局部字节混在同一抽屉。 */
function PageAssociation({
  page,
  onSelectTable,
  onOpenIndex
}: {
  page: Pick<PageHeader, 'page_id' | 'table_name' | 'index_name'>
  onSelectTable?: (tableName: string) => void
  onOpenIndex?: (indexName: string) => void
}) {
  const tableName = page.table_name?.trim() || '未知'
  const indexName = page.index_name?.trim() || '未知'
  const tableLinkable = tableName !== '未知' && tableName !== 'MASKED' && !!onSelectTable
  const indexLinkable = indexName !== '未知' && indexName !== 'MASKED' && !!onOpenIndex
  return (
    <section className="storage-page-associations" aria-label="页面关联对象">
      <div className="storage-page-associations-heading">
        <strong>关联对象</strong>
        <span>可从这里跳转</span>
      </div>
      <div className="storage-page-association-grid">
        <div className="storage-page-association">
          <span>
            <Table2 size={12} />表
          </span>
          {tableLinkable ? (
            <button type="button" title={`定位表 ${tableName}`} onClick={() => onSelectTable(tableName)}>
              {tableName}
            </button>
          ) : (
            <strong className={tableName === '未知' ? 'unknown' : ''}>{tableName}</strong>
          )}
        </div>
        <div className="storage-page-association">
          <span>
            <ListTree size={12} />
            索引
          </span>
          {indexLinkable ? (
            <button type="button" title={`打开索引 ${indexName}`} onClick={() => onOpenIndex(indexName)}>
              {indexName}
            </button>
          ) : (
            <strong className={indexName === '未知' ? 'unknown' : ''}>{indexName}</strong>
          )}
        </div>
      </div>
    </section>
  )
}

function freePageOffset(value: number | null | undefined): string {
  return typeof value === 'number' ? `0x${value.toString(16)} · ${value.toLocaleString()} B` : '—'
}

/** 【前端特供】展示 FREE 页的链式后继，并允许从当前页直接定位下一空闲页。 */
function FreePageInfo({ detail, onSelectPage }: { detail: StoragePageDetail; onSelectPage?: (pageId: string) => void }) {
  const nextPageId = detail.free_page_next_id
  const hasNextPage = typeof nextPageId === 'number'
  const status = detail.free_page_error ?? (hasNextPage ? '指针有效' : detail.free_page_is_tail ? '链尾' : '兼容格式或指针不可用')
  const layoutText = detail.free_page_format === 'linked_page_v1' ? 'linked_page_v1 · MFR1 + uint64' : (detail.free_page_format ?? '—')
  return (
    <section className="selected-payload-special free-page-info" aria-label="空闲页链结构摘要">
      <div className="selected-payload-special-heading">
        <strong>空闲页链</strong>
        <span>FREE 页专用结构</span>
      </div>
      <dl>
        <div>
          <dt>布局</dt>
          <dd>
            <code>{layoutText}</code>
          </dd>
        </div>
        <div>
          <dt>当前页</dt>
          <dd>
            <code>#{detail.page_id}</code>
          </dd>
        </div>
        <div>
          <dt>下一空闲页</dt>
          <dd>
            {hasNextPage && onSelectPage ? (
              <button type="button" className="free-page-next-link" onClick={() => onSelectPage(String(nextPageId))}>
                <code>#{nextPageId}</code>
                <ArrowRight size={12} />
              </button>
            ) : (
              <code>{hasNextPage ? `#${nextPageId}` : '链尾（NULL）'}</code>
            )}
          </dd>
        </div>
        <div>
          <dt>下一页偏移</dt>
          <dd>
            <code>{hasNextPage ? freePageOffset(detail.free_page_next_offset) : '—'}</code>
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>
            <code>{status}</code>
          </dd>
        </div>
      </dl>
    </section>
  )
}

/** 【前端特供】展示可扩展命名页目录链的当前页与目录项。 */
function DirectoryPageInfo({ detail, onSelectPage }: { detail: StoragePageDetail; onSelectPage?: (pageId: string) => void }) {
  const directory: DirectoryPageInfo | undefined = detail.directory
  if (!directory) return null
  const nextPageId = directory.next_page_id
  const hasNextPage = typeof nextPageId === 'number'
  return (
    <section className="selected-payload-special directory-page-info" aria-label="命名页目录结构摘要">
      <div className="selected-payload-special-heading">
        <strong>命名页目录</strong>
        <span>可扩展 DIRECTORY 页链</span>
      </div>
      <dl>
        <div>
          <dt>格式</dt>
          <dd>
            <code>{directory.format}</code>
          </dd>
        </div>
        <div>
          <dt>当前页</dt>
          <dd>
            <code>#{detail.page_id}</code>
          </dd>
        </div>
        <div>
          <dt>目录根页</dt>
          <dd>
            <code>{typeof directory.root_page_id === 'number' ? `#${directory.root_page_id}` : '未设置'}</code>
          </dd>
        </div>
        <div>
          <dt>下一目录页</dt>
          <dd>
            {hasNextPage && onSelectPage ? (
              <button type="button" className="free-page-next-link" onClick={() => onSelectPage(String(nextPageId))}>
                <code>#{nextPageId}</code>
                <ArrowRight size={12} />
              </button>
            ) : (
              <code>{hasNextPage ? `#${nextPageId}` : '链尾（NULL）'}</code>
            )}
          </dd>
        </div>
      </dl>
      {directory.entries.length > 0 ? (
        <div className="directory-entry-table-wrap">
          <table className="directory-entry-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>页号</th>
              </tr>
            </thead>
            <tbody>
              {directory.entries.map(entry => (
                <tr key={`${entry.name}-${entry.page_id}`}>
                  <td>
                    <code>{entry.name}</code>
                  </td>
                  <td>
                    {onSelectPage ? (
                      <button type="button" className="free-page-next-link" onClick={() => onSelectPage(String(entry.page_id))}>
                        <code>#{entry.page_id}</code>
                        <ArrowRight size={12} />
                      </button>
                    ) : (
                      <code>#{entry.page_id}</code>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="stage-note">此目录页没有逻辑命名项。</p>
      )}
      {directory.error && <p className="hint">{directory.error}</p>}
    </section>
  )
}

function PagePayloadWorkbench({
  detail,
  jsonDetail,
  raw,
  onSelectPage
}: {
  detail: StoragePageDetail
  jsonDetail: JsonObject | null
  raw: RawPayload | null
  onSelectPage?: (pageId: string) => void
}) {
  return (
    <section className="storage-page-workbench" aria-label="页面级信息">
      <div className="storage-page-workbench-heading">
        <strong>页面信息</strong>
      </div>
      {detail.type === 'free' && <FreePageInfo detail={detail} onSelectPage={onSelectPage} />}
      {detail.type === 'directory' && <DirectoryPageInfo detail={detail} onSelectPage={onSelectPage} />}
      {jsonDetail && (
        <details className="storage-json-details" open>
          <summary>结构化页字段</summary>
          <JsonTree key={`page-${detail.page_id}`} value={jsonDetail} />
        </details>
      )}
      {raw && (
        <RawPreview
          payload={raw}
          title={
            detail.type === 'catalog'
              ? '目录链 Payload'
              : detail.type === 'directory'
                ? '命名页目录 Payload'
                : detail.type === 'free'
                  ? 'Free-list 指针 Payload'
                  : 'Payload'
          }
          pageWide
          pageType={detail.type}
        />
      )}
      {detail.raw_page && <RawPreview payload={detail.raw_page} title="整页字节" pageWide pageType={detail.type} />}
      {detail.note && <p className="hint">{detail.note}</p>}
    </section>
  )
}

function RawPreview({
  payload,
  title = '原始 payload 预览',
  pageWide = false,
  pageType
}: {
  payload: RawPayload
  title?: string
  pageWide?: boolean
  pageType?: string
}) {
  const inspection = inspectStoragePayload(decodeRawPayloadBytes(payload), false, pageType)
  return (
    <details className={`raw-preview ${pageWide ? 'page-wide-preview' : ''}`}>
      <summary>
        <span>{title}</span>
        <small>
          {payload.encoding} · {payload.preview_bytes} / {payload.size_bytes} B{payload.truncated ? ' · 已截断' : ''}
        </small>
      </summary>
      <div className="raw-tabs">
        {inspection && (
          <section className="selected-payload-special raw-payload-inspection" aria-label="Payload 结构解析">
            <div className="selected-payload-special-heading">
              <strong>{inspection.title}</strong>
              <span>{inspection.summary}</span>
            </div>
            <dl>
              {inspection.fields.map(field => (
                <div key={field.label}>
                  <dt>{field.label}</dt>
                  <dd>
                    <code>{field.value}</code>
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        )}
        <pre>{payload.text ?? payload.hex}</pre>
        <details>
          <summary>Hex</summary>
          <pre>{payload.hex}</pre>
        </details>
        <details>
          <summary>Base64</summary>
          <pre>{payload.base64}</pre>
        </details>
        <p>{payload.note}</p>
      </div>
    </details>
  )
}

function indexKey(value: JsonValue[] | null): string {
  return value === null ? '—' : JSON.stringify(value)
}

function indexRowId(rowId: [number, number]): string {
  return `(${rowId[0]}, ${rowId[1]})`
}

function indexKeyRange(node: IndexNode): string {
  const minKey = indexKey(node.min_key)
  const maxKey = indexKey(node.max_key)
  return minKey === maxKey ? minKey : `${minKey} → ${maxKey}`
}

interface IndexLayoutItem {
  node: IndexNode
  children: IndexLayoutItem[]
  expanded: boolean
  x: number
  y: number
  width: number
  depth: number
}

interface IndexLayout {
  items: IndexLayoutItem[]
  edges: { from: IndexLayoutItem; to: IndexLayoutItem }[]
  leaves: IndexLayoutItem[]
  width: number
  height: number
}

const INDEX_NODE_WIDTH = 152
const INDEX_NODE_HEIGHT = 64
const INDEX_NODE_GAP = 24
const INDEX_LEVEL_GAP = 64
const INDEX_LAYOUT_PADDING = 28
const INDEX_DEFAULT_EXPANSION_LIMIT = 24

function defaultIndexCollapsed(value: IndexSnapshot): Set<number> {
  const rootPageId = value.root_page_id
  return new Set(
    (value.nodes ?? [])
      .filter(node => node.children.length > 0 && (node.page_id !== rootPageId || node.child_count > INDEX_DEFAULT_EXPANSION_LIMIT))
      .map(node => node.page_id)
  )
}

function buildIndexLayout(value: IndexSnapshot, collapsed: Set<number>): IndexLayout {
  const nodesById = new Map((value.nodes ?? []).map(node => [node.page_id, node]))
  const roots =
    value.root_page_id === null
      ? (value.nodes ?? []).filter(node => node.parent_page_id === null)
      : [nodesById.get(value.root_page_id)].filter((node): node is IndexNode => node !== undefined)
  const items: IndexLayoutItem[] = []
  const edges: { from: IndexLayoutItem; to: IndexLayoutItem }[] = []
  const leaves: IndexLayoutItem[] = []
  let maxDepth = 0

  const createItem = (node: IndexNode, depth: number): IndexLayoutItem => {
    const children = node.children
      .map(child => nodesById.get(child))
      .filter((child): child is IndexNode => child !== undefined)
      .map(child => createItem(child, depth + 1))
    return {
      node,
      children,
      expanded: children.length > 0 && !collapsed.has(node.page_id),
      x: 0,
      y: 0,
      width: INDEX_NODE_WIDTH,
      depth
    }
  }
  const measure = (item: IndexLayoutItem): number => {
    if (!item.expanded) return INDEX_NODE_WIDTH
    const childWidth = item.children.reduce((sum, child) => sum + measure(child), 0) + Math.max(0, item.children.length - 1) * INDEX_NODE_GAP
    item.width = Math.max(INDEX_NODE_WIDTH, childWidth)
    return item.width
  }
  const place = (item: IndexLayoutItem, left: number) => {
    item.x = left + item.width / 2
    item.y = INDEX_LAYOUT_PADDING + item.depth * (INDEX_NODE_HEIGHT + INDEX_LEVEL_GAP)
    maxDepth = Math.max(maxDepth, item.depth)
    items.push(item)
    if (item.node.node_type === 'leaf') leaves.push(item)
    if (!item.expanded) return
    const childWidth = item.children.reduce((sum, child) => sum + child.width, 0) + Math.max(0, item.children.length - 1) * INDEX_NODE_GAP
    let childLeft = left + (item.width - childWidth) / 2
    item.children.forEach(child => {
      place(child, childLeft)
      edges.push({ from: item, to: child })
      childLeft += child.width + INDEX_NODE_GAP
    })
  }

  let left = INDEX_LAYOUT_PADDING
  roots.forEach(root => {
    const item = createItem(root, 0)
    measure(item)
    place(item, left)
    left += item.width + INDEX_NODE_GAP
  })
  const width = Math.max(INDEX_NODE_WIDTH + INDEX_LAYOUT_PADDING * 2, left - INDEX_NODE_GAP + INDEX_LAYOUT_PADDING)
  const height = INDEX_LAYOUT_PADDING * 2 + (maxDepth + 1) * INDEX_NODE_HEIGHT + maxDepth * INDEX_LEVEL_GAP
  return { items, edges, leaves, width, height }
}

function IndexDiagramNode({
  item,
  onPage,
  onToggle
}: {
  item: IndexLayoutItem
  onPage: (pageId: number) => void
  onToggle: (pageId: number) => void
}) {
  const { node } = item
  return (
    <foreignObject x={item.x - INDEX_NODE_WIDTH / 2} y={item.y} width={INDEX_NODE_WIDTH} height={INDEX_NODE_HEIGHT}>
      <div
        className={`index-btree-node ${node.node_type}`}
        role="treeitem"
        aria-level={item.depth + 1}
        aria-expanded={item.children.length > 0 ? item.expanded : undefined}
      >
        <div className="index-btree-node-head">
          <button type="button" className="index-page-link" aria-label={`查看页面 ${node.page_id}`} onClick={() => onPage(node.page_id)}>
            页 #{node.page_id}
          </button>
          <span>{node.node_type === 'leaf' ? '叶子' : '内部'}</span>
          {item.children.length > 0 && (
            <button
              type="button"
              className="index-btree-toggle"
              aria-label={`${item.expanded ? '收起' : '展开'}页面 ${node.page_id} 的子页`}
              onClick={() => onToggle(node.page_id)}
            >
              <ChevronRight size={12} />
            </button>
          )}
        </div>
        <div className="index-btree-node-meta">
          <strong>{node.key_count.toLocaleString()} 键</strong>
          <span>{node.node_type === 'internal' ? `${node.child_count} 子页` : '叶链'}</span>
        </div>
        <code title={indexKeyRange(node)}>{indexKeyRange(node)}</code>
      </div>
    </foreignObject>
  )
}

function IndexDiagram({ value, onPage }: { value: IndexSnapshot; onPage: (pageId: number) => void }) {
  const [collapsed, setCollapsed] = useState<Set<number>>(() => defaultIndexCollapsed(value))
  const [zoom, setZoom] = useState(1)
  useEffect(() => {
    setCollapsed(defaultIndexCollapsed(value))
    setZoom(1)
  }, [value.root_page_id, value.page_count, value.total])
  const nodesById = new Map((value.nodes ?? []).map(node => [node.page_id, node]))
  const roots =
    value.root_page_id === null
      ? (value.nodes ?? []).filter(node => node.parent_page_id === null)
      : [nodesById.get(value.root_page_id)].filter((node): node is IndexNode => node !== undefined)
  if (roots.length === 0)
    return (
      <div className="index-view-empty">
        <strong>暂无落盘节点图</strong>
        <span>当前索引没有可展示的物理节点关系。</span>
      </div>
    )
  const layout = buildIndexLayout(value, collapsed)
  const toggleNode = (pageId: number) =>
    setCollapsed(current => {
      const next = new Set(current)
      if (next.has(pageId)) next.delete(pageId)
      else next.add(pageId)
      return next
    })
  const collapseAll = () => setCollapsed(new Set((value.nodes ?? []).filter(node => node.children.length > 0).map(node => node.page_id)))
  const changeZoom = (delta: number) => setZoom(current => Math.min(2, Math.max(0.5, Number((current + delta).toFixed(1)))))
  // HOW：仅在 Ctrl + 滚轮时缩放，普通滚轮仍用于浏览超宽的叶子页链。
  const handleCanvasWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey) return
    event.preventDefault()
    changeZoom(event.deltaY > 0 ? -0.1 : 0.1)
  }
  const renderWidth = Math.max(layout.width * zoom, INDEX_NODE_WIDTH)
  const renderHeight = layout.height * zoom
  return (
    <div className="index-diagram" aria-label="落盘 B+Tree 层级、键槽与叶链关系图">
      <div className="index-tree-toolbar">
        <div className="index-tree-toolbar-title">
          <strong>页层级</strong>
          <span>实线父子 · 双向虚线叶链</span>
        </div>
        <div className="index-tree-actions">
          <button type="button" className="index-tree-action" onClick={collapseAll}>
            全部收起
          </button>
          <button type="button" className="index-tree-action" onClick={() => setCollapsed(new Set())}>
            全部展开
          </button>
          <span className="index-tree-zoom" aria-label="画布缩放">
            <span>缩放</span>
            <button type="button" className="index-tree-action" title="缩小画布" aria-label="缩小画布" onClick={() => changeZoom(-0.1)}>
              −
            </button>
            <b>{Math.round(zoom * 100)}%</b>
            <button type="button" className="index-tree-action" title="放大画布" aria-label="放大画布" onClick={() => changeZoom(0.1)}>
              +
            </button>
            <button type="button" className="index-tree-action" title="重置为 100%" onClick={() => setZoom(1)}>
              适配
            </button>
          </span>
        </div>
      </div>
      <div className="index-btree-legend">
        <span>
          <i className="internal" />
          内部页
        </span>
        <span>
          <i className="leaf" />
          叶子页
        </span>
        <span>
          <i className="sibling" />
          叶链
        </span>
      </div>
      <div className="index-btree-canvas" onWheel={handleCanvasWheel} title="按住 Ctrl 滚轮缩放，普通滚轮浏览叶链">
        <svg
          role="tree"
          aria-label="B+Tree 页面层级"
          className="index-btree-svg"
          width={renderWidth}
          height={renderHeight}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          preserveAspectRatio="xMinYMin meet"
        >
          <defs>
            <marker id="index-leaf-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto-start-reverse">
              <path d="M0,0 L7,3.5 L0,7 z" fill="#71aaa2" />
            </marker>
          </defs>
          <g className="index-btree-edges">
            {layout.edges.map(edge => (
              <line
                key={`${edge.from.node.page_id}-${edge.to.node.page_id}`}
                x1={edge.from.x}
                y1={edge.from.y + INDEX_NODE_HEIGHT}
                x2={edge.to.x}
                y2={edge.to.y}
              />
            ))}
          </g>
          {layout.leaves.length > 1 && (
            <g className="index-btree-leaf-links">
              {layout.leaves.slice(0, -1).map((leaf, index) => {
                const next = layout.leaves[index + 1]
                const hasNext = leaf.node.next_page_id === next.node.page_id
                const hasPrev = next.node.prev_page_id === leaf.node.page_id
                const y = leaf.y + INDEX_NODE_HEIGHT / 2
                return hasNext || hasPrev ? (
                  <g key={`${leaf.node.page_id}-${next.node.page_id}`}>
                    <line
                      x1={leaf.x + INDEX_NODE_WIDTH / 2 + 3}
                      y1={y}
                      x2={next.x - INDEX_NODE_WIDTH / 2 - 3}
                      y2={y}
                      markerStart={hasPrev ? 'url(#index-leaf-arrow)' : undefined}
                      markerEnd={hasNext ? 'url(#index-leaf-arrow)' : undefined}
                    />
                    <title>
                      叶子页 #{leaf.node.page_id} {hasNext && hasPrev ? '↔' : hasNext ? '→' : '←'} #{next.node.page_id}
                    </title>
                  </g>
                ) : null
              })}
            </g>
          )}
          <g className="index-btree-nodes">
            {layout.items.map(item => (
              <IndexDiagramNode key={item.node.page_id} item={item} onPage={onPage} onToggle={toggleNode} />
            ))}
          </g>
        </svg>
      </div>
      {value.pages_truncated && <p className="hint">物理页过多，仅显示前 256 页。</p>}
    </div>
  )
}

function IndexEntryList({ value }: { value: IndexSnapshot }) {
  const firstEntry = value.total === 0 ? 0 : value.offset + 1
  const lastEntry = Math.min(value.offset + value.entries.length, value.total)
  return (
    <div className="index-entry-list">
      <div className="index-list-heading">
        <strong>键组</strong>
        <small>
          {firstEntry}–{lastEntry} / {value.total}
        </small>
      </div>
      {value.entries.length === 0 ? (
        <div className="index-view-empty">
          <strong>暂无索引条目</strong>
          <span>当前索引还没有可读取的键组。</span>
        </div>
      ) : (
        <div className="index-entry-table-wrap">
          <table className="index-entry-table">
            <thead>
              <tr>
                <th>#</th>
                <th>索引键</th>
                <th>行数</th>
                <th>RowId</th>
              </tr>
            </thead>
            <tbody>
              {value.entries.map((entry, index) => (
                <tr key={`${value.offset + index}-${JSON.stringify(entry.key)}`}>
                  <td className="index-entry-number">{value.offset + index + 1}</td>
                  <td>
                    <code>{indexKey(entry.key)}</code>
                  </td>
                  <td>
                    <strong>{entry.row_count.toLocaleString()}</strong>
                  </td>
                  <td>
                    <div className="index-row-id-list">
                      {entry.row_ids.slice(0, 3).map(rowId => (
                        <code key={indexRowId(rowId)}>{indexRowId(rowId)}</code>
                      ))}
                      {entry.rows_truncated && <small>+更多</small>}
                      {entry.row_ids.length === 0 && <span>—</span>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function IndexInspector({
  value,
  onPage,
  onNavigate,
  busy
}: {
  value: IndexSnapshot
  onPage: (pageId: number) => void
  onNavigate: (offset: number) => void
  busy: boolean
}) {
  const [view, setView] = useState<'diagram' | 'list'>('diagram')
  const canGoBack = value.offset > 0
  const canGoForward = value.offset + value.entries.length < value.total
  return (
    <div className="index-inspector">
      <div className="index-inspector-header">
        <div>
          <strong>{value.physical ? 'B+Tree' : '有序索引'}</strong>
          <span>{value.metadata.columns.join(', ')}</span>
        </div>
        <div className="index-view-switch" role="tablist" aria-label="索引展示方式">
          <button
            type="button"
            role="tab"
            aria-selected={view === 'diagram'}
            className={view === 'diagram' ? 'active' : ''}
            onClick={() => setView('diagram')}
          >
            <Network size={13} />
            图示
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'list'}
            className={view === 'list' ? 'active' : ''}
            onClick={() => setView('list')}
          >
            <List size={13} />
            列表
          </button>
        </div>
      </div>
      <div className="index-tree-summary">
        <span>
          根 <b>#{value.root_page_id ?? '—'}</b>
        </span>
        <span>
          高 <b>{value.height}</b>
        </span>
        <span>
          页{' '}
          <b>
            {value.page_count}
            {value.pages_truncated ? '+' : ''}
          </b>
        </span>
        <span>
          键组 <b>{value.total.toLocaleString()}</b>
        </span>
      </div>
      {view === 'diagram' ? <IndexDiagram value={value} onPage={onPage} /> : <IndexEntryList value={value} />}
      {view === 'list' && value.total > value.entries.length && (
        <div className="small-pagination index-pagination">
          <button aria-label="上一页索引条目" disabled={!canGoBack || busy} onClick={() => onNavigate(Math.max(0, value.offset - value.limit))}>
            <ChevronLeft size={13} />
          </button>
          <span>
            第 {Math.floor(value.offset / Math.max(1, value.limit)) + 1} / {Math.ceil(value.total / Math.max(1, value.limit))} 页
          </span>
          <button aria-label="下一页索引条目" disabled={!canGoForward || busy} onClick={() => onNavigate(value.offset + value.limit)}>
            <ChevronRight size={13} />
          </button>
        </div>
      )}
    </div>
  )
}

function IndexScreen({
  name,
  value,
  loading,
  onBack,
  onPage,
  onNavigate,
  busy,
  jsonDetail,
  raw,
  headingRef
}: {
  name: string
  value: IndexSnapshot | null
  loading: boolean
  onBack: () => void
  onPage: (pageId: number) => void
  onNavigate: (offset: number) => void
  busy: boolean
  jsonDetail: JsonObject | null
  raw: RawPayload | null
  headingRef: { current: HTMLHeadingElement | null }
}) {
  return (
    <section className="storage-index-screen" aria-label={`索引 ${name}`} aria-busy={loading}>
      <div className="storage-index-screen-header">
        <button type="button" className="storage-index-back" aria-label="返回索引列表" title="返回索引列表" onClick={onBack}>
          <ArrowLeft size={14} />
          索引
        </button>
        <div className="storage-index-title">
          <h2 ref={headingRef} tabIndex={-1}>
            {name}
          </h2>
        </div>
      </div>
      {loading ? (
        <div className="inspector-empty index-screen-loading" role="status">
          <RefreshCw className="spin" />
          <strong>读取索引…</strong>
        </div>
      ) : value ? (
        <>
          <IndexInspector value={value} onPage={onPage} onNavigate={onNavigate} busy={busy} />
          {jsonDetail && (
            <details className="storage-json-details">
              <summary>原始响应</summary>
              <JsonTree value={jsonDetail} />
            </details>
          )}
          {raw && <RawPreview payload={raw} />}
        </>
      ) : (
        <div className="index-view-empty">
          <strong>索引读取失败</strong>
          <span>未返回可展示的索引结构。</span>
        </div>
      )}
    </section>
  )
}

export default function StoragePanel({
  fullMode = false,
  onExit,
  selectedTable = null,
  onSelectTable,
  active = true,
  refreshToken = 0
}: StoragePanelProps) {
  // 快照、缓存与刷新忙态
  const [snapshot, setSnapshot] = useState<StorageSnapshot | null>(null)
  const [cacheSnapshot, setCacheSnapshot] = useState<StorageCacheSnapshot | null>(null)
  const [error, setError] = useState('')
  const [denied, setDenied] = useState(false)
  const [snapshotBusy, setSnapshotBusy] = useState(false)
  const [cacheBusy, setCacheBusy] = useState(false)
  const [policyBusy, setPolicyBusy] = useState(false)
  const [policyDraft, setPolicyDraft] = useState<ReplacementPolicy | null>(null)
  const [policyNotice, setPolicyNotice] = useState('')
  const [protectionBusy, setProtectionBusy] = useState(false)
  const [protectionNotice, setProtectionNotice] = useState('')
  const [demoBusy, setDemoBusy] = useState(false)
  const [demoResult, setDemoResult] = useState<StorageBufferPoolDemo | null>(null)
  const [capacityBusy, setCapacityBusy] = useState(false)
  const [capacityDraft, setCapacityDraft] = useState<string | null>(null)
  const [capacityNotice, setCapacityNotice] = useState('')
  const [incrementalBusy, setIncrementalBusy] = useState(false)
  const [pageRefreshBusy, setPageRefreshBusy] = useState(false)
  const [detailBusy, setDetailBusy] = useState(false)
  const [snapshotProgress, setSnapshotProgress] = useState({ loaded: 0, total: 0 })
  const [detailLoading, setDetailLoading] = useState(false)

  // 页签与地图显示选项
  const [tab, setTab] = useState<StorageTab>('pages')
  const [cacheFocus, setCacheFocus] = useState(false)
  const [pageUsageVisual, setPageUsageVisual] = useState<PageUsageVisual>('color')

  // 选中对象与详情：选中页、选中块、索引页联动
  const [detail, setDetail] = useState<StorageDetail | null>(null)
  const [detailTitle, setDetailTitle] = useState('')
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null)
  const [selected, setSelected] = useState<StorageSelection | null>(null)
  const [detailOffset, setDetailOffset] = useState(0)
  const [detailTotal, setDetailTotal] = useState(0)
  const [cellSelection, setCellSelection] = useState<PageGridSelection | null>(null)
  const [indexPageIdsByName, setIndexPageIdsByName] = useState<Record<string, number[]>>({})
  const [indexBindingError, setIndexBindingError] = useState('')

  // HOW：请求编号 ref 用来作废过期响应；焦点 ref 用于关闭详情后把焦点还给列表项。
  const indexButtonRefs = useRef(new Map<string, HTMLButtonElement>())
  const detailHeadingRef = useRef<HTMLHeadingElement>(null)
  const indexHeadingRef = useRef<HTMLHeadingElement>(null)
  const snapshotRequestRef = useRef(0)
  const changeRequestRef = useRef(0)
  const cacheRequestRef = useRef(0)
  const indexRequestRef = useRef(0)
  const activeRefreshRef = useRef(false)
  const detailRequestRef = useRef(0)
  const slotDetailRequestRef = useRef(0)
  const { tooltip, tooltipContainerProps } = useStorageTooltip()
  const detailPanelPane = useResizableWidth({ initialWidth: null, minWidth: 320, maxWidth: 760, edge: 'left' })
  const busy =
    snapshotBusy || cacheBusy || policyBusy || protectionBusy || demoBusy || capacityBusy || incrementalBusy || pageRefreshBusy || detailBusy
  const refreshSnapshot = useCallback(async () => {
    const requestId = ++snapshotRequestRef.current
    setSnapshotBusy(true)
    setSnapshotProgress({ loaded: 0, total: 0 })
    setError('')
    setDenied(false)
    try {
      const value = await loadStorageSnapshot(progress => {
        if (requestId !== snapshotRequestRef.current) return
        setSnapshotProgress({ loaded: progress.loaded, total: progress.total })
        setSnapshot(progress.snapshot)
      })
      if (requestId === snapshotRequestRef.current) {
        setSnapshot(value)
        setCacheSnapshot({
          snapshot_at: value.snapshot_at,
          readonly: value.readonly,
          buffer_pool: value.buffer_pool,
          io: value.io,
          note: '来自页面地图快照。切换到缓存页签或手动刷新可读取最新运行态。'
        })
      }
    } catch (error) {
      if (requestId === snapshotRequestRef.current) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      }
    } finally {
      if (requestId === snapshotRequestRef.current) setSnapshotBusy(false)
    }
  }, [])
  const refreshCache = useCallback(async () => {
    const requestId = ++cacheRequestRef.current
    setCacheBusy(true)
    setError('')
    setDenied(false)
    try {
      const value = await api<StorageCacheSnapshot>('/api/storage/cache?offset=0&limit=100')
      if (requestId === cacheRequestRef.current) setCacheSnapshot(value)
    } catch (error) {
      if (requestId === cacheRequestRef.current) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      }
    } finally {
      if (requestId === cacheRequestRef.current) setCacheBusy(false)
    }
  }, [])
  const changeReplacementPolicy = useCallback(
    async (replacementPolicy: ReplacementPolicy) => {
      if (policyBusy || busy) return
      setPolicyBusy(true)
      setError('')
      setDenied(false)
      setPolicyNotice('')
      try {
        const value = await api<StoragePolicyChange>('/api/storage/cache/policy', {
          replacement_policy: replacementPolicy
        })
        setPolicyDraft(value.replacement_policy)
        setPolicyNotice(
          value.changed
            ? `已切换为 ${value.replacement_policy.toUpperCase()}；下一次缓存淘汰开始生效。`
            : `当前已使用 ${value.replacement_policy.toUpperCase()}。`
        )
        setCacheSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
        setSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
      } catch (error) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      } finally {
        setPolicyBusy(false)
      }
    },
    [busy, policyBusy]
  )
  const changePageTypeProtection = useCallback(
    async (enabled: boolean) => {
      if (protectionBusy || busy) return
      setProtectionBusy(true)
      setError('')
      setDenied(false)
      setProtectionNotice('')
      try {
        const value = await api<StorageProtectionChange>('/api/storage/cache/protection', {
          protect_page_types: enabled
        })
        setProtectionNotice(enabled ? '页面类型保护已热加载；下一次淘汰优先回收 HEAP 页。' : '页面类型保护已关闭；下一次淘汰恢复普通队列顺序。')
        setCacheSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
        setSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
      } catch (error) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      } finally {
        setProtectionBusy(false)
      }
    },
    [busy, protectionBusy]
  )
  const runBufferPoolDemo = useCallback(
    async (compare: boolean) => {
      if (demoBusy || busy) return
      setDemoBusy(true)
      setError('')
      setDenied(false)
      try {
        const value = await api<StorageBufferPoolDemo>('/api/storage/cache/demo', {
          compare,
          prime_rounds: 3,
          scan_rounds: 2,
          probe_rounds: 1,
          cycles: 2
        })
        setDemoResult(value)
        if (!compare) {
          setCacheSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
          setSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
        }
      } catch (error) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      } finally {
        setDemoBusy(false)
      }
    },
    [busy, demoBusy]
  )
  const changeCapacity = useCallback(
    async (capacity: number) => {
      if (capacityBusy || busy) return
      if (!Number.isInteger(capacity) || capacity < 1 || capacity > 4096) {
        setCapacityNotice('缓存页数范围应为 1–4096。')
        return
      }
      setCapacityBusy(true)
      setError('')
      setDenied(false)
      setCapacityNotice('')
      try {
        const value = await api<StorageResizeChange>('/api/storage/cache/resize', {
          buffer_pool_size: capacity
        })
        setCapacityDraft(null)
        setCapacityNotice(
          value.changed ? `已调整为 ${value.capacity} 页；本次缩容淘汰 ${value.evicted_pages} 页。` : `当前已使用 ${value.capacity} 页。`
        )
        setCacheSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
        setSnapshot(current => (current ? { ...current, snapshot_at: value.snapshot_at, buffer_pool: value.buffer_pool } : current))
      } catch (error) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      } finally {
        setCapacityBusy(false)
      }
    },
    [busy, capacityBusy]
  )
  const refreshIndexCatalog = useCallback(async () => {
    const requestId = ++indexRequestRef.current
    setDetailBusy(true)
    setError('')
    setDenied(false)
    try {
      const value = await api<StorageIndexSnapshot>('/api/storage/indexes?limit=100')
      if (requestId !== indexRequestRef.current) return
      setSnapshot(current => (current ? { ...current, indexes: value.indexes, snapshot_at: value.snapshot_at } : current))
    } catch (error) {
      if (requestId === indexRequestRef.current) {
        setError(errorMessage(error))
        setDenied(error instanceof ApiError && error.status === 403)
      }
    } finally {
      if (requestId === indexRequestRef.current) setDetailBusy(false)
    }
  }, [])
  useEffect(() => {
    void refreshSnapshot()
    return () => {
      snapshotRequestRef.current += 1
      detailRequestRef.current += 1
      changeRequestRef.current += 1
      cacheRequestRef.current += 1
      indexRequestRef.current += 1
    }
  }, [refreshSnapshot])
  const refreshPage = useCallback(
    async (pageId: string, offset = detailOffset) => {
      const requestId = ++detailRequestRef.current
      setPageRefreshBusy(true)
      setError('')
      setDenied(false)
      try {
        const data = await api<StoragePageDetail>(`/api/storage/pages/${encodeURIComponent(pageId)}?offset=${offset}&limit=40`)
        if (requestId !== detailRequestRef.current) return
        setDetail(data)
        setSelected({ kind: 'pages', value: pageId })
        setSelectedPageId(pageId)
        setDetailOffset(offset)
        setDetailTotal(Number(data.total_slots ?? 0))
        setCellSelection(null)
        setSnapshot(current => (current ? { ...current, pages: current.pages.map(page => (page.page_id === data.page_id ? data : page)) } : current))
      } catch (error) {
        if (requestId === detailRequestRef.current) {
          setError(errorMessage(error))
          setDenied(error instanceof ApiError && error.status === 403)
        }
      } finally {
        if (requestId === detailRequestRef.current) setPageRefreshBusy(false)
      }
    },
    [detailOffset]
  )
  const refreshPageChanges = useCallback(async () => {
    const base = snapshot
    if (!base) return
    const requestId = ++changeRequestRef.current
    const since = base.storage_revision ?? 0
    setIncrementalBusy(true)
    try {
      const value = await api<StoragePageChanges>(`/api/storage/changes?since=${since}&limit=500`)
      if (requestId !== changeRequestRef.current) return
      if (value.truncated) {
        await refreshSnapshot()
        return
      }
      setSnapshot(current => {
        if (!current) return current
        const pagesById = new Map(current.pages.map(page => [page.page_id, page]))
        for (const page of value.pages) pagesById.set(page.page_id, page)
        return {
          ...current,
          pages: [...pagesById.values()].sort((left, right) => left.page_id - right.page_id),
          storage_revision: value.revision,
          snapshot_at: value.changed_page_ids.length ? value.snapshot_at : current.snapshot_at,
          total: value.total ?? current.total,
          page_size: value.page_size ?? current.page_size,
          free_pages: value.free_pages ?? current.free_pages,
          free_page_count: value.free_page_count ?? current.free_page_count,
          free_list_head: value.free_list_head ?? current.free_list_head,
          free_list_format: value.free_list_format ?? current.free_list_format,
          catalog_page_id: value.catalog_page_id !== undefined ? value.catalog_page_id : current.catalog_page_id,
          directory_root_page: value.directory_root_page !== undefined ? value.directory_root_page : current.directory_root_page,
          directory_page_count: value.directory_page_count ?? current.directory_page_count,
          named_pages: value.named_pages ?? current.named_pages
        }
      })
    } catch (error) {
      if (requestId === changeRequestRef.current) setError(errorMessage(error))
    } finally {
      if (requestId === changeRequestRef.current) setIncrementalBusy(false)
    }
  }, [refreshSnapshot, snapshot])
  const inspect = useCallback(async (kind: InspectableStorageKind, value: string, start = 0, indexName?: string) => {
    const requestId = ++detailRequestRef.current
    setDetailBusy(true)
    setDetailLoading(true)
    setDetail(null)
    setSelected({ kind, value })
    setDetailOffset(start)
    setDetailTotal(0)
    setDetailTitle(kind === 'pages' ? '' : `索引 ${value}`)
    setError('')
    if (kind === 'pages') {
      setSelectedPageId(value)
      // WHY：重新打开页面时先清掉旧块选中态，避免异步加载期间抽屉短暂展示上一页的块信息。
      ++slotDetailRequestRef.current
      setCellSelection(null)
    }
    try {
      const indexQuery = kind === 'pages' && indexName ? `&index_name=${encodeURIComponent(indexName)}` : ''
      const resolveIndexQuery = kind === 'pages' && !indexName ? '&resolve_index=0' : ''
      const data = await api<StorageDetail>(
        `/api/storage/${kind}/${encodeURIComponent(value)}?offset=${start}&limit=40${indexQuery}${resolveIndexQuery}`
      )
      if (requestId !== detailRequestRef.current) return
      setDetail(data)
      setSelected({ kind, value })
      if (kind === 'pages') setSelectedPageId(value)
      setDetailOffset(start)
      setDetailTotal(Number(isPageDetail(data) ? (data.total_slots ?? 0) : data.total))
      setDetailTitle(kind === 'pages' ? '' : `索引 ${value}`)
    } catch (error) {
      if (requestId === detailRequestRef.current) setError(errorMessage(error))
    } finally {
      if (requestId === detailRequestRef.current) {
        setDetailBusy(false)
        setDetailLoading(false)
      }
    }
  }, [])
  const refreshActiveView = useCallback(
    async (_automatic = false) => {
      if (busy || activeRefreshRef.current) return
      activeRefreshRef.current = true
      try {
        if (tab === 'pages') {
          const jobs: Promise<void>[] = [refreshPageChanges()]
          if (cacheFocus) jobs.push(refreshCache())
          if (selected?.kind === 'pages') jobs.push(refreshPage(selected.value))
          await Promise.all(jobs)
        } else if (tab === 'buffer') {
          await refreshCache()
        } else if (tab === 'indexes') {
          if (selected?.kind === 'indexes') await inspect('indexes', selected.value, detailOffset)
          else await refreshIndexCatalog()
        }
      } finally {
        activeRefreshRef.current = false
      }
    },
    [busy, cacheFocus, detailOffset, inspect, refreshCache, refreshIndexCatalog, refreshPage, refreshPageChanges, selected, tab]
  )
  useEffect(() => {
    if (!active || refreshToken === 0) return
    void refreshActiveView(true)
    // WHY：SQL 完成只触发当前存储页签的轻量刷新，不重载整张页面地图。
  }, [active, refreshToken])
  const selectPage = useCallback(
    (pageId: string) => {
      // WHY：切换页必须一次点击就加载新页详情；旧实现只清空 detail/selected/cellSelection，
      // 表现为“第一次点击折叠工作区与抽屉，第二次点击才切换”。
      const pageLoaded = selected?.kind === 'pages' && detail !== null && isPageDetail(detail)
      if (pageTileAction(selectedPageId, pageId, pageLoaded) === 'noop') return
      void inspect('pages', pageId)
    },
    [detail, inspect, selected, selectedPageId]
  )
  function selectTab(value: StorageTab) {
    setTab(value)
    setDetail(null)
    setSelected(null)
    setSelectedPageId(null)
    setDetailLoading(false)
    setDetailBusy(false)
    ++detailRequestRef.current
    if (value === 'pages') void refreshPageChanges()
    if (value === 'buffer') void refreshCache()
    if (value === 'indexes') void refreshIndexCatalog()
  }
  const pageDetail = selected?.kind === 'pages' && detail && isPageDetail(detail) ? detail : null
  const indexDetail = selected?.kind === 'indexes' && detail && isIndexDetail(detail) ? detail : null
  const handleCellDetail = useCallback(
    (selection: PageGridSelection | null) => {
      const requestId = ++slotDetailRequestRef.current
      setCellSelection(selection)
      if (!selection || selected?.kind !== 'pages' || selection.slotIds.length === 0) return
      const pageId = selected.value
      const missingSlotIds = selection.slotIds
        .filter(slotId => !selection.slotDetails.some(slot => slot.slot_id === slotId && slotRecordLoaded(slot)))
        .slice(0, 12)
      if (missingSlotIds.length === 0) return
      const selectionKey = `${selection.cellIndex}:${selection.slotIds.join(',')}`
      void Promise.all(
        missingSlotIds.map(async slotId => {
          try {
            const value = await api<StoragePageDetail>(`/api/storage/pages/${encodeURIComponent(pageId)}?offset=${slotId}&limit=1`)
            return value.slots?.find(slot => slot.slot_id === slotId) ?? null
          } catch {
            return null
          }
        })
      ).then(rows => {
        if (requestId !== slotDetailRequestRef.current) return
        const loaded = rows.filter((slot): slot is StorageSlot => slot !== null)
        if (loaded.length === 0) return
        setCellSelection(current => {
          if (!current || `${current.cellIndex}:${current.slotIds.join(',')}` !== selectionKey) return current
          const byId = new Map<number, StorageSlot>(current.slotDetails.map(slot => [slot.slot_id, slot]))
          for (const slot of loaded) byId.set(slot.slot_id, slot)
          return {
            ...current,
            slotDetails: current.slotIds.map(slotId => byId.get(slotId)).filter((slot): slot is StorageSlot => slot !== undefined)
          }
        })
      })
    },
    [selected]
  )
  function closeDetail() {
    const pageId = selected?.kind === 'pages' ? selected.value : null
    const indexName = selected?.kind === 'indexes' ? selected.value : null
    if (pageId) {
      // WHY：页级数据已在中央工作台展示，关闭抽屉只取消块选中，不能清空当前页面。
      ++slotDetailRequestRef.current
      setCellSelection(null)
      return
    }
    ++detailRequestRef.current
    setDetail(null)
    setSelected(null)
    setDetailLoading(false)
    setDetailBusy(false)
    if (indexName) window.requestAnimationFrame(() => indexButtonRefs.current.get(indexName)?.focus({ preventScroll: true }))
  }
  useEffect(() => {
    if (!detail) return
    if (selected?.kind === 'indexes') indexHeadingRef.current?.focus({ preventScroll: true })
    else detailHeadingRef.current?.focus({ preventScroll: true })
  }, [detail, selected])
  useEffect(() => {
    if (!detail && !detailLoading) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDetail()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [detail, detailLoading, selected])
  const raw = pageDetail ? rawValue(pageDetail.raw_payload) : null
  const jsonDetail = useMemo(() => (detail ? jsonDetailValue(detail, pageDetail !== null) : null), [detail, pageDetail])
  const currentCache = cacheSnapshot?.buffer_pool ?? snapshot?.buffer_pool
  const currentCapacity = currentCache?.stats.capacity ?? 64
  const pendingCapacity = capacityDraft === null ? currentCapacity : Number(capacityDraft)
  const capacityValid = Number.isInteger(pendingCapacity) && pendingCapacity >= 1 && pendingCapacity <= 4096
  const displayedCapacity = capacityValid ? pendingCapacity : currentCapacity
  const cacheBytes = displayedCapacity * (snapshot?.page_size ?? 4096)
  const cacheSizeLabel = cacheBytes >= 1024 * 1024 ? `${(cacheBytes / (1024 * 1024)).toFixed(1)} MiB` : `${Math.round(cacheBytes / 1024)} KiB`
  const demoRows = demoResult?.kind === 'compare' ? (demoResult.results ?? []) : demoResult?.result ? [demoResult.result] : []
  const demoReference = demoRows[0]
  const cacheView = useMemo(() => {
    const evictionOrder = currentCache?.eviction_order ?? []
    const frames = currentCache?.frames ?? []
    const evictionRankByPageId = new Map(evictionOrder.map((pageId, index) => [pageId, index + 1] as const))
    const cacheFrameByPageId = new Map(frames.map(frame => [frame.page_id, frame] as const))
    const cachedPageIds = new Set([...frames.map(frame => frame.page_id), ...evictionOrder])
    const orderedCacheFrames = [...frames].sort((left, right) => {
      const leftRank = evictionRankByPageId.get(left.page_id) ?? Number.MAX_SAFE_INTEGER
      const rightRank = evictionRankByPageId.get(right.page_id) ?? Number.MAX_SAFE_INTEGER
      return leftRank - rightRank || left.page_id - right.page_id
    })
    return { evictionRankByPageId, cacheFrameByPageId, cachedPageIds, orderedCacheFrames }
  }, [currentCache])
  const { evictionRankByPageId, cacheFrameByPageId, cachedPageIds, orderedCacheFrames } = cacheView
  const visiblePageTypes = useMemo(() => PAGE_TYPE_LEGEND.filter(item => snapshot?.pages.some(page => page.type === item.type)), [snapshot?.pages])
  const usageFill = pageUsageVisual === 'fill'
  const linkedDataPageIds = useMemo(() => new Set(selectedTable?.page_ids.map(pageId => Number(pageId)) ?? []), [selectedTable?.page_ids])
  const linkedIndexRootIds = useMemo(
    () => new Set((selectedTable?.indexes ?? []).map(index => index.root_page_id).filter((pageId): pageId is number => typeof pageId === 'number')),
    [selectedTable?.indexes]
  )
  const linkedIndexPageIds = useMemo(
    () => new Set<number>([...linkedIndexRootIds, ...Object.values(indexPageIdsByName).flat()]),
    [indexPageIdsByName, linkedIndexRootIds]
  )
  const linkedPageIds = useMemo(() => new Set([...linkedDataPageIds, ...linkedIndexPageIds]), [linkedDataPageIds, linkedIndexPageIds])
  const firstLinkedPageId = useMemo(
    () => (selectedTable && snapshot ? (snapshot.pages.find(page => linkedPageIds.has(page.page_id))?.page_id ?? null) : null),
    [linkedPageIds, selectedTable, snapshot]
  )
  const selectedIndexBindingsKey = selectedTable?.indexes.map(index => `${index.name}\u0002${index.root_page_id ?? ''}`).join('\u0001') ?? ''
  useEffect(() => {
    let cancelled = false
    const indexNames = selectedIndexBindingsKey ? selectedIndexBindingsKey.split('\u0001').map(binding => binding.split('\u0002', 1)[0]) : []
    setIndexPageIdsByName({})
    setIndexBindingError('')
    if (indexNames.length === 0) {
      return () => {
        cancelled = true
      }
    }
    void Promise.all(
      indexNames.map(async name => {
        try {
          const value = await api<IndexSnapshot>(`/api/storage/indexes/${encodeURIComponent(name)}?limit=1`)
          return { name, pageIds: value.all_page_ids ?? value.page_ids, failed: false }
        } catch {
          return { name, pageIds: [] as number[], failed: true }
        }
      })
    ).then(results => {
      if (cancelled) return
      const next: Record<string, number[]> = {}
      let failed = false
      for (const result of results) {
        next[result.name] = result.pageIds
        failed = failed || result.failed
      }
      setIndexPageIdsByName(next)
      setIndexBindingError(failed ? '部分索引页未加载' : '')
    })
    return () => {
      cancelled = true
    }
  }, [selectedIndexBindingsKey, snapshot?.snapshot_at])
  const indexScreenOpen = tab === 'indexes' && selected?.kind === 'indexes'
  const selectedIndexName = selected?.kind === 'indexes' ? selected.value : ''
  const inspectPageFromIndex = useCallback(
    (pageId: number) => {
      if (!selectedIndexName) return
      // WHY：索引页点击应明确切到页面页签；否则只改 selected 会卸载索引视图，却仍停留在索引页签。
      setTab('pages')
      void inspect('pages', String(pageId), 0, selectedIndexName)
    },
    [inspect, selectedIndexName]
  )
  const openIndex = useCallback(
    (indexName: string) => {
      void inspect('indexes', indexName)
    },
    [inspect]
  )
  const selectedPageHeader = selectedPageId && snapshot ? (snapshot.pages.find(page => String(page.page_id) === selectedPageId) ?? null) : null
  const detailPanelOpen = selected?.kind === 'pages' && pageDetail !== null && cellSelection !== null
  // HOW：页面地图页签常驻右侧详情栏；未选中块时只给占位提示，布局不因开关抽屉而重排。
  const railOpen = tab === 'pages'
  const detailPanelHeading = pageDetail ? (cellSelection ? `块 #${cellSelection.cellIndex}` : '块') : detailTitle
  const detailPanel = railOpen && (
    <aside className={storageDetailPanelClassName(detailPanelPane.resizing)} aria-labelledby="storage-panel-title" aria-busy={detailLoading}>
      <div
        className="storage-detail-resize-handle"
        role="separator"
        aria-label="调整存储详情宽度"
        aria-orientation="vertical"
        aria-valuemin={320}
        aria-valuemax={760}
        aria-valuenow={detailPanelPane.width ?? undefined}
        onPointerDown={detailPanelPane.beginResize}
      />
      <div className="storage-detail-panel-header">
        <div>
          <h3 id="storage-panel-title" ref={detailHeadingRef} tabIndex={-1}>
            {detailPanelOpen ? detailPanelHeading : '块详情'}
          </h3>
        </div>
        {detailPanelOpen && (
          <div className="storage-detail-panel-actions">
            <button className="icon-button" aria-label="关闭存储详情" onClick={closeDetail} disabled={detailLoading || pageRefreshBusy}>
              <X size={18} />
            </button>
          </div>
        )}
      </div>
      <div className="storage-detail-panel-body">
        {detailPanelOpen ? (
          <div className="storage-detail page-detail">
            {pageDetail && cellSelection && <PageGridSelectionDetail selection={cellSelection} detail={pageDetail} />}
            {selected?.kind === 'pages' && detailTotal > 40 && pageDetail?.type !== 'heap' && (
              <div className="small-pagination">
                <button
                  aria-label="上一页存储条目"
                  disabled={!detailOffset || busy}
                  onClick={() => inspect(selected.kind, selected.value, Math.max(0, detailOffset - 40))}
                >
                  <ChevronLeft size={13} />
                </button>
                <span>
                  已显示 {Math.min(detailOffset + 40, detailTotal)} / {detailTotal}
                </span>
                <button
                  aria-label="下一页存储条目"
                  disabled={detailOffset + 40 >= detailTotal || busy}
                  onClick={() => inspect(selected.kind, selected.value, detailOffset + 40)}
                >
                  <ChevronRight size={13} />
                </button>
              </div>
            )}
          </div>
        ) : (
          <p className="storage-rail-empty">
            <ListTree size={13} />
            <span>在页面画布上选一块查看页内字节。</span>
          </p>
        )}
      </div>
    </aside>
  )

  return (
    <section
      className={`storage-panel ${fullMode ? 'full-storage' : ''} ${indexScreenOpen ? 'index-screen-active' : ''}`}
      aria-label="存储检查与缓存运行态"
      aria-busy={busy}
    >
      <div
        className={`storage-mode-layout ${railOpen ? 'has-rail' : ''}`}
        style={detailPanelPane.width === null ? undefined : ({ '--storage-detail-width': `${detailPanelPane.width}px` } as CSSProperties)}
      >
        <div className="storage-scroll">
          <div className="storage-heading">
            <span>
              <LockKeyhole size={14} />
              <span className="storage-heading-title">
                <b>{indexScreenOpen ? '索引检查' : '存储检查'}</b>
                <small>{indexScreenOpen ? 'B+Tree 结构 · 键组' : '页面 · 槽位 · 页内字节'}</small>
              </span>
            </span>
            <div className="storage-heading-actions">
              {fullMode && onExit && (
                <button className="subtle" onClick={onExit}>
                  返回 SQL
                </button>
              )}
              <button className="subtle" onClick={() => void refreshActiveView(false)} disabled={busy} title="只刷新当前页签或选中对象">
                <RefreshCw size={13} className={busy ? 'spin' : ''} />
                {tab === 'pages'
                  ? selected?.kind === 'pages'
                    ? '刷新本页'
                    : '刷新地图'
                  : tab === 'buffer'
                    ? '刷新缓存'
                    : tab === 'indexes'
                      ? selected?.kind === 'indexes'
                        ? '刷新索引'
                        : '刷新索引目录'
                      : '刷新'}
              </button>
              {tab === 'pages' && selected?.kind === 'pages' && (
                <button className="subtle" onClick={() => void refreshSnapshot()} disabled={busy} title="重新加载完整页面地图">
                  地图
                </button>
              )}
            </div>
          </div>
          {error && (
            <div className="inline-error" role="alert">
              <strong>{denied ? '无权查看存储' : '读取失败'}</strong>
              <p>{error}</p>
              {denied && <span>需要全库 SELECT 和 SECURITY 权限。</span>}
            </div>
          )}
          {!snapshot && !error && (
            <div className="inspector-empty">
              <RefreshCw className="spin" />
              <span>{snapshotProgress.total ? `正在读取页面 ${snapshotProgress.loaded} / ${snapshotProgress.total}…` : '读取数据库快照…'}</span>
            </div>
          )}
          {snapshot && !denied && (
            <>
              {indexScreenOpen ? (
                <IndexScreen
                  name={selectedIndexName}
                  value={indexDetail}
                  loading={detailLoading}
                  onBack={closeDetail}
                  onPage={inspectPageFromIndex}
                  onNavigate={offset => {
                    void inspect('indexes', selectedIndexName, offset)
                  }}
                  busy={busy}
                  jsonDetail={jsonDetail}
                  raw={raw}
                  headingRef={indexHeadingRef}
                />
              ) : (
                <>
                  <div className="storage-summary-line">
                    <span>
                      <b>{snapshot.total}</b> 页 · {snapshot.page_size.toLocaleString()} B / 页
                    </span>
                    <span>空闲 {snapshot.free_page_count} 页</span>
                  </div>
                  {snapshotBusy && snapshotProgress.total > snapshotProgress.loaded && (
                    <div className="storage-load-status" role="status" aria-live="polite">
                      <div>
                        <RefreshCw size={12} className="spin" />
                        <span>
                          正在载入页面 {snapshotProgress.loaded} / {snapshotProgress.total}
                        </span>
                      </div>
                      <progress value={snapshotProgress.loaded} max={snapshotProgress.total} />
                    </div>
                  )}
                  <div className="storage-tabs">
                    {STORAGE_TAB_OPTIONS.map(([value, label]) => (
                      <button key={value} className={tab === value ? 'active' : ''} onClick={() => selectTab(value)}>
                        {label}
                      </button>
                    ))}
                  </div>
                  {tab === 'pages' && (
                    <>
                      <div className="storage-page-guide compact">
                        <div className="page-guide-copy">
                          <strong>页面</strong>
                          <small>
                            {snapshot.pages.length} / {snapshot.total} 页{indexBindingError ? ` · ${indexBindingError}` : ''}
                          </small>
                        </div>
                        <div className="page-guide-side">
                          <div className="page-color-legend" aria-label="页面类型颜色图例">
                            <span className="page-legend-label">颜色图例</span>
                            {visiblePageTypes.map(item => (
                              <span key={item.type}>
                                <i className={`page-key ${item.type}`} />
                                <span>{item.label}</span>
                              </span>
                            ))}
                            {selectedTable && (
                              <span className="table-link-legend">
                                <i className="page-key table-link" />
                                <span>关联页</span>
                              </span>
                            )}
                            <span className="page-density-legend" title={usageFill ? '填充高度代表页面使用率' : '页块颜色越深，页面使用率越高'}>
                              <i className={`page-density-swatch ${usageFill ? 'fill' : 'depth'}`} />
                              <span>{usageFill ? '低→高：填充比例' : '浅→深：颜色深度'}</span>
                            </span>
                          </div>
                          <button
                            type="button"
                            className={`page-usage-toggle ${usageFill ? 'active' : ''}`}
                            aria-pressed={usageFill}
                            aria-label="切换页面使用率的视觉编码"
                            title={`切换为${usageFill ? '颜色深度' : '填充比例'}表示页面使用率`}
                            onClick={() => setPageUsageVisual(value => (value === 'color' ? 'fill' : 'color'))}
                          >
                            <span>使用率</span>
                            <span className="page-usage-toggle-track" aria-hidden="true" />
                            <span className="page-usage-toggle-value">{usageFill ? '填充比例' : '颜色深度'}</span>
                          </button>
                          <button
                            type="button"
                            className={`cache-toggle ${cacheFocus ? 'active' : ''}`}
                            aria-pressed={cacheFocus}
                            onClick={() => {
                              setCacheFocus(value => !value)
                              if (!cacheFocus) void refreshCache()
                            }}
                          >
                            <HardDrive size={12} />
                            <span>{cacheFocus ? '关闭缓存高亮' : '高亮缓存页'}</span>
                            <small>{cachedPageIds.size} 页</small>
                          </button>
                        </div>
                      </div>
                      <PageMap
                        tooltipContainerProps={tooltipContainerProps}
                        cacheFocus={cacheFocus}
                        usageFill={usageFill}
                        tableFocus={Boolean(selectedTable)}
                        scrollTargetKey={selectedTable?.name ?? ''}
                        scrollTargetPageId={tab === 'pages' ? firstLinkedPageId : null}
                        ariaLabel={
                          selectedTable
                            ? `${selectedTable.name} 的关联页面：${[...linkedPageIds].join(', ') || '暂无'}`
                            : cacheFocus
                              ? `缓存高亮已开启，${cachedPageIds.size} 个页面在缓存中`
                              : `页面预览，使用率按${usageFill ? '填充比例' : '颜色深度'}表示`
                        }
                        pages={snapshot.pages}
                        selectedPageId={selectedPageId}
                        linkedPageIds={linkedPageIds}
                        linkedIndexPageIds={linkedIndexPageIds}
                        linkedIndexRootIds={linkedIndexRootIds}
                        cachedPageIds={cachedPageIds}
                        cachePolicy={currentCache?.policy ?? 'lru'}
                        cacheFrameByPageId={cacheFrameByPageId}
                        evictionRankByPageId={evictionRankByPageId}
                        onSelectPage={selectPage}
                      />
                      {!pageDetail && selectedPageHeader && selectedPageHeader.table_name && (
                        <div className="storage-page-association-preview">
                          <PageAssociation page={selectedPageHeader} onSelectTable={onSelectTable} onOpenIndex={openIndex} />
                        </div>
                      )}
                      {pageDetail && (
                        <PageGrid detail={pageDetail} onCellDetail={handleCellDetail} onSelectTable={onSelectTable} onOpenIndex={openIndex} />
                      )}
                      {pageDetail?.type === 'heap' && (
                        <section className="storage-slot-workbench" aria-label="槽位记录">
                          <div className="storage-slot-workbench-heading">
                            <div>
                              <strong>槽位记录</strong>
                              <span>{pageDetail.total_slots ?? pageDetail.slots?.length ?? 0} 条</span>
                            </div>
                          </div>
                          <PageData detail={pageDetail} />
                          {selected && detailTotal > 40 && (
                            <div className="slot-pagination">
                              <button
                                aria-label="上一页槽位记录"
                                disabled={!detailOffset || busy}
                                onClick={() => inspect(selected.kind, selected.value, Math.max(0, detailOffset - 40))}
                              >
                                <ChevronLeft size={13} />
                              </button>
                              <span>
                                已显示 {Math.min(detailOffset + 40, detailTotal)} / {detailTotal}
                              </span>
                              <button
                                aria-label="下一页槽位记录"
                                disabled={detailOffset + 40 >= detailTotal || busy}
                                onClick={() => inspect(selected.kind, selected.value, detailOffset + 40)}
                              >
                                <ChevronRight size={13} />
                              </button>
                            </div>
                          )}
                        </section>
                      )}
                      {pageDetail && <PagePayloadWorkbench detail={pageDetail} jsonDetail={jsonDetail} raw={raw} onSelectPage={selectPage} />}
                    </>
                  )}
                  {tab === 'buffer' && currentCache && (
                    <>
                      <div className="storage-tab-refresh-status" role="status">
                        {cacheBusy ? (
                          <>
                            <RefreshCw size={11} className="spin" />
                            正在刷新缓存运行态…
                          </>
                        ) : (
                          <>缓存快照 · {new Date(cacheSnapshot?.snapshot_at ?? snapshot.snapshot_at).toLocaleTimeString()}</>
                        )}
                      </div>
                      <div className="buffer-stats">
                        <span>
                          命中率 <b>{(currentCache.stats.hit_rate * 100).toFixed(1)}%</b>
                        </span>
                        <span>
                          {currentCache.stats.size} / {currentCache.stats.capacity} 帧 · {currentCache.policy.toUpperCase()}
                        </span>
                        <span>
                          换入 {currentCache.stats.misses} · 淘汰 {currentCache.stats.evictions}
                        </span>
                      </div>
                      <div className="storage-policy-control storage-capacity-control">
                        <div className="storage-policy-label">
                          <HardDrive size={13} />
                          <label htmlFor="storage-buffer-capacity">缓存页数</label>
                        </div>
                        <input
                          id="storage-buffer-capacity"
                          type="number"
                          min={1}
                          max={4096}
                          step={1}
                          value={capacityDraft ?? String(currentCapacity)}
                          onChange={event => setCapacityDraft(event.target.value)}
                          disabled={busy}
                          title="在线调整当前服务进程的缓存容量；缩容只淘汰未 pin 页"
                        />
                        <span className="storage-capacity-size">约 {cacheSizeLabel}</span>
                        <button
                          type="button"
                          className="subtle"
                          onClick={() => void changeCapacity(pendingCapacity)}
                          disabled={busy || !capacityValid || pendingCapacity === currentCapacity}
                        >
                          {capacityBusy ? <RefreshCw size={12} className="spin" /> : '应用'}
                        </button>
                        <small>在线调整 · 当前服务进程</small>
                      </div>
                      {capacityNotice && (
                        <div className="storage-policy-notice" role="status">
                          {capacityNotice}
                        </div>
                      )}
                      <div className="storage-policy-control">
                        <div className="storage-policy-label">
                          <Settings2 size={13} />
                          <label htmlFor="storage-replacement-policy">缓存置换策略</label>
                        </div>
                        <select
                          id="storage-replacement-policy"
                          value={policyDraft ?? (currentCache.policy as ReplacementPolicy)}
                          onChange={event => setPolicyDraft(event.target.value as ReplacementPolicy)}
                          disabled={busy}
                          title="切换后不清空现有缓存帧；下一次淘汰开始采用新策略"
                        >
                          <option value="lru">LRU · 最近最少使用</option>
                          <option value="fifo">FIFO · 先进先出</option>
                          <option value="2q">2Q · 抗扫描污染</option>
                        </select>
                        <button
                          type="button"
                          className="subtle"
                          onClick={() => void changeReplacementPolicy((policyDraft ?? currentCache.policy) as ReplacementPolicy)}
                          disabled={busy || (policyDraft ?? currentCache.policy) === currentCache.policy}
                        >
                          {policyBusy ? <RefreshCw size={12} className="spin" /> : '应用'}
                        </button>
                        <small>当前服务进程</small>
                      </div>
                      {policyNotice && (
                        <div className="storage-policy-notice" role="status">
                          {policyNotice}
                        </div>
                      )}
                      <div className="storage-policy-control">
                        <div className="storage-policy-label">
                          <ShieldCheck size={13} />
                          <span>页面类型保护</span>
                        </div>
                        <button
                          type="button"
                          className="subtle"
                          onClick={() => void changePageTypeProtection(!currentCache.protect_page_types)}
                          disabled={busy}
                          aria-pressed={currentCache.protect_page_types}
                          title="在线切换；开启后优先淘汰 HEAP 页，受保护页按缓存一半预算控制"
                        >
                          {protectionBusy ? <RefreshCw size={12} className="spin" /> : currentCache.protect_page_types ? '已开启' : '已关闭'}
                        </button>
                        <small>热加载 · 保护预算 {currentCache.protected_page_limit} 帧 · 下一次淘汰生效</small>
                      </div>
                      {protectionNotice && (
                        <div className="storage-policy-notice" role="status">
                          {protectionNotice}
                        </div>
                      )}
                      <div className="storage-policy-control">
                        <div className="storage-policy-label">
                          <FlaskConical size={13} />
                          <span>扫描污染实验</span>
                        </div>
                        <button
                          type="button"
                          className="subtle"
                          onClick={() => void runBufferPoolDemo(false)}
                          disabled={busy}
                          title="重置当前缓存，运行一次热点索引与顺序扫描冲突实验"
                        >
                          {demoBusy ? <RefreshCw size={12} className="spin" /> : '当前策略'}
                        </button>
                        <button
                          type="button"
                          className="subtle"
                          onClick={() => void runBufferPoolDemo(true)}
                          disabled={busy}
                          title="用相同工作负载对照 LRU、2Q、类型保护和组合策略"
                        >
                          四策略对比
                        </button>
                        <small>自动重置 · 2 次扫描 · 2 个周期 · 每轮一次热点探测</small>
                      </div>
                      {demoResult && (
                        <section className="buffer-demo-panel" aria-label="Buffer Pool 扫描污染实验结果">
                          <div className="buffer-demo-heading">
                            <strong>{demoResult.kind === 'compare' ? '四策略对照结果' : '当前策略实验结果'}</strong>
                            <span>
                              {demoResult.database_page_count.toLocaleString()} 个物理页 · 缓存 {demoResult.buffer_pool.stats.capacity} 帧 ·
                              {demoReference
                                ? ` 核心 ${demoReference.core_index_pages.length} + 次热点 ${demoReference.hot_index_pages.length - demoReference.core_index_pages.length} 个 INDEX`
                                : ' 分层 INDEX'}
                              {' → 顺序 HEAP 扫描 → 逆序核心 + 次热点探测'}
                            </span>
                          </div>
                          <table className="compact-table">
                            <thead>
                              <tr>
                                <th>策略</th>
                                <th>扫描淘汰</th>
                                <th>探测命中</th>
                                <th>探测缺页</th>
                                <th>热点命中率</th>
                                <th>热点保留</th>
                                <th>类型保护跳过</th>
                              </tr>
                            </thead>
                            <tbody>
                              {demoRows.map(result => (
                                <tr key={`${result.mode ?? 'current'}-${result.policy}-${String(result.protect_page_types)}`}>
                                  <td>
                                    {result.mode ?? result.policy.toUpperCase()}
                                    <small>
                                      {' '}
                                      · {result.policy.toUpperCase()}
                                      {result.protect_page_types ? ' + type-aware' : ''}
                                    </small>
                                  </td>
                                  <td>{result.scan.evictions}</td>
                                  <td>{result.probe.hits}</td>
                                  <td>{result.probe.misses}</td>
                                  <td>{(result.probe.hit_rate * 100).toFixed(0)}%</td>
                                  <td>
                                    {result.hot_index_resident_after_scan.length} / {result.hot_index_pages.length}
                                  </td>
                                  <td>{result.total.type_protection_skips ?? 0}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          <p className="buffer-demo-note">
                            结论看“探测缺页”：LRU 被顺序扫描污染后热点索引页被换出；2Q 或页面类型保护应将该值压到 0。
                          </p>
                        </section>
                      )}
                      <table className="compact-table">
                        <thead>
                          <tr>
                            <th>淘汰序</th>
                            <th>页号</th>
                            <th>类型</th>
                            <th>Pin</th>
                            <th>Dirty</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderedCacheFrames.map(frame => {
                            const evictionRank = evictionRankByPageId.get(frame.page_id)
                            return (
                              <tr key={frame.page_id}>
                                <td>{evictionRank ?? (frame.pin_count ? 'Pin' : '—')}</td>
                                <td>{frame.page_id}</td>
                                <td>{frame.type}</td>
                                <td>{frame.pin_count}</td>
                                <td>{frame.dirty ? '是' : '否'}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                      <JsonTree value={cacheSnapshot?.io ?? snapshot.io} name="进程页 I/O 计数" />
                    </>
                  )}
                  {tab === 'indexes' && (
                    <>
                      {snapshot.indexes.length === 0 ? (
                        <div className="side-empty">暂无显式索引</div>
                      ) : (
                        snapshot.indexes.map(index => (
                          <button
                            type="button"
                            className={`index-select ${selectedTable?.indexes.some(item => item.name === index.name) ? 'table-index-linked' : ''}`}
                            key={index.name}
                            ref={element => {
                              if (element) indexButtonRefs.current.set(index.name, element)
                              else indexButtonRefs.current.delete(index.name)
                            }}
                            aria-label={`打开索引 ${index.name}`}
                            onClick={() => {
                              void inspect('indexes', index.name)
                            }}
                          >
                            <span>{index.name}</span>
                            <small>
                              {index.columns.join(', ')}
                              {selectedTable?.indexes.some(item => item.name === index.name) ? ` · ${selectedTable.name}` : ''}
                            </small>
                            <ChevronRight size={13} />
                          </button>
                        ))
                      )}
                    </>
                  )}
                  <details className="storage-limitations">
                    <summary>当前存储实现与限制</summary>
                    {snapshot.limitations.map(note => (
                      <p key={note}>{note}</p>
                    ))}
                    <p>空闲页：{snapshot.free_pages.join(', ') || '当前快照范围内无空闲页'}</p>
                    <p>
                      空闲链：{snapshot.free_list_format ?? '—'} · 头{' '}
                      {typeof snapshot.free_list_head === 'number' ? `#${snapshot.free_list_head}` : '链尾（空）'}
                    </p>
                  </details>
                  <div className="snapshot-time">更新 {new Date(snapshot.snapshot_at).toLocaleTimeString()}</div>
                </>
              )}
            </>
          )}
        </div>
        {detailPanel}
      </div>
      <StorageTooltip tooltip={tooltip} />
    </section>
  )
}
