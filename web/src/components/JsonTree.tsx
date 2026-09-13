import { useState } from 'react'

/** 有界树视图，浏览器不根据数据推导任何计划或存储结构。 */
export default function JsonTree({
  value,
  name = 'root',
  depth = 0,
  defaultExpandedDepth = 1
}: {
  value: unknown
  name?: string
  depth?: number
  defaultExpandedDepth?: number
}) {
  const [expanded, setExpanded] = useState(depth <= defaultExpandedDepth)
  if (value === null || typeof value !== 'object')
    return (
      <div className="tree-leaf">
        <span>{name}</span>
        <b>{value === null ? 'null' : String(value)}</b>
      </div>
    )
  const entries = Object.entries(value)
  if (depth > 10) return <pre className="code-block">{JSON.stringify(value, null, 2)}</pre>
  return (
    <div className="json-tree">
      <button className="tree-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        <span>{expanded ? '▾' : '▸'}</span> {name} <small>{Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`}</small>
      </button>
      {expanded && (
        <div className="tree-children">
          {entries.slice(0, 100).map(([key, child]) => (
            <JsonTree key={key} value={child} name={key} depth={depth + 1} defaultExpandedDepth={defaultExpandedDepth} />
          ))}
          {entries.length > 100 && <p className="hint">树视图显示前 100 项；完整内容见 JSON。</p>}
        </div>
      )}
    </div>
  )
}
