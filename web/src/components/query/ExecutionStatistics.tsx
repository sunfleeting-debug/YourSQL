/** 单条 SQL 的本次执行指标和同指纹历史趋势。 */

import { useMemo, useState } from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import { elapsed } from '../../api'
import type { MonitorQuery } from '../../types/monitoring'
import type { ExecutionStatisticsResponse } from '../../types/statistics'

type TrendMetric = 'total_ms' | 'page_reads' | 'page_writes' | 'cache_hits' | 'cache_misses' | 'cache_evictions'

const TREND_OPTIONS: Array<readonly [TrendMetric, string]> = [
  ['total_ms', '总耗时'],
  ['page_reads', '页读入'],
  ['page_writes', '页写出'],
  ['cache_hits', '缓存命中'],
  ['cache_misses', '缓存未命中'],
  ['cache_evictions', '缓存淘汰']
]

const PHASES: Array<readonly [keyof MonitorQuery, string]> = [
  ['queue_wait_ms', '排队'],
  ['compile_ms', '编译'],
  ['execute_ms', '执行'],
  ['materialize_ms', '物化']
]

function time(value: string): string {
  return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
}

function queryTitle(sql: string): string {
  return sql.replace(/\s+/g, ' ').trim() || '未命名语句'
}

function metricValue(record: MonitorQuery, metric: TrendMetric): number {
  return record[metric]
}

function metricText(metric: TrendMetric, value: number): string {
  return metric === 'total_ms' ? elapsed(value) : value.toLocaleString()
}

function StatCard({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="query-stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  )
}

function TrendChart({ items, metric, currentId }: { items: MonitorQuery[]; metric: TrendMetric; currentId: string }) {
  const values = items.map(item => metricValue(item, metric))
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 1)
  const range = max > min ? max - min : 1
  const points = useMemo(
    () =>
      values.map((value, index) => {
        const x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100
        const y = 88 - ((value - min) / range) * 76
        return { x, y: Math.max(10, y), value, item: items[index] }
      }),
    [items, min, range, values]
  )
  const label = TREND_OPTIONS.find(([key]) => key === metric)?.[1] ?? metric

  if (!items.length) {
    return <div className="query-stat-chart-empty">暂无同一 SQL 的历史执行记录</div>
  }

  return (
    <div className="query-stat-chart-wrap">
      <svg className="query-stat-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={`${label}历史趋势`}>
        <line x1="0" x2="100" y1="88" y2="88" className="query-stat-chart-axis" />
        {points.length > 1 && <polyline points={points.map(point => `${point.x},${point.y}`).join(' ')} className="query-stat-chart-line" />}
        {points.map(point => (
          <circle
            key={point.item.query_id}
            cx={point.x}
            cy={point.y}
            r={point.item.query_id === currentId ? 2.5 : 1.8}
            className={point.item.query_id === currentId ? 'current' : ''}
          >
            <title>
              {time(point.item.finished_at)} · {metricText(metric, point.value)}
            </title>
          </circle>
        ))}
      </svg>
      <div className="query-stat-chart-scale">
        <span>{time(items[0].finished_at)}</span>
        <span>最高 {metricText(metric, max)}</span>
        <span>{time(items[items.length - 1].finished_at)}</span>
      </div>
    </div>
  )
}

export default function ExecutionStatistics({
  data,
  loading,
  error,
  onRetry
}: {
  data: ExecutionStatisticsResponse | null
  loading: boolean
  error: string
  onRetry: () => void
}) {
  const [metric, setMetric] = useState<TrendMetric>('total_ms')

  if (loading) {
    return (
      <div className="empty-state">
        <RefreshCw className="spin" />
        <span>正在读取执行统计…</span>
      </div>
    )
  }
  if (error) {
    return (
      <div className="empty-state error-text">
        <Activity />
        <strong>执行统计暂不可用</strong>
        <p>{error}</p>
        <button onClick={onRetry}>
          <RefreshCw size={14} />
          重试
        </button>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="empty-state">
        <Activity />
        <strong>暂无执行统计</strong>
        <p>执行 SQL 后可查看本次执行和历史趋势。</p>
      </div>
    )
  }

  const current = data.current
  const cacheRequests = current.cache_hits + current.cache_misses
  const hitRate = cacheRequests ? `${((current.cache_hits / cacheRequests) * 100).toFixed(1)}%` : '—'
  const phaseTotal = Math.max(current.total_ms, 1)

  return (
    <div className="query-statistics" aria-label="SQL 执行统计">
      <header className="query-statistics-heading">
        <div>
          <div className="query-statistics-title">
            <Activity size={15} />
            <strong>本次执行</strong>
            <code>{queryTitle(current.sql)}</code>
          </div>
          <small>
            {time(current.finished_at)} · 同一 SQL 历史执行 {data.total} 次
          </small>
        </div>
        {current.diagnostic_available === false && <span className="query-statistics-note">本次仅保留轻量指标</span>}
      </header>

      <div className="query-stat-cards">
        <StatCard label="总耗时" value={elapsed(current.total_ms)} note={current.slow ? '超过慢查询阈值' : undefined} />
        <StatCard label="执行耗时" value={elapsed(current.execute_ms)} note={`排队 ${elapsed(current.queue_wait_ms)}`} />
        <StatCard label="页读入" value={current.page_reads.toLocaleString()} note={`页写出 ${current.page_writes.toLocaleString()}`} />
        <StatCard
          label="缓存命中率"
          value={hitRate}
          note={`命中 ${current.cache_hits.toLocaleString()} · 未命中 ${current.cache_misses.toLocaleString()}`}
        />
      </div>

      <div className="query-stat-sections">
        <section className="query-stat-section">
          <div className="query-stat-section-heading">
            <strong>本次阶段耗时</strong>
            <span>端到端 {elapsed(current.total_ms)}</span>
          </div>
          <div className="query-stat-phases">
            {PHASES.map(([key, label]) => {
              const value = current[key]
              const duration = typeof value === 'number' ? value : 0
              return (
                <div className="query-stat-phase" key={key}>
                  <span>{label}</span>
                  <div className="query-stat-phase-track">
                    <i style={{ width: `${Math.min(100, (duration / phaseTotal) * 100)}%` }} />
                  </div>
                  <code>{elapsed(duration)}</code>
                </div>
              )
            })}
          </div>
        </section>

        <section className="query-stat-section">
          <div className="query-stat-section-heading">
            <strong>访问统计</strong>
            <span>本次查询增量</span>
          </div>
          <dl className="query-stat-resource-list">
            <div>
              <dt>缓存命中</dt>
              <dd>{current.cache_hits.toLocaleString()}</dd>
            </div>
            <div>
              <dt>缓存未命中</dt>
              <dd>{current.cache_misses.toLocaleString()}</dd>
            </div>
            <div>
              <dt>缓存淘汰</dt>
              <dd>{current.cache_evictions.toLocaleString()}</dd>
            </div>
            <div>
              <dt>检查 / 返回</dt>
              <dd>
                {current.rows_examined.toLocaleString()} / {current.rows_returned.toLocaleString()}
              </dd>
            </div>
          </dl>
        </section>
      </div>

      <section className="query-stat-section query-stat-trend-section">
        <div className="query-stat-section-heading">
          <div>
            <strong>历史执行趋势</strong>
            <span>按同一 SQL 指纹聚合，当前执行已高亮</span>
          </div>
          <label className="query-stat-select">
            指标
            <select value={metric} onChange={event => setMetric(event.target.value as TrendMetric)}>
              {TREND_OPTIONS.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <TrendChart items={data.items} metric={metric} currentId={current.query_id} />
      </section>
    </div>
  )
}
