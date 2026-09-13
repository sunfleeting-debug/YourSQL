import { useEffect, useMemo, useState, type ReactElement } from 'react'
import type { DBError, Stage } from '../types'
import { elapsed } from '../api'
import AstTree from './AstTree'
import JsonTree from './JsonTree'
import StageCanvas from './StageCanvas'

const names: Record<string, string> = {
  tokens: 'Token 流', ast: 'AST', binding: '语义检查', logical_plan: '执行计划',
}

const groupDefinitions = [
  {label: '编译器主流程', keys: ['tokens', 'ast', 'binding', 'logical_plan']},
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function display(value: unknown): string {
  if (value === null) return 'NULL'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function tokenRows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function TokenTable({value}: {value: unknown}) {
  const rows = tokenRows(value)
  if (!rows.length) return <div className="stage-note">无可展示 Token</div>
  return <div className="token-table-wrap"><table className="token-table"><thead><tr><th>#</th><th>种类</th><th>词素</th><th>值</th><th>位置</th></tr></thead><tbody>
    {rows.slice(0, 500).map((token, index) => <tr key={`${String(token.lexeme)}-${index}`}><td>{index + 1}</td><td><code>{display(token.kind)}</code></td><td className="token-lexeme"><code>{display(token.lexeme)}</code></td><td>{token.value === undefined ? <span className="muted">—</span> : <code>{display(token.value)}</code>}</td><td><code>Ln {display(token.line)}, Col {display(token.column)}</code></td></tr>)}
  </tbody></table>{rows.length > 500 && <p className="hint">仅显示前 500 项</p>}</div>
}

function statusLabel(stage: Stage | undefined): string {
  if (!stage) return '未接入'
  return ({success: '已完成', error: '失败', unsupported: '未接入', partial: '部分支持'} as Record<string, string>)[stage.status] ?? stage.status
}

function optionLabel(name: string, stage: Stage | undefined): string {
  if (!stage || stage.status === 'success') return names[name] ?? name
  return `${names[name] ?? name} · ${statusLabel(stage)}`
}

function recordValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {}
}

function semanticSummary(value: unknown): {statement: string; tables: string; output: string; binding: string} {
  const root = recordValue(value)
  const statement = recordValue(root.statement)
  const tables: string[] = []
  const from = recordValue(statement.from_table)
  if (typeof from.name === 'string') tables.push(from.name)
  if (typeof statement.table === 'string') tables.push(statement.table)
  const joins = Array.isArray(statement.joins) ? statement.joins : []
  joins.forEach(join => {
    const table = recordValue(recordValue(join).table)
    if (typeof table.name === 'string') tables.push(table.name)
  })
  const outputColumns = Array.isArray(root.output_columns) ? root.output_columns.map(display) : []
  const insertIndexes = Array.isArray(root.insert_indexes) ? root.insert_indexes : []
  return {
    statement: typeof statement.node === 'string' ? statement.node : '—',
    tables: tables.length ? [...new Set(tables)].join(', ') : '无表来源',
    output: outputColumns.length ? outputColumns.join(', ') : '—',
    binding: insertIndexes.length ? `${insertIndexes.length} 个写入列已定位` : '对象与列引用已完成绑定',
  }
}

function SemanticSummary({value}: {value: unknown}) {
  const summary = semanticSummary(value)
  return <div className="semantic-summary">
    <div className="semantic-result"><span className="semantic-check">✓</span><div><strong>检查通过</strong><small>对象、列和输入信息可以进入计划生成</small></div></div>
    <dl className="semantic-facts">
      <div><dt>语句</dt><dd>{summary.statement}</dd></div>
      <div><dt>对象</dt><dd>{summary.tables}</dd></div>
      <div><dt>结果列</dt><dd>{summary.output}</dd></div>
      <div><dt>绑定</dt><dd>{summary.binding}</dd></div>
    </dl>
    <details className="semantic-raw"><summary>绑定详情</summary><JsonTree value={value} defaultExpandedDepth={0}/></details>
  </div>
}

function StageError({error}: {error: DBError}) {
  const location = error.line == null ? '' : `行 ${error.line}${error.column == null ? '' : ` · 列 ${error.column}`}`
  return <div className="stage-error" role="alert"><div><strong>{error.code}</strong>{location && <span>{location}</span>}</div><p>{error.message}</p>{error.position_note && <small>{error.position_note}</small>}</div>
}

function planOperators(value: unknown): {kind: string; table: string}[] {
  const item = recordValue(value)
  const properties = recordValue(item.properties)
  const current = typeof item.node === 'string' ? [{kind: item.node, table: typeof properties.table === 'string' ? properties.table : ''}] : []
  const children = Array.isArray(item.children) ? item.children.flatMap(planOperators) : []
  return [...current, ...children]
}

function PlanDelta({before, after}: {before: unknown; after: unknown}) {
  const beforeScans = planOperators(before).filter(item => item.kind.endsWith('Scan'))
  const afterScans = planOperators(after).filter(item => item.kind.endsWith('Scan'))
  if (!beforeScans.length && !afterScans.length) return null
  const rows = Array.from(new Set([...beforeScans.map(item => item.table), ...afterScans.map(item => item.table)]))
    .filter(Boolean)
    .map(table => ({table, before: beforeScans.find(item => item.table === table)?.kind ?? '—', after: afterScans.find(item => item.table === table)?.kind ?? '—'}))
  return <div className="plan-delta"><div className="plan-delta-heading"><strong>访问路径</strong><small>优化前 → 优化后</small></div>{rows.map(row => <div className="plan-delta-row" key={row.table}><code>{row.table}</code><span>{row.before}</span><b>→</b><span className={row.before === row.after ? 'unchanged' : 'changed'}>{row.after}</span></div>)}</div>
}

function artifact(value: unknown, name: string): ReactElement {
  if (name === 'tokens') return <TokenTable value={value}/>
  if (name === 'ast') return <AstTree value={value}/>
  if (name === 'binding') return <SemanticSummary value={value}/>
  // WHY：结构树只展开根节点，阶段字段很多时先给出结构，细节由用户主动展开。
  return <JsonTree value={value} defaultExpandedDepth={0}/>
}

function PlanState({stage, label, subtitle, value, tone}: {
  stage: Stage | undefined; label: string; subtitle: string; value: unknown; tone: 'before' | 'after'
}) {
  const hasData = value !== null && value !== undefined && stage?.status !== 'unsupported'
  return <article className={`plan-state plan-state-${tone}`}>
    <header className="plan-state-heading">
      <div className="plan-state-title"><span className="plan-state-index">{tone === 'before' ? '01' : '02'}</span><div><strong>{label}</strong><small>{subtitle}</small></div></div>
      <div className="plan-state-meta"><span className={`stage-detail-status ${stage?.status ?? 'unsupported'}`}>{statusLabel(stage)}</span>{stage?.duration_ms != null && <span>{elapsed(stage.duration_ms)}</span>}</div>
    </header>
    {stage?.reason && <p className="plan-state-reason">{stage.reason}</p>}
    {stage?.error && <StageError error={stage.error}/>}
    {hasData ? <StageCanvas value={value} title={`${label}计划算子`}/> : <div className="plan-state-empty">暂无计划产物</div>}
  </article>
}

function PlanComparison({before, after}: {before: Stage | undefined; after: Stage | undefined}) {
  return <div className="plan-comparison">
    <div className="plan-comparison-intro"><div><strong>优化前后对比</strong><span>纵向对齐算子结构与访问路径</span></div><div className="plan-comparison-legend"><span className="before">优化前</span><b>→</b><span className="after">优化后</span></div></div>
    <PlanState stage={before} label="优化前" subtitle="逻辑计划" value={before?.data} tone="before"/>
    <div className="plan-transition" aria-label="优化器变更摘要"><div className="plan-transition-rule"><span>优化器</span></div>{before?.data !== undefined && after?.data !== undefined && <PlanDelta before={before.data} after={after.data}/>}</div>
    <PlanState stage={after} label="优化后" subtitle="执行计划" value={after?.data} tone="after"/>
  </div>
}

export default function PipelinePanel({stages}: {stages: Stage[]}) {
  const [selected, setSelected] = useState('logical_plan')
  const stageMap = useMemo(() => new Map(stages.map(stage => [stage.name, stage])), [stages])
  const groups = useMemo(() => {
    // WHY：课程要求的主输出只有四段；未返回的阶段不生成“未接入”占位项，避免把内部观测混入主链路。
    return groupDefinitions
      .map(group => ({...group, keys: group.keys.filter(key => stageMap.has(key))}))
      .filter(group => group.keys.length)
  }, [stageMap, stages])

  const availableKeys = groups.flatMap(group => group.keys)
  useEffect(() => {
    if (availableKeys.length && !availableKeys.includes(selected)) setSelected(availableKeys[0])
  }, [availableKeys, selected])

  const current = stageMap.get(selected)
  const optimized = stageMap.get('optimized_plan')

  if (!groups.length) return <div className="inspector-empty"><strong>暂无课程流水线数据</strong></div>

  return <div className="pipeline">
    <div className="pipeline-overview pipeline-stage-picker" aria-label="查询流水线阶段">
      <label className="pipeline-select-wrap"><select value={selected} onChange={event => setSelected(event.target.value)} aria-label="选择流水线阶段">
        {groups.map(group => <optgroup label={group.label} key={group.label}>{group.keys.map(key => <option value={key} key={key}>{optionLabel(key, stageMap.get(key))}</option>)}</optgroup>)}
      </select></label>
    </div>

    <section className="stage-detail">
      <div className="detail-heading stage-detail-heading">
        <h3>{names[selected] ?? selected}</h3>
        {selected !== 'logical_plan' && <div className="stage-meta"><span className={`stage-detail-status ${current?.status ?? 'unsupported'}`}>{statusLabel(current)}</span>{current?.duration_ms != null && <span>{elapsed(current.duration_ms)}</span>}</div>}
      </div>
      {selected === 'logical_plan' ? <PlanComparison before={current} after={optimized}/> : !current ? <div className="stage-unavailable"><strong>未接入</strong></div> : <>
        {current.reason && <p className="stage-reason">{current.reason}</p>}
        {current.error && <StageError error={current.error}/>}
        {current.data !== null && current.data !== undefined && current.status !== 'unsupported'
          ? artifact(current.data, selected)
          : current.status !== 'unsupported' && <div className="stage-empty">暂无产物</div>}
      </>}
    </section>
  </div>
}
