import { useEffect, useId, useMemo, useState } from 'react'
import { Minus, Plus, RotateCcw } from 'lucide-react'

interface PlanNode {
  id: string
  kind: string
  properties: Record<string, unknown>
  children: PlanNode[]
}

interface LayoutNode extends PlanNode {
  x: number
  y: number
  width: number
  subtreeWidth: number
}

interface PlanGraph {
  root: PlanNode | null
  nodes: LayoutNode[]
  width: number
  height: number
}

const NODE_MIN_WIDTH = 96
const NODE_MAX_WIDTH = 148
const NODE_HEIGHT = 34
const COLUMN_GAP = 20
const ROW_GAP = 16
const PADDING = 16

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function textValue(value: unknown): string {
  if (value === undefined) return '—'
  if (value === null) return 'NULL'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value, null, 2) ?? '—'
  } catch {
    return String(value)
  }
}

function clip(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value
}

function compactParameter(value: unknown): string | null {
  if (value === null || value === undefined) return '""'
  if (typeof value === 'string' && value.length <= 12) return JSON.stringify(value)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

function formatLiteral(value: unknown): string {
  if (value === null) return 'NULL'
  if (typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE'
  if (typeof value === 'number') return String(value)
  return textValue(value)
}

function formatExpression(value: unknown, depth = 0): string | null {
  if (depth > 4 || value === null || value === undefined) return null
  if (!isRecord(value)) return formatLiteral(value)
  const node = typeof value.node === 'string' ? value.node : ''
  if (node === 'ColumnRef') return `${typeof value.table === 'string' ? `${value.table}.` : ''}${String(value.name ?? '')}`
  if (node === 'Literal') return formatLiteral(value.value)
  if (node === 'Star') return typeof value.table === 'string' ? `${value.table}.*` : '*'
  if (node === 'Parameter') return String(value.name ?? '?')
  if (node === 'TableRef') return `${String(value.name ?? '')}${value.alias ? ` AS ${String(value.alias)}` : ''}`
  if (node === 'SelectItem') {
    const expression = formatExpression(value.expression, depth + 1)
    return expression ? `${expression}${value.alias ? ` AS ${String(value.alias)}` : ''}` : null
  }
  if (node === 'OrderItem') {
    const expression = formatExpression(value.expression, depth + 1)
    return expression ? `${expression} ${value.descending ? 'DESC' : 'ASC'}` : null
  }
  if (node === 'BinaryOp') {
    const left = formatExpression(value.left, depth + 1)
    const right = formatExpression(value.right, depth + 1)
    return left && right ? `${left} ${String(value.operator ?? '')} ${right}` : null
  }
  if (node === 'UnaryOp') {
    const operand = formatExpression(value.operand, depth + 1)
    return operand ? `${String(value.operator ?? '')} ${operand}` : null
  }
  if (node === 'IsNull') {
    const expression = formatExpression(value.expression, depth + 1)
    return expression ? `${expression} IS ${value.negated ? 'NOT ' : ''}NULL` : null
  }
  if (node === 'InPredicate') {
    const expression = formatExpression(value.expression, depth + 1)
    const values = Array.isArray(value.values) ? value.values.map(item => formatExpression(item, depth + 1)).filter(Boolean) : []
    return expression && values.length ? `${expression} ${value.negated ? 'NOT ' : ''}IN (${values.join(', ')})` : null
  }
  if (node === 'BetweenPredicate') {
    const expression = formatExpression(value.expression, depth + 1)
    const lower = formatExpression(value.lower, depth + 1)
    const upper = formatExpression(value.upper, depth + 1)
    return expression && lower && upper ? `${expression} ${value.negated ? 'NOT ' : ''}BETWEEN ${lower} AND ${upper}` : null
  }
  if (node === 'FunctionCall') {
    const args = Array.isArray(value.args) ? value.args.map(item => formatExpression(item, depth + 1)).filter(Boolean) : []
    return `${String(value.name ?? '函数')}(${args.join(', ')})`
  }
  return null
}

function formatList(value: unknown, formatter = formatExpression): string | null {
  if (!Array.isArray(value) || !value.length) return null
  const items = value.map(item => formatter(item)).filter((item): item is string => Boolean(item))
  return items.length ? items.join(', ') : null
}

function operatorSummary(node: PlanNode): string | null {
  const properties = node.properties
  if (node.kind === 'Filter') return formatExpression(properties.predicate)
  if (node.kind === 'Project') return formatList(properties.items) ?? (properties.distinct ? 'DISTINCT' : null)
  if (node.kind === 'SeqScan') return typeof properties.table === 'string' ? properties.table : null
  if (node.kind === 'IndexScan') {
    const table = typeof properties.table === 'string' ? properties.table : ''
    const column = typeof properties.index_column === 'string' ? properties.index_column : '索引'
    return table ? `${table} · ${column}` : column
  }
  if (node.kind === 'Limit') {
    const limit = properties.limit == null ? null : String(properties.limit)
    const offset = typeof properties.offset === 'number' && properties.offset > 0 ? ` +${properties.offset}` : ''
    return limit ? `${limit}${offset}` : offset ? `offset ${offset.slice(2)}` : null
  }
  if (node.kind === 'Sort') return formatList(properties.order_by)
  if (node.kind === 'Aggregate') {
    const group = formatList(properties.group_by)
    const having = formatExpression(properties.having)
    return [group ? `GROUP BY ${group}` : null, having ? `HAVING ${having}` : null].filter(Boolean).join(' · ') || null
  }
  if (node.kind === 'Join') {
    const type = typeof properties.join_type === 'string' ? properties.join_type : 'JOIN'
    const condition = formatExpression(properties.on)
    return condition ? `${type} · ${condition}` : type
  }
  const entries = Object.entries(properties)
    .map(([key, value]) => {
      const formatted = formatExpression(value) ?? compactParameter(value)
      return formatted ? `${key}=${formatted}` : null
    })
    .filter((item): item is string => Boolean(item))
  return entries.slice(0, 2).join(', ') || null
}

function operatorLabel(node: PlanNode): string {
  const summary = operatorSummary(node)
  return summary ? `${node.kind} · ${summary}` : node.kind
}

function nodeWidth(node: PlanNode): number {
  const label = operatorLabel(node)
  return Math.min(NODE_MAX_WIDTH, Math.max(NODE_MIN_WIDTH, 20 + label.length * 5.6))
}

function readPlan(value: unknown): PlanNode | null {
  let serial = 0
  const visit = (item: unknown): PlanNode | null => {
    if (!isRecord(item)) return null
    const kind =
      typeof item.node === 'string' ? item.node : typeof item.kind === 'string' ? item.kind : typeof item.type === 'string' ? item.type : 'Plan'
    const properties = isRecord(item.properties) ? item.properties : {}
    const children = Array.isArray(item.children) ? item.children.map(visit).filter((child): child is PlanNode => child !== null) : []
    return { id: `operator-${serial++}`, kind, properties, children }
  }
  return visit(value)
}

function subtreeWidth(node: PlanNode): number {
  const ownWidth = nodeWidth(node)
  if (!node.children.length) return ownWidth
  const childrenWidth = node.children.reduce((total, child, index) => total + subtreeWidth(child) + (index ? COLUMN_GAP : 0), 0)
  return Math.max(ownWidth, childrenWidth)
}

function maxDepth(node: PlanNode, depth = 0): number {
  return node.children.reduce((max, child) => Math.max(max, maxDepth(child, depth + 1)), depth)
}

function layoutPlan(root: PlanNode): PlanGraph {
  const depth = maxDepth(root)
  const nodes: LayoutNode[] = []
  const width = subtreeWidth(root)
  const place = (node: PlanNode, level: number, left: number): LayoutNode => {
    const subtree = subtreeWidth(node)
    const ownWidth = nodeWidth(node)
    const placed = {
      ...node,
      x: left + (subtree - ownWidth) / 2,
      y: PADDING + level * (NODE_HEIGHT + ROW_GAP),
      width: ownWidth,
      subtreeWidth: subtree
    }
    nodes.push(placed)
    const childrenWidth = node.children.reduce((total, child, index) => total + subtreeWidth(child) + (index ? COLUMN_GAP : 0), 0)
    let cursor = left + (subtree - childrenWidth) / 2
    node.children.forEach(child => {
      const childLayout = place(child, level + 1, cursor)
      cursor += childLayout.subtreeWidth + COLUMN_GAP
    })
    return placed
  }
  place(root, 0, PADDING)
  const height = Math.max(150, PADDING * 2 + (depth + 1) * NODE_HEIGHT + depth * ROW_GAP)
  return { root, nodes, width: width + PADDING * 2, height }
}

/** 绘制算子输入关系，并在节点内保留一行可读参数；完整 properties 仍放在详情区。 */
export default function StageCanvas({ value, title, variant = 'pipeline' }: { value: unknown; title: string; variant?: 'pipeline' | 'result' }) {
  const [zoom, setZoom] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const markerId = `plan-arrow-${useId().replace(/:/g, '')}`
  const graph = useMemo(() => {
    const root = readPlan(value)
    return root ? layoutPlan(root) : { root: null, nodes: [], width: 0, height: 0 }
  }, [value])
  useEffect(() => {
    setSelectedId(null)
    setZoom(1)
  }, [graph])
  const selected = graph.nodes.find(node => node.id === selectedId)
  const positions = useMemo(() => new Map(graph.nodes.map(node => [node.id, node])), [graph.nodes])

  if (!graph.root) return <div className="plan-empty">无法识别计划</div>

  return (
    <div className={`canvas-board plan-canvas ${variant === 'result' ? 'explain-plan-canvas' : ''}`} aria-label={`${title}画板`}>
      <div className="canvas-toolbar">
        <div className="canvas-tools">
          <button
            className="icon-button"
            aria-label="缩小画板"
            onClick={() => setZoom(current => Math.max(0.55, Number((current - 0.1).toFixed(2))))}
          >
            <Minus size={14} />
          </button>
          <span className="canvas-zoom">{Math.round(zoom * 100)}%</span>
          <button className="icon-button" aria-label="放大画板" onClick={() => setZoom(current => Math.min(1.5, Number((current + 0.1).toFixed(2))))}>
            <Plus size={14} />
          </button>
          <button className="icon-button" aria-label="重置画板缩放" onClick={() => setZoom(1)}>
            <RotateCcw size={13} />
          </button>
        </div>
      </div>
      <div className="plan-content">
        <div className="canvas-scroll plan-scroll">
          <svg
            className="stage-svg plan-svg"
            width={graph.width * zoom}
            height={graph.height * zoom}
            viewBox={`0 0 ${graph.width} ${graph.height}`}
            role="img"
            aria-label={title}
          >
            <defs>
              <marker id={markerId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor" />
              </marker>
            </defs>
            {graph.nodes.flatMap(parent =>
              parent.children.map(child => {
                const from = positions.get(parent.id)
                const to = positions.get(child.id)
                if (!from || !to) return null
                const x1 = from.x + from.width / 2
                const y1 = from.y + NODE_HEIGHT
                const x2 = to.x + to.width / 2
                const y2 = to.y
                return (
                  <path
                    key={`${child.id}-${parent.id}`}
                    className="stage-edge plan-edge"
                    markerEnd={`url(#${markerId})`}
                    d={`M ${x1} ${y1} C ${x1} ${y1 + 10}, ${x2} ${y2 - 10}, ${x2} ${y2}`}
                  />
                )
              })
            )}
            {graph.nodes.map(node => {
              const active = node.id === selected?.id
              const label = operatorLabel(node)
              const summary = operatorSummary(node)
              {
                /* WHY：旧版 .stage-node rect 会覆盖计划节点色条；计划图使用独立 class，避免 SVG 样式串扰。 */
              }
              return (
                <g
                  key={node.id}
                  className={`plan-node plan-kind-${node.kind.toLowerCase().replace(/[^a-z0-9]+/g, '-')}${active ? ' active' : ''}`}
                  transform={`translate(${node.x},${node.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${label} 算子`}
                  aria-pressed={active}
                  onClick={() => setSelectedId(node.id)}
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      setSelectedId(node.id)
                    }
                  }}
                >
                  <title>{label}</title>
                  <rect className="plan-node-surface" width={node.width} height={NODE_HEIGHT} rx="6" />
                  <rect className="plan-node-accent" width="4" height={NODE_HEIGHT} rx="2" />
                  {summary ? (
                    <>
                      <text className="plan-node-kind" x="10" y="13">
                        {node.kind}
                      </text>
                      <text className="plan-node-detail" x="10" y="26">
                        {clip(summary, 23)}
                      </text>
                    </>
                  ) : (
                    <text className="plan-node-label" x="10" y="22">
                      {node.kind}
                    </text>
                  )}
                </g>
              )
            })}
          </svg>
        </div>
        {selected && (
          <aside className="plan-inspector" aria-label="算子详情">
            <>
              <div className="plan-inspector-heading" title={selected.kind}>
                <strong>{selected.kind}</strong>
              </div>
              <dl className="plan-properties">
                {Object.entries(selected.properties).length ? (
                  Object.entries(selected.properties).map(([key, value]) => {
                    const displayValue = textValue(value)
                    return (
                      <div key={key}>
                        <dt>{key}</dt>
                        <dd title={displayValue}>{displayValue}</dd>
                      </div>
                    )
                  })
                ) : (
                  <div>
                    <dt>属性</dt>
                    <dd>—</dd>
                  </div>
                )}
              </dl>
            </>
          </aside>
        )}
      </div>
    </div>
  )
}
