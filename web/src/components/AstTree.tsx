import { useState } from 'react'

interface AstItem {
  id: string
  label: string
  value?: string
  children: AstItem[]
}

const fieldNames: Record<string, string> = {
  items: '投影列', from_table: '来源表', joins: '连接', where: '过滤条件', group_by: '分组',
  having: '分组过滤', order_by: '排序', limit: '行数上限', offset: '偏移量', distinct: '去重',
  union: '联合查询', expression: '表达式', left: '左值', right: '右值', operator: '运算符',
  values: '写入值', columns: '写入列', table: '目标表', alias: '别名', args: '参数',
  on: '连接条件', descending: '降序', nulls_first: '空值优先', lower: '下界', upper: '上界',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function textValue(value: unknown): string {
  if (value === null) return 'NULL'
  if (value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return String(value)
  return JSON.stringify(value) ?? '—'
}

function nodeTitle(item: Record<string, unknown>): {label: string; value?: string} {
  const node = typeof item.node === 'string' ? item.node : ''
  if (node === 'Select') return {label: '查询 SELECT'}
  if (node === 'Insert') return {label: '插入 INSERT', value: typeof item.table === 'string' ? item.table : undefined}
  if (node === 'Delete') return {label: '删除 DELETE', value: typeof item.table === 'string' ? item.table : undefined}
  if (node === 'Update') return {label: '更新 UPDATE', value: typeof item.table === 'string' ? item.table : undefined}
  if (node === 'CreateTable') return {label: '建表 CREATE TABLE', value: typeof item.name === 'string' ? item.name : undefined}
  if (node === 'ColumnRef') return {label: '列', value: `${item.table ? `${String(item.table)}.` : ''}${String(item.name ?? '')}`}
  if (node === 'TableRef') return {label: '表', value: `${String(item.name ?? '')}${item.alias ? ` · ${String(item.alias)}` : ''}`}
  if (node === 'Literal') return {label: '常量', value: textValue(item.value)}
  if (node === 'Parameter') return {label: '参数', value: textValue(item.name)}
  if (node === 'Star') return {label: '全部列', value: typeof item.table === 'string' ? item.table : undefined}
  if (node === 'BinaryOp') return {label: '条件表达式', value: typeof item.operator === 'string' ? item.operator : undefined}
  if (node === 'UnaryOp') return {label: '一元表达式', value: typeof item.operator === 'string' ? item.operator : undefined}
  if (node === 'FunctionCall') return {label: '函数', value: typeof item.name === 'string' ? item.name : undefined}
  if (node === 'JoinClause') return {label: '连接 JOIN', value: typeof item.join_type === 'string' ? item.join_type : undefined}
  if (node === 'OrderItem') return {label: '排序项', value: item.descending ? 'DESC' : 'ASC'}
  if (node === 'SelectItem') return {label: '投影项', value: typeof item.alias === 'string' ? `AS ${item.alias}` : undefined}
  return {label: node || '节点'}
}

function childLabel(key: string): string {
  return fieldNames[key] ?? key
}

function buildItem(value: unknown, key = 'root'): AstItem | null {
  const id = `ast-${key}`
  if (Array.isArray(value)) {
    if (!value.length) return null
    return {id, label: childLabel(key), value: `${value.length} 项`, children: value.map((item, index) => buildItem(item, `${key}[${index + 1}]`)).filter((item): item is AstItem => item !== null)}
  }
  if (value === null || value === undefined) return null
  if (!isRecord(value)) return {id, label: childLabel(key), value: textValue(value), children: []}

  const title = nodeTitle(value)
  const node = typeof value.node === 'string' ? value.node : ''
  const hiddenFields = new Set(['node', ...(node === 'TableRef' ? ['name', 'alias']
    : node === 'ColumnRef' ? ['name', 'table']
      : node === 'Literal' ? ['value']
        : node === 'Star' ? ['table']
          : node === 'SelectItem' ? ['alias']
            : node === 'FunctionCall' ? ['name']
              : node === 'BinaryOp' || node === 'UnaryOp' ? ['operator']
                : node === 'JoinClause' ? ['join_type']
                  : [])])
  const children = Object.entries(value)
    .filter(([name, child]) => {
      if (hiddenFields.has(name) || child === null || child === undefined) return false
      if (Array.isArray(child) && child.length === 0) return false
      if (child === false || (name === 'offset' && child === 0)) return false
      return true
    })
    .map(([name, child]) => buildItem(child, name))
    .filter((item): item is AstItem => item !== null)
  return {id, label: title.label, value: title.value, children}
}

function Branch({item, depth}: {item: AstItem; depth: number}) {
  const [expanded, setExpanded] = useState(depth < 2)
  const hasChildren = item.children.length > 0
  return <div className={`ast-tree-branch depth-${Math.min(depth, 3)}`}>
    <button className={`ast-tree-node ${hasChildren ? '' : 'leaf'}`} aria-expanded={hasChildren ? expanded : undefined} onClick={() => hasChildren && setExpanded(value => !value)}>
      {hasChildren && <span className="ast-tree-chevron">{expanded ? '▾' : '▸'}</span>}
      <strong>{item.label}</strong>{item.value !== undefined && <code>{item.value}</code>}
    </button>
    {hasChildren && expanded && <div className="ast-tree-children">{item.children.map(child => <Branch key={child.id} item={child} depth={depth + 1}/>)}</div>}
  </div>
}

/** 面向 SQL 结构的 AST 树；字段改成语义标签，避免把原始 JSON 当作语法树展示。 */
export default function AstTree({value}: {value: unknown}) {
  const root = buildItem(value)
  return root ? <div className="ast-tree" aria-label="AST 结构树"><Branch item={root} depth={0}/></div> : <div className="stage-empty">无法识别 AST</div>
}
