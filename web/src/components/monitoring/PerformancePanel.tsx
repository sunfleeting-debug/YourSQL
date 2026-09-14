/** 性能监控工作区：延迟摘要、慢查询和缓存淘汰诊断。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, BarChart3, Database, Gauge, RefreshCw, Server, Timer } from 'lucide-react'
import { api, elapsed, errorMessage } from '../../api'
import JsonTree from '../query/JsonTree'
import type { MonitorDetail, MonitorQuery, MonitorQueriesResponse, MonitorSummary } from '../../types/monitoring'

function number(value: number, digits = 1): string {
  return value.toFixed(digits)
}

function time(value: string): string {
  return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
}

function queryTitle(query: MonitorQuery): string {
  return query.sql.replace(/\s+/g, ' ').trim() || '未命名语句'
}

function TimingBar({ label, value, total, tone }: { label: string; value: number; total: number; tone: string }) {
  const width = total > 0 ? Math.min(100, (value / total) * 100) : 0
  return (
    <div className="monitor-timing-row">
      <span>{label}</span>
      <div className="monitor-timing-track">
        <i style={{ width: `${width}%`, background: tone }} />
      </div>
      <code>{elapsed(value)}</code>
    </div>
  )
}

function LatencyChart({ samples, threshold }: { samples: MonitorSummary['latency_series']; threshold: number }) {
  const points = useMemo(() => {
    if (!samples.length) return ''
    const max = Math.max(threshold, ...samples.map(sample => sample.total_ms), 1)
    return samples
      .map((sample, index) => {
        const x = samples.length === 1 ? 50 : (index / (samples.length - 1)) * 100
        const y = 92 - (sample.total_ms / max) * 78
        return `${x},${Math.max(10, y)}`
      })
      .join(' ')
  }, [samples, threshold])
  const thresholdY = samples.length ? 92 - (threshold / Math.max(threshold, ...samples.map(sample => sample.total_ms), 1)) * 78 : 14
  return (
    <div className="monitor-chart-wrap">
      {samples.length ? (
        <svg className="monitor-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="最近查询延迟趋势">
          <line x1="0" x2="100" y1={thresholdY} y2={thresholdY} className="monitor-chart-threshold" />
          <polyline points={points} className="monitor-chart-line" />
          {samples.map((sample, index) => {
            const [x, y] = points.split(' ')[index].split(',')
            return <circle key={`${sample.at}-${index}`} cx={x} cy={y} r="1.7" className={sample.slow ? 'slow' : ''} />
          })}
        </svg>
      ) : (
        <div className="monitor-chart-empty">
          <Activity size={20} />
          <span>执行查询后显示趋势</span>
        </div>
      )}
      <div className="monitor-chart-axis">
        <span>{samples.length ? time(samples[0].at) : '—'}</span>
        <span>阈值 {elapsed(threshold)}</span>
        <span>{samples.length ? time(samples[samples.length - 1].at) : '—'}</span>
      </div>
    </div>
  )
}

export default function PerformancePanel({ active = true }: { active?: boolean }) {
  const [summary, setSummary] = useState<MonitorSummary | null>(null)
  const [queries, setQueries] = useState<MonitorQuery[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<MonitorDetail | null>(null)
  const [busy, setBusy] = useState(true)
  const [detailBusy, setDetailBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const [nextSummary, nextQueries] = await Promise.all([
        api<MonitorSummary>('/api/monitor/summary'),
        api<MonitorQueriesResponse>('/api/monitor/queries?slow_only=1&limit=100')
      ])
      setSummary(nextSummary)
      setQueries(nextQueries.items)
      setSelectedId(current =>
        current && nextQueries.items.some(item => item.query_id === current) ? current : (nextQueries.items[0]?.query_id ?? null)
      )
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    if (active) void refresh()
  }, [active, refresh])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailBusy(true)
    api<MonitorDetail>(`/api/monitor/queries/${encodeURIComponent(selectedId)}`)
      .then(value => {
        if (!cancelled) setDetail(value)
      })
      .catch(requestError => {
        if (!cancelled) setError(errorMessage(requestError))
      })
      .finally(() => {
        if (!cancelled) setDetailBusy(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  if (error && !summary) {
    return (
      <section className="monitor-panel" aria-label="性能监控">
        <div className="inline-error monitor-error">{error}</div>
        <button className="monitor-retry" onClick={() => void refresh()}>
          <RefreshCw size={14} />
          重新加载
        </button>
      </section>
    )
  }

  const currentSummary = summary ?? {
    threshold_ms: 500,
    sampled_queries: 0,
    successful_queries: 0,
    failed_queries: 0,
    slow_queries: 0,
    avg_ms: 0,
    p50_ms: 0,
    p95_ms: 0,
    max_ms: 0,
    cache_hit_rate: 0,
    page_reads: 0,
    page_writes: 0,
    cache_hits: 0,
    cache_misses: 0,
    cache_evictions: 0,
    latency_series: [],
    storage_events: [],
    storage_policy: 'lru',
    retention: '',
    log_failures: 0
  }
  const selected = detail ?? (selectedId ? queries.find(item => item.query_id === selectedId) : null)

  return (
    <section className="monitor-panel" aria-label="性能监控">
      <header className="monitor-heading">
        <div>
          <div className="monitor-title-line">
            <Gauge size={17} />
            <h2>性能监控</h2>
            <span className="monitor-live-dot" aria-label="进程内实时采样" title="进程内实时采样" />
          </div>
          <p>观察查询耗时、阶段拆分和缓存访问路径。</p>
        </div>
        <div className="monitor-heading-actions">
          <span className="monitor-threshold">慢查询 ≥ {elapsed(currentSummary.threshold_ms)}</span>
          <button onClick={() => void refresh()} disabled={busy} title="刷新监控数据">
            <RefreshCw size={14} className={busy ? 'spin' : ''} />
            刷新
          </button>
        </div>
      </header>

      <div className="monitor-content">
        <div className="monitor-kpis">
          <div className="monitor-kpi">
            <span>
              <Timer size={14} />
              查询样本
            </span>
            <strong>{currentSummary.sampled_queries}</strong>
            <small>
              {currentSummary.successful_queries} 成功 · {currentSummary.failed_queries} 失败
            </small>
          </div>
          <div className="monitor-kpi">
            <span>
              <BarChart3 size={14} />
              P95 延迟
            </span>
            <strong>{elapsed(currentSummary.p95_ms)}</strong>
            <small>平均 {elapsed(currentSummary.avg_ms)}</small>
          </div>
          <div className="monitor-kpi warning">
            <span>
              <AlertTriangle size={14} />
              慢查询
            </span>
            <strong>{currentSummary.slow_queries}</strong>
            <small>峰值 {elapsed(currentSummary.max_ms)}</small>
          </div>
          <div className="monitor-kpi">
            <span>
              <Database size={14} />
              缓存命中率
            </span>
            <strong>{number(currentSummary.cache_hit_rate * 100, 1)}%</strong>
            <small>
              {currentSummary.cache_evictions} 次淘汰 · {currentSummary.storage_policy.toUpperCase()}
            </small>
          </div>
        </div>

        <div className="monitor-overview-grid">
          <section className="monitor-card monitor-trend-card">
            <div className="monitor-card-heading">
              <div>
                <strong>延迟趋势</strong>
                <span>最近 {currentSummary.latency_series.length} 条观测</span>
              </div>
              <code>P50 {elapsed(currentSummary.p50_ms)}</code>
            </div>
            <LatencyChart samples={currentSummary.latency_series} threshold={currentSummary.threshold_ms} />
          </section>
          <section className="monitor-card monitor-io-card">
            <div className="monitor-card-heading">
              <div>
                <strong>存储访问</strong>
                <span>查询期间累计页级指标</span>
              </div>
              <Server size={15} />
            </div>
            <dl className="monitor-io-list">
              <div>
                <dt>页读取</dt>
                <dd>{currentSummary.page_reads}</dd>
              </div>
              <div>
                <dt>页写入</dt>
                <dd>{currentSummary.page_writes}</dd>
              </div>
              <div>
                <dt>缓存命中</dt>
                <dd>{currentSummary.cache_hits}</dd>
              </div>
              <div>
                <dt>缓存未命中</dt>
                <dd>{currentSummary.cache_misses}</dd>
              </div>
            </dl>
          </section>
        </div>

        <div className="monitor-main-grid">
          <section className="monitor-card monitor-query-card">
            <div className="monitor-card-heading">
              <div>
                <strong>慢查询列表</strong>
                <span>{queries.length ? `${queries.length} 条，按耗时排序` : '达到阈值后自动出现'}</span>
              </div>
              <span className="monitor-count">{currentSummary.slow_queries}</span>
            </div>
            {queries.length ? (
              <div className="monitor-query-list">
                {queries.map(query => (
                  <button
                    className={`monitor-query-row ${query.query_id === selectedId ? 'active' : ''}`}
                    key={query.query_id}
                    onClick={() => setSelectedId(query.query_id)}
                  >
                    <span className="monitor-query-sql" title={query.sql}>
                      {queryTitle(query)}
                    </span>
                    <code>{elapsed(query.total_ms)}</code>
                    <small>
                      {time(query.finished_at)} · {query.operator ?? 'Statement'}
                    </small>
                  </button>
                ))}
              </div>
            ) : (
              <div className="monitor-empty">
                <Gauge size={24} />
                <strong>暂无慢查询</strong>
                <span>当前采样会保留在延迟趋势中。</span>
              </div>
            )}
          </section>

          <section className="monitor-card monitor-detail-card">
            <div className="monitor-card-heading">
              <div>
                <strong>查询详情</strong>
                <span>{detailBusy ? '正在读取阶段明细…' : selected ? `语句 ${selected.statement_index + 1}` : '选择一条查询'}</span>
              </div>
            </div>
            {selected ? (
              <div className="monitor-detail">
                <pre className="monitor-sql">{selected.sql}</pre>
                <div className="monitor-detail-meta">
                  <span className={selected.status === 'success' ? 'success-text' : 'error-text'}>
                    {selected.status === 'success' ? '成功' : (selected.error_code ?? '执行失败')}
                  </span>
                  <span>{time(selected.finished_at)}</span>
                  <span>{selected.rows_returned} 行结果</span>
                </div>
                <div className="monitor-timing">
                  <TimingBar label="排队" value={selected.queue_wait_ms} total={selected.total_ms} tone="#9aa9b7" />
                  <TimingBar label="编译" value={selected.compile_ms} total={selected.total_ms} tone="#6baaa3" />
                  <TimingBar label="执行" value={selected.execute_ms} total={selected.total_ms} tone="#087f78" />
                  <TimingBar label="物化" value={selected.materialize_ms} total={selected.total_ms} tone="#e1a04c" />
                </div>
                <div className="monitor-detail-stats">
                  <span>
                    检查行 <strong>{selected.rows_examined}</strong>
                  </span>
                  <span>
                    页读 <strong>{selected.page_reads}</strong>
                  </span>
                  <span>
                    命中 <strong>{selected.cache_hits}</strong>
                  </span>
                  <span>
                    淘汰 <strong>{selected.cache_evictions}</strong>
                  </span>
                  {detail?.plan_estimate && (
                    <span title="优化器估算值，单位为 cost，不是毫秒">
                      估算成本 <strong>{detail.plan_estimate.total_cost.toFixed(2)}</strong>
                    </span>
                  )}
                </div>
                {detail?.plan && (
                  <details className="monitor-plan" open>
                    <summary>执行计划</summary>
                    <JsonTree value={detail.plan} />
                  </details>
                )}
              </div>
            ) : (
              <div className="monitor-empty detail-empty">
                <Activity size={24} />
                <span>选择慢查询查看阶段拆分。</span>
              </div>
            )}
          </section>
        </div>

        <section className="monitor-card monitor-events-card">
          <div className="monitor-card-heading">
            <div>
              <strong>最近缓存淘汰</strong>
              <span>用于核对 LRU/FIFO 替换和脏页回写</span>
            </div>
            <code>{currentSummary.storage_events.length} 条事件</code>
          </div>
          {currentSummary.storage_events.length ? (
            <div className="monitor-events-list">
              {currentSummary.storage_events
                .slice(-8)
                .reverse()
                .map(event => (
                  <span key={`${event.at}-${event.page_id}`}>
                    <code>{time(event.at)}</code>
                    <strong>页 {event.page_id}</strong>
                    <small>
                      {event.policy.toUpperCase()} · {event.writeback ? '脏页回写' : '无需回写'}
                    </small>
                  </span>
                ))}
            </div>
          ) : (
            <p className="monitor-event-empty">暂无淘汰事件；缓存容量未触发替换。</p>
          )}
        </section>
      </div>
    </section>
  )
}
