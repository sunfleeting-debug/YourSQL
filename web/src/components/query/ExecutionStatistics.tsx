/** 单条 SQL 的当前执行统计。 */

import { Activity, RefreshCw } from 'lucide-react'
import { elapsed } from '../../api'
import type { MonitorQuery } from '../../types/monitoring'
import type { ExecutionStatisticsResponse } from '../../types/statistics'

const PHASES: Array<readonly [keyof MonitorQuery, string]> = [
  ['queue_wait_ms', '排队'],
  ['compile_ms', '编译'],
  ['execute_ms', '执行'],
  ['materialize_ms', '物化']
]

function time(value: string): string {
  return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
}

function number(value: number): string {
  return value.toLocaleString()
}

function StatValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="query-stat-summary-item">
      <dt>{label}</dt>
      <dd>{value}</dd>
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
        <p>执行 SQL 后可查看本次执行摘要。</p>
      </div>
    )
  }

  const current = data
  const cacheRequests = current.cache_hits + current.cache_misses
  const hitRate = cacheRequests ? `${((current.cache_hits / cacheRequests) * 100).toFixed(1)}%` : '—'
  const phaseTotal = Math.max(current.total_ms, 1)

  return (
    <div className="query-statistics" aria-label="SQL 执行统计">
      <header className="query-statistics-heading">
        <div className="query-statistics-title">
          <Activity size={15} />
          <strong>执行统计</strong>
          <span>{time(current.finished_at)}</span>
        </div>
        <span className={current.status === 'success' ? 'success-text' : 'error-text'}>{current.status === 'success' ? '执行成功' : '执行失败'}</span>
      </header>

      <dl className="query-stat-summary">
        <StatValue label="总耗时" value={elapsed(current.total_ms)} />
        <StatValue label="执行耗时" value={elapsed(current.execute_ms)} />
        <StatValue label="返回行数" value={number(current.rows_returned)} />
        <StatValue label="影响行数" value={number(current.affected_rows)} />
      </dl>

      <div className="query-stat-details">
        <section className="query-stat-section">
          <div className="query-stat-section-heading">
            <strong>阶段耗时</strong>
            <span>端到端 {elapsed(current.total_ms)}</span>
          </div>
          <div className="query-stat-phases">
            {PHASES.map(([key, label]) => {
              const value = typeof current[key] === 'number' ? current[key] : 0
              return (
                <div className="query-stat-phase" key={key}>
                  <span>{label}</span>
                  <div className="query-stat-phase-track">
                    <i style={{ width: `${Math.min(100, (value / phaseTotal) * 100)}%` }} />
                  </div>
                  <code>{elapsed(value)}</code>
                </div>
              )
            })}
          </div>
        </section>

        <section className="query-stat-section">
          <div className="query-stat-section-heading">
            <strong>访问统计</strong>
            <span>本次查询</span>
          </div>
          <dl className="query-stat-resource-list">
            <StatValue label="页读入" value={number(current.page_reads)} />
            <StatValue label="页写出" value={number(current.page_writes)} />
            <StatValue label="缓存命中" value={number(current.cache_hits)} />
            <StatValue label="缓存未命中" value={number(current.cache_misses)} />
            <StatValue label="缓存命中率" value={hitRate} />
            <StatValue label="检查行数" value={number(current.rows_examined)} />
          </dl>
        </section>
      </div>
    </div>
  )
}
