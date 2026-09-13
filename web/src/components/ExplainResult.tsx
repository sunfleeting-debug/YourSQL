import StageCanvas from './StageCanvas'
import type { PlanEstimate, QueryResult } from '../types'

interface PlanRecord {
  node?: unknown
  kind?: unknown
  properties?: unknown
  children?: unknown
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function recordValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {}
}

function unwrapExplainPlan(value: unknown): unknown {
  const plan = recordValue(value)
  const node = typeof plan.node === 'string' ? plan.node : typeof plan.kind === 'string' ? plan.kind : ''
  if (node === 'Explain' && Array.isArray(plan.children) && plan.children.length > 0) return plan.children[0]
  return value
}

function optimizedPlan(result: QueryResult): unknown {
  const stage = result.stages.find(item => item.name === 'optimized_plan' && item.data !== null && item.data !== undefined)
  return unwrapExplainPlan(stage?.data ?? result.plan)
}

function planNodes(value: unknown): PlanRecord[] {
  const plan = recordValue(value) as PlanRecord
  const node = typeof plan.node === 'string' || typeof plan.kind === 'string' ? [plan] : []
  const children = Array.isArray(plan.children) ? plan.children.flatMap(planNodes) : []
  return [...node, ...children]
}

function planKind(node: PlanRecord): string {
  return typeof node.node === 'string' ? node.node : typeof node.kind === 'string' ? node.kind : 'Plan'
}

function planSummary(result: QueryResult, plan: unknown): { count: string; access: string; raw: string; estimate: PlanEstimate | null } {
  const nodes = planNodes(plan)
  const scans = nodes.filter(node => planKind(node).endsWith('Scan'))
  const scan = scans[0]
  const properties = recordValue(scan?.properties)
  const table = typeof properties.table === 'string' ? properties.table : ''
  const indexColumn = typeof properties.index_column === 'string' ? properties.index_column : ''
  const access = scan
    ? `${planKind(scan) === 'IndexScan' ? '索引访问' : '顺序访问'}${table ? ` · ${table}` : ''}${indexColumn ? ` · ${indexColumn}` : ''}`
    : '未识别扫描算子'
  const raw = typeof result.rows[0]?.[0] === 'string' ? result.rows[0][0] : '暂无原始计划文本'
  return { count: `${nodes.length} 个算子`, access, raw, estimate: result.plan_estimate ?? null }
}

function formatCost(value: number): string {
  return Number.isFinite(value) ? value.toFixed(value >= 10 ? 1 : 2) : '—'
}

function formatMillis(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return '—'
  return value < 10 ? `${value.toFixed(2)} ms` : `${value.toFixed(1)} ms`
}

export default function ExplainResult({ result }: { result: QueryResult }) {
  const plan = optimizedPlan(result)
  const summary = planSummary(result, plan)
  const hasPlan = planNodes(plan).length > 0

  return (
    <div className="explain-result" aria-label="EXPLAIN 执行计划">
      <header className="explain-result-heading">
        <div>
          <span className="explain-result-label">EXPLAIN</span>
          <h3>执行计划</h3>
          <p>
            {summary.count} · {summary.access}
          </p>
        </div>
        <span className="explain-result-status">优化后</span>
      </header>
      <div className="explain-result-metrics" aria-label="执行计划估算与实际耗时">
        <div title="基于当前表统计信息的优化器成本单位，不是毫秒预测">
          <span>预估成本</span>
          <strong>{summary.estimate ? formatCost(summary.estimate.total_cost) : '—'}</strong>
          <small>优化器模型</small>
        </div>
        <div>
          <span>预估行数</span>
          <strong>{summary.estimate ? summary.estimate.rows.toLocaleString() : '—'}</strong>
          <small>统计信息</small>
        </div>
        <div>
          <span>实际耗时</span>
          <strong>{formatMillis(result.execution_ms ?? result.elapsed_ms)}</strong>
          <small>本次执行</small>
        </div>
      </div>
      {hasPlan ? (
        <StageCanvas value={plan} title="优化后的执行计划" variant="result" />
      ) : (
        <div className="explain-plan-missing">结构化计划暂不可用，请展开下方原始计划文本查看。</div>
      )}
      <details className="explain-raw-details">
        <summary>查看原始计划文本</summary>
        <pre>{summary.raw}</pre>
      </details>
    </div>
  )
}
