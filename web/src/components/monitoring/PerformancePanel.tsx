/** 性能监控工作区：延迟摘要、慢查询和缓存淘汰诊断。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, BarChart3, Database, Gauge, Pause, Play, RefreshCw, Timer } from 'lucide-react'
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

function signalLabel(signal: string): string {
  const labels: Record<string, string> = {
    io: 'I/O',
    cache_eviction: '缓存淘汰',
    scan_amplification: '扫描放大',
    queue: '排队过长',
    latency_regression: '延迟回归'
  }
  return labels[signal] ?? signal
}

function sampleNumber(sample: MonitorSample, key: 'total_ms' | 'page_reads' | 'page_writes' | 'cache_hits' | 'cache_misses'): number {
  const value = sample[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
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

type LiveMetric = 'total_ms' | 'page_reads' | 'page_writes' | 'cache_hits' | 'cache_misses' | 'success' | 'failed'
type MonitorSample = MonitorSummary['latency_series'][number]

interface LiveLine {
  key: LiveMetric
  label: string
  color: string
}

interface LiveCardConfig {
  title: string
  lines: LiveLine[]
  threshold?: number
  latest: (sample: MonitorSample) => string
}

const LIVE_CARDS: LiveCardConfig[] = [
  {
    title: '查询延迟',
    lines: [{ key: 'total_ms', label: '耗时', color: '#087f78' }],
    latest: sample => elapsed(sampleNumber(sample, 'total_ms'))
  },
  {
    title: '页 I/O',
    lines: [
      { key: 'page_reads', label: '读入', color: '#2d9fe3' },
      { key: 'page_writes', label: '写出', color: '#e15a3a' }
    ],
    latest: sample => `读 ${sampleNumber(sample, 'page_reads').toLocaleString()} · 写 ${sampleNumber(sample, 'page_writes').toLocaleString()}`
  },
  {
    title: '缓存访问',
    lines: [
      { key: 'cache_hits', label: '命中', color: '#087f78' },
      { key: 'cache_misses', label: '未命中', color: '#e1a04c' }
    ],
    latest: sample => `命中 ${sampleNumber(sample, 'cache_hits').toLocaleString()} · 未命中 ${sampleNumber(sample, 'cache_misses').toLocaleString()}`
  },
  {
    title: '查询状态',
    lines: [
      { key: 'success', label: '成功', color: '#2d9fe3' },
      { key: 'failed', label: '失败', color: '#e15a3a' }
    ],
    latest: sample => (sample.status === 'success' ? '最近成功' : '最近失败')
  }
]

function liveValue(sample: MonitorSample, key: LiveMetric): number {
  if (key === 'success') return sample.status === 'success' ? 1 : 0
  if (key === 'failed') return sample.status === 'success' ? 0 : 1
  return sampleNumber(sample, key)
}

function LiveMetricChart({ config, samples }: { config: LiveCardConfig; samples: MonitorSample[] }) {
  const chart = useMemo(() => {
    if (!samples.length) return null
    const max = Math.max(config.threshold ?? 0, ...config.lines.flatMap(line => samples.map(sample => liveValue(sample, line.key))), 1)
    const lines = config.lines.map(line => ({
      ...line,
      points: samples.map((sample, index) => {
        const x = samples.length === 1 ? 50 : (index / (samples.length - 1)) * 100
        const y = 92 - (liveValue(sample, line.key) / max) * 78
        return { x, y: Math.max(10, y) }
      })
    }))
    return {
      lines,
      thresholdY: config.threshold == null ? null : 92 - (config.threshold / max) * 78
    }
  }, [config, samples])

  return (
    <div className="monitor-live-chart-wrap">
      {chart ? (
        <svg className="monitor-live-chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={`${config.title}动态趋势`}>
          {[25, 50, 75].map(level => (
            <line key={level} x1="0" x2="100" y1={level} y2={level} className="monitor-live-grid-line" />
          ))}
          {chart.thresholdY != null && <line x1="0" x2="100" y1={chart.thresholdY} y2={chart.thresholdY} className="monitor-live-threshold" />}
          {chart.lines.map(line => (
            <g key={line.key}>
              <polyline
                points={line.points.map(point => `${point.x},${point.y}`).join(' ')}
                className="monitor-live-line"
                style={{ stroke: line.color }}
              />
              <circle
                cx={line.points[line.points.length - 1].x}
                cy={line.points[line.points.length - 1].y}
                r="1.8"
                className="monitor-live-point"
                style={{ fill: line.color }}
              />
            </g>
          ))}
        </svg>
      ) : (
        <div className="monitor-live-chart-empty">
          <Activity size={18} />
          <span>执行查询后显示趋势</span>
        </div>
      )}
      <div className="monitor-live-axis">
        <span>{samples.length ? time(samples[0].at) : '—'}</span>
        <span>{config.threshold != null ? `阈值 ${elapsed(config.threshold)}` : `${samples.length} 个采样`}</span>
        <span>{samples.length ? time(samples[samples.length - 1].at) : '—'}</span>
      </div>
      <div className="monitor-live-legend">
        {config.lines.map(line => (
          <span key={line.key}>
            <i style={{ background: line.color }} />
            {line.label}
          </span>
        ))}
      </div>
    </div>
  )
}

function LiveMetricGrid({ samples, threshold }: { samples: MonitorSample[]; threshold: number }) {
  return (
    <div className="monitor-live-grid" aria-label="动态监控指标">
      {LIVE_CARDS.map(config => (
        <section className="monitor-live-card" key={config.title}>
          <div className="monitor-live-card-heading">
            <div>
              <strong>{config.title}</strong>
              <span>{samples.length ? config.latest(samples[samples.length - 1]) : '等待采样'}</span>
            </div>
            <span className="monitor-live-badge">LIVE</span>
          </div>
          <LiveMetricChart config={{ ...config, threshold: config.title === '查询延迟' ? threshold : undefined }} samples={samples} />
        </section>
      ))}
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
  const [autoRefresh, setAutoRefresh] = useState(true)

  const refresh = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const [nextSummary, nextQueries] = await Promise.all([
        api<MonitorSummary>('/api/monitor/summary'),
        api<MonitorQueriesResponse>('/api/monitor/queries?attention=1&limit=100')
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
    if (!active || !autoRefresh) return
    const timer = window.setInterval(() => void refresh(), 3000)
    return () => window.clearInterval(timer)
  }, [active, autoRefresh, refresh])

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
    diagnostic_sample_rate: 0.01,
    sampled_queries: 0,
    successful_queries: 0,
    failed_queries: 0,
    slow_queries: 0,
    execution_slow_queries: 0,
    queue_slow_queries: 0,
    latency_regressions: 0,
    resource_warnings: 0,
    diagnostic_samples: 0,
    lightweight_samples: 0,
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
    log_failures: 0,
    log_dropped: 0
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
          <span
            className="monitor-threshold"
            title={`普通查询只保留轻量指标；诊断样本含 Trace 和执行计划，默认抽样 ${(currentSummary.diagnostic_sample_rate * 100).toFixed(1)}%`}
          >
            轻量 {currentSummary.lightweight_samples} · 诊断 {currentSummary.diagnostic_samples} · 资源 {currentSummary.resource_warnings}
          </span>
          <button
            className="monitor-live-toggle"
            onClick={() => setAutoRefresh(current => !current)}
            title={autoRefresh ? '暂停动态监控' : '继续动态监控'}
          >
            {autoRefresh ? <Pause size={13} /> : <Play size={13} />}
            {autoRefresh ? '暂停' : '继续'}
          </button>
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

        <LiveMetricGrid samples={currentSummary.latency_series} threshold={currentSummary.threshold_ms} />

        <div className="monitor-main-grid">
          <section className="monitor-card monitor-query-card">
            <div className="monitor-card-heading">
              <div>
                <strong>需关注查询</strong>
                <span>{queries.length ? `${queries.length} 条，按耗时排序` : '达到阈值或资源异常后自动出现'}</span>
              </div>
              <span className="monitor-count">{queries.length}</span>
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
                      {query.resource_signals?.length ? ` · ${query.resource_signals.map(signalLabel).join('、')}` : ''}
                    </small>
                  </button>
                ))}
              </div>
            ) : (
              <div className="monitor-empty">
                <Gauge size={24} />
                <strong>暂无需关注查询</strong>
                <span>当前观测会保留在延迟趋势中。</span>
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
                  {selected.resource_signals?.length ? (
                    <span>
                      关注 <strong>{selected.resource_signals.map(signalLabel).join('、')}</strong>
                    </span>
                  ) : null}
                  {selected.latency_regression && selected.baseline_ms != null ? (
                    <span>
                      基线 <strong>{elapsed(selected.baseline_ms)}</strong>
                    </span>
                  ) : null}
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
                <span>选择查询查看阶段拆分。</span>
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
